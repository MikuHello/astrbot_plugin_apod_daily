"""APOD 插件修复的回归测试。

本机没有安装 astrbot，测试前用最小桩替换 `astrbot.*` 依赖，
再直接从源码路径加载 main.py，只覆盖纯解析/渲染/编排逻辑。
"""
import asyncio
import atexit
import importlib.util
import logging
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
import zoneinfo
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

REPO_DIR = Path(__file__).resolve().parent.parent
MAIN_PATH = REPO_DIR / "main.py"

MP4 = (
    "https://assets.science.nasa.gov/content/dam/science/cds/apod/apod/"
    "2026/october/CRS35-Holland30s1080p.mp4"
)
LINK = (
    "https://science.nasa.gov/image-article/"
    "apod-2026-october-16-chasing-the-international-space-station/"
)
OLD_LINK = (
    "https://science.nasa.gov/image-article/apod-2026-november-06-old/"
)

VIDEO_ARTICLE = f"""
<article>
<h1>Chasing the International Space Station</h1>
<video class="video-js" controls preload="auto">
  <source src="{MP4}" type="video/mp4">
</video>
<p>Explanation: A SpaceX Falcon 9 rocket carrying the CRS-35 Dragon
  lifted off and the booster returned to Landing Zone 40.</p>
<p>Video Credit: Eric Holland</p>
<p>Authors &amp; editors</p>
</article>
"""

IMAGE_ARTICLE = """
<article>
<h1>3D 67P</h1>
<img src="https://assets.science.nasa.gov/content/dam/science/cds/apod/apod/2026/october/67p.jpg">
<p>Explanation: A stereo image of comet 67P rendered for cross-eyed viewing,
  produced from Rosetta mission data and released by the mission team.</p>
<p>Image Credit: Jane Doe</p>
</article>
"""

NO_MEDIA_ARTICLE = """
<article>
<h1>Some Text Only Entry</h1>
<p>Explanation: This entry has neither an image nor a video element at all,
  so the plugin must fall back to text plus the original article link.</p>
</article>
"""

RSS_VIDEO = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:media="http://search.yahoo.com/mrss/">
<channel>
<item>
  <title>APOD: 2026 October 16 &#8211; Chasing the International Space Station</title>
  <link>{LINK}</link>
  <media:content url="{MP4}" type="video/mp4" medium="video" />
  <content:encoded><![CDATA[
  <p>Explanation: A SpaceX Falcon 9 rocket carrying the CRS-35 Dragon
     lifted off and the booster returned to Landing Zone 40.</p>
  ]]></content:encoded>
  <pubDate>Fri, 16 Oct 2026 04:05:00 +0000</pubDate>
</item>
</channel>
</rss>
"""


YOUTUBE_EMBED = "https://www.youtube.com/embed/DzrCEm1ZBRY?feature=oembed"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v=DzrCEm1ZBRY"
YOUTUBE_LINK = (
    "https://science.nasa.gov/image-article/"
    "apod-2026-october-18-sonified-eagle-nebula-pillars/"
)
POSTER = (
    "https://assets.science.nasa.gov/dynamicimage/assets/science/cds/apod/apod/"
    "2026/october/SonifiedEagleNebulaPillars.jpg?w=3048&h=1708&fit=clip"
)
POSTER_XML = POSTER.replace("&", "&#038;")

YOUTUBE_ARTICLE = f"""
<article>
<h1>Sonified: Eagle Nebula Pillars</h1>
<iframe title="Sonified: Eagle Nebula Pillars" src="{YOUTUBE_EMBED}"></iframe>
<p>Explanation: The Pillars of Creation are rendered as sound in this
  sonification of Hubble data.</p>
</article>
"""

RSS_YOUTUBE = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:media="http://search.yahoo.com/mrss/">
<channel>
<item>
  <title>APOD: 2026 October 18 &#8211; Sonified: Eagle Nebula Pillars</title>
  <link>{YOUTUBE_LINK}</link>
  <media:content url="https://www.youtube.com/embed/DzrCEm1ZBRY" medium="video">
    <media:thumbnail url="{POSTER_XML}" />
  </media:content>
  <content:encoded><![CDATA[
  <p>Explanation: The Pillars of Creation are rendered as sound in this
     sonification of Hubble data.</p>
  <iframe src="{YOUTUBE_EMBED}"></iframe>
  ]]></content:encoded>
  <pubDate>Sun, 18 Oct 2026 04:05:00 +0000</pubDate>
</item>
</channel>
</rss>
"""


TOMORROW_HTML = """
<article>
<h1>Example</h1>
<p>Explanation: The sky is dark tonight, and the stars are out.
  <br><br><strong>Tomorrow&#8217;s picture:</strong> eye in the sky
</p>
</article>
"""


class _FakeSP:
    def __init__(self):
        self.prefs = []
        self.removed = []

    async def range_get_async(self, scope, scope_id, key=None):
        return list(self.prefs)

    async def remove_async(self, scope, scope_id, key):
        self.removed.append(key)


def _use_isolated_plugin_data_dir(case=None):
    """把插件数据目录指到一个独立临时目录，用例结束（或进程退出）时删除。

    图片缓存、缓存索引等都会写到这里；只注册 mock 清理会把目录和图片留在
    系统临时目录里（
    跑一次测试就多几个 apod-* 目录）。
    """
    import astrbot.core.utils.astrbot_path as astrbot_path

    directory = tempfile.mkdtemp(prefix="apod-plugin-data-")
    patcher = patch.object(
        astrbot_path, "get_astrbot_plugin_data_path", return_value=directory
    )
    patcher.start()
    if case is None:
        atexit.register(shutil.rmtree, directory, True)
        return directory
    case.addCleanup(shutil.rmtree, directory, True)
    case.addCleanup(patcher.stop)
    return directory


def _install_astrbot_stubs():
    def mod(name):
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
        return module

    astrbot = mod("astrbot")
    api = mod("astrbot.api")
    api.__path__ = []
    astrbot.api = api

    class _Component:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Plain(_Component):
        def __init__(self, text):
            super().__init__(text=text)

    class Image(_Component):
        @staticmethod
        def fromURL(url):
            return Image(file=url)

        @staticmethod
        def fromFileSystem(path):
            return Image(file=path, path=path)

    class Video(_Component):
        @staticmethod
        def fromURL(url):
            return Video(file=url)

    class Reply(_Component):
        def __init__(self, id=None):
            super().__init__(id=id)

    comp = mod("astrbot.api.message_components")
    comp.Plain, comp.Image, comp.Video = Plain, Image, Video
    comp.Reply = Reply
    api.message_components = comp

    class AstrBotConfig(dict):
        pass

    api.AstrBotConfig = AstrBotConfig
    api.logger = logging.getLogger("apod-test")

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

    class _Filter:
        def _identity(self):
            def deco(func):
                return func

            return deco

        def command(self, *args, **kwargs):
            return self._identity()

        def llm_tool(self, *args, **kwargs):
            return self._identity()

        def permission_type(self, *args, **kwargs):
            return self._identity()

    event = mod("astrbot.api.event")

    class AstrMessageEvent:  # noqa: N801 - 与 AstrBot API 同名
        pass

    event.AstrMessageEvent = AstrMessageEvent
    event.MessageChain = MessageChain
    event.filter = _Filter()
    api.event = event

    event_filter = mod("astrbot.api.event.filter")

    class PermissionType:
        GROUP_ADMIN = "GROUP_ADMIN"

    event_filter.PermissionType = PermissionType

    star = mod("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context, config):
            self.context = context
            self.config = config

    def register(*args, **kwargs):
        def deco(cls):
            return cls

        return deco

    star.Context, star.Star, star.register = Context, Star, register

    core = mod("astrbot.core")
    core.sp = _FakeSP()
    core_utils = mod("astrbot.core.utils")
    core_utils.__path__ = []
    core.utils = core_utils
    astrbot_path = mod("astrbot.core.utils.astrbot_path")
    _PLUGIN_DATA_DIR = tempfile.mkdtemp(prefix="apod-plugin-data-")
    atexit.register(shutil.rmtree, _PLUGIN_DATA_DIR, True)
    astrbot_path.get_astrbot_plugin_data_path = lambda: _PLUGIN_DATA_DIR
    core_utils.astrbot_path = astrbot_path

    # 插件通过 astrbot.core.utils.io.download_file 下载图片（trust_env=True）。
    io_utils = mod("astrbot.core.utils.io")

    class DownloadFileHTTPError(RuntimeError):
        """与 AstrBot 同名异类：测试只会用它的文本形式。"""

    async def download_file(url, path, *args, **kwargs):
        raise AssertionError("测试必须先 patch download_file / _download_image")

    io_utils.DownloadFileHTTPError = DownloadFileHTTPError
    io_utils.download_file = download_file
    core_utils.io = io_utils

    # aiohttp 只在网络层使用，解析/渲染测试不需要真实实现。
    aiohttp = mod("aiohttp")

    class ClientTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class ClientError(Exception):
        pass

    class ClientConnectorError(ClientError):
        """“连不上”属于可确认未提交的失败（与 aiohttp 同名）。"""

        def __init__(self, *args, **kwargs):
            super().__init__("Cannot connect to host")

    class ClientPayloadError(ClientError):
        """已提交、读响应体时断连（结果未知）。"""

        def __init__(self, *args, **kwargs):
            super().__init__("Response payload is not completed")

    class InvalidURL(ClientError):
        pass

    class ClientSession:
        def __init__(self, *args, **kwargs):
            pass

    aiohttp.ClientTimeout = ClientTimeout
    aiohttp.ClientError = ClientError
    aiohttp.ClientConnectorError = ClientConnectorError
    aiohttp.ClientPayloadError = ClientPayloadError
    aiohttp.InvalidURL = InvalidURL
    aiohttp.ClientSession = ClientSession
    return comp


_COMP = _install_astrbot_stubs()
_spec = importlib.util.spec_from_file_location("apod_main", MAIN_PATH)
apod_main = importlib.util.module_from_spec(_spec)
sys.modules["apod_main"] = apod_main
_spec.loader.exec_module(apod_main)

# 本机测试环境没有 tzdata（运行时 AstrBot 自带），统一固定为同一个 ET 时区。
try:
    _ET = zoneinfo.ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 - 本地无 tzdata 时退化为固定 EDT
    _ET = timezone(timedelta(hours=-4))
apod_main.APOD._et_timezone = staticmethod(lambda: _ET)


class _LogCapture(logging.Handler):
    """收集日志行，供需要断言“什么都没记”的用例使用（assertLogs 做不到）。"""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _capture_logs(case, level=logging.INFO):
    handler = _LogCapture()
    logger = logging.getLogger("apod-test")
    previous = logger.level
    logger.setLevel(level)
    logger.addHandler(handler)
    case.addCleanup(logger.removeHandler, handler)
    case.addCleanup(logger.setLevel, previous)
    return handler


class _FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    async def save_config_async(self):
        self.saved += 1


def _pre_submit_error():
    """能证明没提交的发送失败（本地媒体准备阶段）：只有这种才允许自动补发。"""
    return FileNotFoundError("image cache miss")


def _network_error():
    """网络类失败：适配器/QQ SDK 内部可能已重试甚至已提交，只能算结果未知。"""
    return apod_main.aiohttp.ClientConnectorError()


def _post_submit_error():
    """已提交、读响应时断连：结果未知，不得自动重发。"""
    return apod_main.aiohttp.ClientPayloadError()


def _make_plugin(config=None):
    plugin = apod_main.APOD(None, config if config is not None else {})
    plugin.plugin_id = "mikuhello/astrbot_plugin_apod_daily"
    plugin.send_media = True
    plugin.send_title = True
    plugin.send_explanation = True
    plugin.send_date = True
    plugin.translate_title = True
    plugin.translate_explanation = True
    plugin.translate_parse_retry = 1
    plugin.cache_limit = 1000
    plugin.cache_images = False
    plugin.is_divided = True
    plugin.quote_reply = False
    plugin.timeout = 30
    # initialize() 才读取配置，夹具直接给出与默认配置一致的网络重试次数。
    plugin.retry_count = 3
    return plugin


def _make_reply_event(message_id="msg-1"):
    event = SimpleNamespace(
        unified_msg_origin="umo",
        message_obj=SimpleNamespace(message_id=message_id),
        sent=[],
        stopped=False,
    )

    async def send(chain):
        event.sent.append(chain)

    def stop_event():
        event.stopped = True

    event.send = send
    event.stop_event = stop_event
    return event


class TitlePrefixTests(unittest.TestCase):
    """feed 标题前缀剥离：两种日期顺序都要处理干净。"""

    def setUp(self):
        self.plugin = _make_plugin()

    def test_title_prefix_and_date_formats(self):
        cases = [
            ("APOD: 2026 October 16 – Stars", "Stars", "2026-10-16"),
            ("APOD: 29 October 2026 – Stars", "Stars", "2026-10-29"),
            ("APOD: 29 Oct 2026 – Stars", "Stars", "2026-10-29"),
            ("APOD: 2026 Oct 29 – Stars", "Stars", "2026-10-29"),
            ("APOD: May 5, 2026 – Stars", "Stars", "2026-05-05"),
            ("APOD: Nov 3, 2026 – Stars", "Stars", "2026-11-03"),
            ("APOD: May Sky Wonders – Part II", "May Sky Wonders – Part II", None),
            ("APOD: 3D 67P – Part II", "3D 67P – Part II", None),
            ("APOD: May 5 Missions – Part II", "May 5 Missions – Part II", None),
            ("The Horns of Venus", "The Horns of Venus", None),
            ("APOD: 2026 October 29", "APOD: 2026 October 29", "2026-10-29"),
        ]
        for raw, title, date in cases:
            with self.subTest(raw=raw):
                data = self.plugin._build_apod_data(raw, LINK, IMAGE_ARTICLE)
                self.assertEqual(data["title"], title)
                if date:
                    self.assertEqual(data["date"], date)

    def test_feed_entry_with_day_first_date(self):
        rss = RSS_VIDEO.replace(
            "APOD: 2026 October 16 &#8211; Chasing the International Space Station",
            "APOD: 16 October 2026 &#8211; Chasing the International Space Station",
        )
        data = self.plugin._parse_feed(rss)
        self.assertIsNotNone(data)
        self.assertEqual(data["title"], "Chasing the International Space Station")
        self.assertEqual(data["date"], "2026-10-16")


class VideoParsingTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    def test_html_parser_collects_video_and_og_video(self):
        parser = apod_main._HTMLAttrParser()
        parser.feed(
            '<meta property="og:video" content="%s"/>'
            '<video src="a.mp4"></video>'
            '<source src="b.mp4">' % MP4
        )
        self.assertEqual(parser.og_video, MP4)
        self.assertEqual(parser.video_srcs, ["a.mp4", "b.mp4"])

    def test_feed_video_entry_becomes_video_media(self):
        data = self.plugin._parse_feed(RSS_VIDEO)
        self.assertIsNotNone(data)
        self.assertEqual(data["media_type"], "video")
        self.assertEqual(data["url"], MP4)
        self.assertEqual(data["date"], "2026-10-16")
        self.assertEqual(data["link"], LINK)

    def test_build_apod_data_from_video_article(self):
        data = self.plugin._build_apod_data(
            "APOD: 2026 October 16 \u2013 Chasing the International Space Station",
            LINK,
            VIDEO_ARTICLE,
        )
        self.assertEqual(data["media_type"], "video")
        self.assertEqual(data["url"], MP4)
        self.assertEqual(data["hdurl"], MP4)
        self.assertEqual(data["date"], "2026-10-16")
        self.assertEqual(data["credit"], "Eric Holland")
        self.assertIn("Landing Zone 40", data["explanation"])

    def test_explanation_stops_before_video_credit(self):
        data = self.plugin._build_apod_data("APOD: t", LINK, VIDEO_ARTICLE)
        self.assertTrue(data["explanation"].endswith("Landing Zone 40."))

    def test_explanation_stops_before_image_credit(self):
        data = self.plugin._build_apod_data(
            "APOD: 2026 October 17 \u2013 3D 67P", LINK, IMAGE_ARTICLE
        )
        self.assertTrue(data["explanation"].endswith("mission team."))

    def test_image_entry_still_parses_as_image(self):
        data = self.plugin._build_apod_data(
            "APOD: 2026 October 17 \u2013 3D 67P", LINK, IMAGE_ARTICLE
        )
        self.assertEqual(data["media_type"], "image")
        self.assertTrue(data["url"].endswith("67p.jpg"))
        self.assertEqual(data["credit"], "Jane Doe")

    def test_video_preferred_over_image(self):
        data = self.plugin._build_apod_data(
            "APOD: 2026 October 16 \u2013 Chasing the International Space Station",
            LINK,
            VIDEO_ARTICLE,
            image_url=(
                "https://assets.science.nasa.gov/content/dam/science/cds/apod/"
                "apod/2026/october/poster.jpg"
            ),
        )
        self.assertEqual(data["media_type"], "video")
        self.assertEqual(data["url"], MP4)

    def test_no_media_entry_stays_other(self):
        data = self.plugin._build_apod_data("APOD: title", LINK, NO_MEDIA_ARTICLE)
        self.assertEqual(data["media_type"], "other")
        self.assertIsNone(data["url"])

    def test_missing_explanation_marker_is_invalid(self):
        html = "<article><h1>t</h1><p>No marker, just prose.</p></article>"
        self.assertIsNone(self.plugin._build_apod_data("APOD: t", LINK, html))

    def test_nasa_video_logo_placeholder_is_not_used_as_image(self):
        page = (
            '<html><head>'
            '<meta property="og:image" '
            'content="https://science.nasa.gov/wp-content/uploads/2023/10/NASA_logo-1.png"/>'
            f'<meta property="og:video" content="{MP4}"/>'
            '<meta property="article:published_time" content="2026-10-16T00:00:00+00:00"/>'
            '</head><body>'
            '<article><h1>Chasing the International Space Station</h1>'
            f'<video><source src="{MP4}" type="video/mp4"></video>'
            '<p>A SpaceX Falcon 9 rocket boosted the CRS-35 Dragon toward the'
            ' station before the booster returned to Landing Zone 40.</p>'
            '<p>Video Credit: Eric Holland</p></article></body></html>'
        )
        data = self.plugin._build_nasa_data(page, LINK)
        self.assertEqual(data["media_type"], "video")
        self.assertEqual(data["url"], MP4)
        self.assertEqual(data["credit"], "Eric Holland")
        self.assertEqual(data["date"], "2026-10-16")


class YouTubeVideoTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    def test_html_parser_collects_iframe_srcs(self):
        parser = apod_main._HTMLAttrParser()
        parser.feed(f'<iframe src="{YOUTUBE_EMBED}"></iframe>')
        self.assertEqual(parser.iframe_srcs, [YOUTUBE_EMBED])

    def test_youtube_urls_normalize_to_watch_link(self):
        for url in (
            YOUTUBE_EMBED,
            "https://youtu.be/DzrCEm1ZBRY",
            "https://www.youtube.com/watch?v=DzrCEm1ZBRY&t=30s",
            "https://m.youtube.com/watch?v=DzrCEm1ZBRY",
            "https://www.youtube.com/shorts/DzrCEm1ZBRY",
            "https://www.youtube-nocookie.com/embed/DzrCEm1ZBRY",
        ):
            self.assertEqual(self.plugin._youtube_watch_url(url), YOUTUBE_WATCH, url)

    def test_protocol_relative_youtube_url_is_accepted(self):
        self.assertEqual(
            self.plugin._youtube_watch_url("//www.youtube.com/embed/DzrCEm1ZBRY"),
            YOUTUBE_WATCH,
        )

    def test_protocol_relative_iframe_is_detected(self):
        html = YOUTUBE_ARTICLE.replace(
            "https://www.youtube.com/embed/", "//www.youtube.com/embed/"
        )
        data = self.plugin._build_apod_data("APOD: t", YOUTUBE_LINK, html)
        self.assertEqual(data["video_platform"], "youtube")
        self.assertEqual(data["url"], YOUTUBE_WATCH)

    def test_youtube_pattern_ignores_other_hosts(self):
        self.assertEqual(self.plugin._youtube_watch_url(MP4), "")
        self.assertEqual(
            self.plugin._youtube_watch_url(
                "https://example.com/?next=https://youtu.be/DzrCEm1ZBRY"
            ),
            "",
        )

    def test_feed_youtube_entry_becomes_video(self):
        data = self.plugin._parse_feed(RSS_YOUTUBE)
        self.assertIsNotNone(data)
        self.assertEqual(data["media_type"], "video")
        self.assertEqual(data["video_platform"], "youtube")
        self.assertEqual(data["url"], YOUTUBE_WATCH)
        self.assertEqual(data["poster_url"], POSTER)
        self.assertEqual(data["date"], "2026-10-18")
        self.assertEqual(data["link"], YOUTUBE_LINK)

    def test_article_iframe_is_detected(self):
        data = self.plugin._build_apod_data(
            "APOD: 2026 October 18 \u2013 Sonified: Eagle Nebula Pillars",
            YOUTUBE_LINK,
            YOUTUBE_ARTICLE,
        )
        self.assertEqual(data["media_type"], "video")
        self.assertEqual(data["video_platform"], "youtube")
        self.assertEqual(data["url"], YOUTUBE_WATCH)

    def test_direct_video_keeps_direct_platform(self):
        data = self.plugin._build_apod_data("APOD: t", LINK, VIDEO_ARTICLE)
        self.assertEqual(data["video_platform"], "direct")
        self.assertEqual(data["url"], MP4)

    def test_display_text_shows_youtube_link(self):
        data = self.plugin._parse_feed(RSS_YOUTUBE)
        text = self.plugin._build_display_text(data, None, None)
        self.assertIn(f"▶ 本期为视频（YouTube）：{YOUTUBE_WATCH}", text)
        self.assertIn(f"原文链接：{YOUTUBE_LINK}", text)
        self.assertNotIn("⚠️", text)

    def test_direct_video_poster_falls_back_to_og_image(self):
        data = self.plugin._build_apod_data(
            "APOD: t", LINK, VIDEO_ARTICLE, image_url=POSTER, video_url=MP4
        )
        self.assertEqual(data["media_type"], "video")
        self.assertEqual(data["video_platform"], "direct")
        self.assertEqual(data["poster_url"], POSTER)

    def test_image_entry_has_no_poster(self):
        data = self.plugin._build_apod_data(
            "APOD: t", LINK, IMAGE_ARTICLE, poster_url=POSTER
        )
        self.assertEqual(data["media_type"], "image")
        self.assertEqual(data["poster_url"], "")

    def test_message_chains_use_poster_instead_of_video(self):
        data = self.plugin._parse_feed(RSS_YOUTUBE)
        chains = asyncio.run(
            self.plugin._build_apod_chains(data, None, None)
        )
        # 默认 is_divided=True：封面帧一条、正文一条。
        self.assertEqual(len(chains), 2)
        self.assertEqual(len(chains[0].chain), 1)
        self.assertNotIsInstance(chains[0].chain[0], _COMP.Video)
        self.assertIsInstance(chains[0].chain[0], _COMP.Image)
        self.assertEqual(chains[0].chain[0].file, POSTER)
        self.assertIsInstance(chains[1].chain[0], _COMP.Plain)

    def test_youtube_poster_merges_when_not_divided(self):
        # YouTube 条目的媒体链是封面帧（图片），关掉 is_divided 时应当
        # 和普通图片期一样合并成一条，而不是因为 media_type=video 恒拆。
        self.plugin.is_divided = False
        data = self.plugin._parse_feed(RSS_YOUTUBE)
        chains = asyncio.run(self.plugin._build_apod_chains(data, None, None))
        self.assertEqual(len(chains), 1)
        self.assertIsInstance(chains[0].chain[0], _COMP.Image)
        self.assertIsInstance(chains[0].chain[-1], _COMP.Plain)

    def test_reply_sends_poster_then_text(self):
        async def translate(apod_data, umo=None):
            return None, None, None

        self.plugin._translate_fields = translate
        event = _make_reply_event()
        data = self.plugin._parse_feed(RSS_YOUTUBE)

        asyncio.run(self.plugin._reply_apod(event, data))
        # 指令路径也走 _build_apod_chains：封面帧一条、正文一条。
        self.assertTrue(event.stopped)
        self.assertEqual(len(event.sent), 2)
        self.assertEqual(len(event.sent[0].chain), 1)
        self.assertIsInstance(event.sent[0].chain[0], _COMP.Image)
        self.assertEqual(event.sent[0].chain[0].file, POSTER)
        self.assertIsInstance(event.sent[1].chain[0], _COMP.Plain)


class TomorrowTitleTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    def _data(self):
        return self.plugin._build_apod_data("APOD: t", LINK, TOMORROW_HTML)

    def test_tomorrow_line_is_extracted(self):
        self.assertEqual(self._data()["tomorrow_title"], "eye in the sky")

    def test_explanation_stops_before_tomorrow_line(self):
        explanation = self._data()["explanation"]
        self.assertTrue(explanation.endswith("stars are out."))
        self.assertNotIn("Tomorrow", explanation)

    def test_missing_tomorrow_line_is_empty(self):
        data = self.plugin._build_apod_data("APOD: t", LINK, IMAGE_ARTICLE)
        self.assertEqual(data["tomorrow_title"], "")

    def test_random_entry_has_no_tomorrow_line(self):
        # 官网 embed 使用 h2，底部 image-article 链接指向相邻两期而非本期。
        for heading in ("h1", "h2", "H2"):
            with self.subTest(heading=heading):
                html = (
                    '<article data-smd-embed-post-id="1230624">'
                    f'<{heading} class="display-48">A <em>Comet</em> &amp; Two Dippers</{heading}>'
                    '<p>Explanation: Some astronomy text.'
                    '<br><strong>Tomorrow&#8217;s picture:</strong> eye in the sky</p>'
                    '<table><tr><th>Date</th><td>February 7, 2023</td></tr></table>'
                    f'<a class="smd-next-prev__prev" href="{LINK}">Yesterday</a>'
                    '</article>'
                )
                data = self.plugin._build_random_apod(html)
                self.assertIsNotNone(data)
                self.assertEqual(data["title"], "A Comet & Two Dippers")
                self.assertEqual(data["link"], "https://science.nasa.gov/?p=1230624")
                self.assertEqual(data["date"], "2023-02-07")
                self.assertEqual(data["tomorrow_title"], "")
                self.assertEqual(data["explanation"], "Some astronomy text.")

    def test_random_entry_without_post_identity_does_not_use_neighbor_link(self):
        for post_id in ("", "wrong", "0"):
            with self.subTest(post_id=post_id):
                html = (
                    f'<article data-smd-embed-post-id="{post_id}"><h2>Random</h2>'
                    f'<p>Explanation: Text.</p><a href="{LINK}">Yesterday</a></article>'
                )
                self.assertIsNone(self.plugin._build_random_apod(html))

    def test_tomorrow_line_rendered_after_explanation(self):
        text = self.plugin._build_display_text(self._data(), None, None)
        self.assertIn("\n\n明日一图：eye in the sky", text)
        self.assertGreater(text.index("明日一图："), text.index("说明："))

    def test_tomorrow_line_hidden_with_explanation(self):
        self.plugin.send_explanation = False
        text = self.plugin._build_display_text(self._data(), None, None)
        self.assertNotIn("明日一图：", text)

    def test_translated_tomorrow_line_prefers_translation(self):
        text = self.plugin._build_display_text(
            self._data(), None, None, tomorrow_zh="天空之眼"
        )
        self.assertIn("明日一图：天空之眼（eye in the sky）", text)

    def test_reply_uses_translated_tomorrow(self):
        async def translate(apod_data, umo=None):
            return None, None, "天空之眼"

        self.plugin._translate_fields = translate
        event = _make_reply_event()
        data = self._data()

        asyncio.run(self.plugin._reply_apod(event, data))
        texts = [
            component.text
            for chain in event.sent
            for component in chain.chain
            if isinstance(component, _COMP.Plain)
        ]
        self.assertTrue(texts)
        self.assertIn("明日一图：天空之眼（eye in the sky）", texts[0])


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    def _video_data(self):
        return self.plugin._build_apod_data(
            "APOD: 2026 October 16 \u2013 Chasing the International Space Station",
            LINK,
            VIDEO_ARTICLE,
        )

    def test_display_text_marks_video_and_shows_link(self):
        text = self.plugin._build_display_text(self._video_data(), None, None)
        self.assertIn("▶ 本期为视频", text)
        self.assertIn(f"原文链接：{LINK}", text)
        self.assertIn("Eric Holland", text)

    def test_display_text_honours_send_flags(self):
        self.plugin.send_title = False
        self.plugin.send_date = False
        self.plugin.send_explanation = False
        text = self.plugin._build_display_text(self._video_data(), None, None)
        self.assertNotIn("标题：", text)
        self.assertNotIn("日期：", text)
        self.assertNotIn("说明：", text)

    def test_display_text_other_shows_link_warning(self):
        data = self.plugin._build_apod_data("APOD: title", LINK, NO_MEDIA_ARTICLE)
        text = self.plugin._build_display_text(data, None, None)
        self.assertIn("⚠️", text)
        self.assertIn(f"原文链接：{LINK}", text)

    def test_video_entry_splits_media_and_text(self):
        # 视频期必须拆两条：[Video] + [Plain]。单链会让平台把正文
        # 塞进富媒体消息的 content，视频气泡不渲染，正文静默丢失。
        self.plugin.is_divided = False
        chains = asyncio.run(
            self.plugin._build_apod_chains(self._video_data(), None, None)
        )
        self.assertEqual(len(chains), 2)
        self.assertEqual(len(chains[0].chain), 1)
        self.assertIsInstance(chains[0].chain[0], _COMP.Video)
        self.assertEqual(chains[0].chain[0].file, MP4)
        self.assertEqual(len(chains[1].chain), 1)
        self.assertIsInstance(chains[1].chain[0], _COMP.Plain)
        self.assertIn("▶ 本期为视频", chains[1].chain[0].text)

    def test_image_entry_merges_when_not_divided(self):
        self.plugin.is_divided = False
        data = self.plugin._build_apod_data("APOD: t", LINK, IMAGE_ARTICLE)
        chains = asyncio.run(self.plugin._build_apod_chains(data, None, None))
        self.assertEqual(len(chains), 1)
        self.assertIsInstance(chains[0].chain[0], _COMP.Image)
        self.assertIsInstance(chains[0].chain[-1], _COMP.Plain)

    def test_image_entry_splits_when_divided(self):
        self.plugin.is_divided = True
        data = self.plugin._build_apod_data("APOD: t", LINK, IMAGE_ARTICLE)
        chains = asyncio.run(self.plugin._build_apod_chains(data, None, None))
        self.assertEqual(len(chains), 2)
        self.assertIsInstance(chains[0].chain[0], _COMP.Image)
        self.assertIsInstance(chains[1].chain[0], _COMP.Plain)

    def test_text_only_when_media_disabled(self):
        # 关掉“发送媒体”开关后应当只剩正文链（降级本来就靠这个开关）。
        self.plugin.send_media = False
        chains = asyncio.run(
            self.plugin._build_apod_chains(self._video_data(), None, None)
        )
        self.assertEqual(len(chains), 1)
        self.assertEqual(len(chains[0].chain), 1)
        self.assertIsInstance(chains[0].chain[0], _COMP.Plain)

    def test_delivery_failure_matrix(self):
        image, text = _COMP.Image.fromURL(POSTER), _COMP.Plain("body")
        # 检查真实发送边界，覆盖 None 成功、False、异常和媒体/正文分别失败。
        # 期望值：(text_sent, media_sent, unknown)
        cases = [
            ("merged fallback", [[image, text]], [False, None],
             (True, False, False, False), [["Image", "Plain"], ["Plain"]]),
            ("fallback failure", [[image, text]],
             [_pre_submit_error(), False], (False, False, False, False),
             [["Image", "Plain"], ["Plain"]]),
            ("split media failure", [[image], [text]], [False, None],
             (True, False, False, False), [["Image"], ["Plain"]]),
            ("split text failure", [[image], [text]], [None, False],
             (False, True, False, False), [["Image"], ["Plain"]]),
            ("media only failure", [[image]], [False],
             (False, False, False, False), [["Image"]]),
            ("timeout is unknown", [[image, text]], [asyncio.TimeoutError()],
             (True, True, True, True), [["Image", "Plain"]]),
            ("empty", [], [], (False, False, False, False), []),
        ]
        for name, components, outcomes, expected, attempts in cases:
            with self.subTest(name=name):
                send = AsyncMock(side_effect=outcomes)
                chains = [apod_main.MessageChain(chain=c) for c in components]
                report = asyncio.run(self.plugin._send_chains(chains, send))
                self.assertEqual(report, expected)
                self.assertEqual([
                    [type(item).__name__ for item in call.args[0].chain]
                    for call in send.await_args_list
                ], attempts)

    def test_delivery_timeout_and_cancellation(self):
        chain = apod_main.MessageChain(chain=[_COMP.Plain("body")])

        async def blocked_send(chain):
            await asyncio.Event().wait()

        with patch.object(apod_main, "SEND_TIMEOUT_SECONDS", 0.001):
            report = asyncio.run(self.plugin._send_chains([chain], blocked_send))
        # 超时是“结果未知”：按已送达记账，避免重发同一条消息。
        self.assertTrue(report.text_sent)
        self.assertTrue(report.text_unknown)
        # 卸载取消必须向上传播，不能被当作发送失败吞掉。
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.plugin._send_chains(
                [chain], AsyncMock(side_effect=asyncio.CancelledError)
            ))


class FeedSourceTests(unittest.TestCase):
    NEWS_ONLY = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><item>'
        "<title>NASA News</title><link>https://www.nasa.gov/news/</link>"
        "</item></channel></rss>"
    )

    def setUp(self):
        self.plugin = _make_plugin()
        self.plugin.retry_count = 0

    @staticmethod
    def _session(bodies):
        class _Resp:
            status = 200

            def __init__(self, body):
                self._body = body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def text(self):
                return self._body

        class _Session:
            def get(self, url):
                return _Resp(bodies.get(url, ""))

        return _Session()

    def _fetch(self, bodies):
        return asyncio.run(self.plugin._fetch_from_feed(self._session(bodies)))

    def test_first_source_is_apod_category_feed(self):
        self.assertEqual(
            apod_main.FEED_URLS[0],
            "https://science.nasa.gov/category/apod/feed/",
        )

    def test_uses_first_source_when_it_has_apod(self):
        data = self._fetch({apod_main.FEED_URLS[0]: RSS_YOUTUBE})
        self.assertIsNotNone(data)
        self.assertEqual(data["video_platform"], "youtube")

    def test_falls_back_when_first_source_has_no_apod(self):
        data = self._fetch(
            {
                apod_main.FEED_URLS[0]: self.NEWS_ONLY,
                apod_main.FEED_URLS[1]: RSS_VIDEO,
            }
        )
        self.assertIsNotNone(data)
        self.assertEqual(data["url"], MP4)

    def test_falls_back_when_first_source_is_malformed(self):
        data = self._fetch(
            {apod_main.FEED_URLS[0]: "<rss>", apod_main.FEED_URLS[1]: RSS_VIDEO}
        )
        self.assertIsNotNone(data)
        self.assertEqual(data["url"], MP4)

    def test_all_sources_without_apod_reports_error(self):
        data = self._fetch({url: self.NEWS_ONLY for url in apod_main.FEED_URLS})
        self.assertIsNone(data)
        self.assertIn("未找到", self.plugin.last_apod_error)


class TranslationPromptTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    def test_both_fields_prompt(self):
        prompt = self.plugin._build_translation_system_prompt(True, True)
        self.assertIn("信、达、雅", prompt)
        self.assertIn("专有名词与编号保留原形", prompt)
        self.assertIn("不可译则不译", prompt)
        self.assertIn("禁止项", prompt)
        self.assertIn("直接用于发布", prompt)
        # 只有返回结构示例被代码块包裹
        self.assertIn("```\n<title>译好的标题</title>\n<explanation>译好的说明</explanation>\n```", prompt)
        self.assertIn("<title>译好的标题</title>", prompt)
        self.assertIn("<explanation>译好的说明</explanation>", prompt)

    def test_title_only_prompt(self):
        prompt = self.plugin._build_translation_system_prompt(True, False)
        self.assertIn("<title>译好的标题</title>", prompt)
        self.assertNotIn("<explanation>", prompt)

    def test_input_uses_same_tags(self):
        text = self.plugin._build_translation_input("3D 67P", "Explained here.")
        self.assertIn("<title>3D 67P</title>", text)
        self.assertIn("<explanation>Explained here.</explanation>", text)

    def test_tomorrow_prompt_and_input(self):
        prompt = self.plugin._build_translation_system_prompt(False, True, True)
        self.assertIn("<tomorrow>译好的明日一图预告</tomorrow>", prompt)
        self.assertIn("明日一图：译成简短的中文短语", prompt)
        text = self.plugin._build_translation_input(
            None, "Explained here.", "eye in the sky"
        )
        self.assertIn("<tomorrow>eye in the sky</tomorrow>", text)
        self.assertNotIn("<title>", text)


class TranslationParseTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    def test_parse_both_fields(self):
        title, expl, _ = self.plugin._parse_translation_response(
            "<title>67P 彗星的三维图</title>\n<explanation>这是说明。</explanation>",
            True,
            True,
        )
        self.assertEqual(title, "67P 彗星的三维图")
        self.assertEqual(expl, "这是说明。")

    def test_parse_strips_outer_code_fence_and_prose(self):
        raw = (
            "好的，以下是翻译：\n```xml\n"
            "<title>67P 彗星的三维图</title>\n"
            "<explanation>这是说明。</explanation>\n```\n请查收。"
        )
        title, expl, _ = self.plugin._parse_translation_response(raw, True, True)
        self.assertEqual(title, "67P 彗星的三维图")
        self.assertEqual(expl, "这是说明。")

    def test_title_takes_first_line_and_drops_period(self):
        title, _, _ = self.plugin._parse_translation_response(
            "<title>67P 彗星的三维图。\n多余第二行</title>", True, False
        )
        self.assertEqual(title, "67P 彗星的三维图")

    def test_single_field_falls_back_to_whole_text(self):
        title, _, _ = self.plugin._parse_translation_response("3D 67P", True, False)
        self.assertEqual(title, "3D 67P")

    def test_single_field_essay_fallback_is_rejected(self):
        essay = (
            "3D 67P 是一个天文学术语的组合，需要更多上下文才能准确翻译。"
            "如果是指 67P/Churyumov-Gerasimenko，则可译为 67P彗星的三维图。"
        )
        title, _, _ = self.plugin._parse_translation_response(essay, True, False)
        self.assertIsNone(title)

    def test_unclosed_tag_is_a_failure(self):
        title, expl, _ = self.plugin._parse_translation_response(
            "<title>67P 彗星的三维图</title><explanation>被截断的说明", True, True
        )
        self.assertEqual(title, "67P 彗星的三维图")
        self.assertIsNone(expl)

    def test_empty_title_tag_does_not_raise(self):
        title, expl, _ = self.plugin._parse_translation_response(
            "<title></title><explanation>说明译文。</explanation>", True, True
        )
        self.assertIsNone(title)
        self.assertEqual(expl, "说明译文。")

    def test_partial_tag_is_not_fallbacked(self):
        title, _, _ = self.plugin._parse_translation_response(
            "<title>三维 67P", True, False
        )
        self.assertIsNone(title)

    def test_partial_explanation_tag_is_not_fallbacked(self):
        _, expl, _ = self.plugin._parse_translation_response(
            "<explanation>被截断的说明", False, True
        )
        self.assertIsNone(expl)

    def test_parse_tomorrow_field(self):
        title, expl, tomorrow = self.plugin._parse_translation_response(
            "<title>标题</title><explanation>说明。</explanation>"
            "<tomorrow>天空之眼</tomorrow>",
            True,
            True,
            True,
        )
        self.assertEqual((title, expl, tomorrow), ("标题", "说明。", "天空之眼"))

    def test_partial_tomorrow_tag_is_not_fallbacked(self):
        _, _, tomorrow = self.plugin._parse_translation_response(
            "<tomorrow>天空之眼", False, False, True
        )
        self.assertIsNone(tomorrow)


class _FakeContext:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def llm_generate(self, chat_provider_id=None, system_prompt=None, prompt=None, **kwargs):
        self.calls.append({"system_prompt": system_prompt, "prompt": prompt})
        return SimpleNamespace(completion_text=self.replies.pop(0))


class TranslationFlowTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    def test_one_call_translates_both_fields(self):
        self.plugin.context = _FakeContext(
            ["<title>三维 67P</title><explanation>说明译文。</explanation>"]
        )
        title, expl, _ = asyncio.run(
            self.plugin._translate_uncached("3D 67P", "English text.", "prov")
        )
        self.assertEqual(title, "三维 67P")
        self.assertEqual(expl, "说明译文。")
        self.assertEqual(len(self.plugin.context.calls), 1)

    def test_parse_failure_retries_with_correction_notice(self):
        self.plugin.translate_parse_retry = 1
        self.plugin.context = _FakeContext(
            [
                "3D 67P 是一个天文学术语的组合，需要更多上下文……",
                "<title>三维 67P</title><explanation>说明译文。</explanation>",
            ]
        )
        title, expl, _ = asyncio.run(
            self.plugin._translate_uncached("3D 67P", "English text.", "prov")
        )
        self.assertEqual((title, expl), ("三维 67P", "说明译文。"))
        self.assertEqual(len(self.plugin.context.calls), 2)
        first = self.plugin.context.calls[0]["prompt"]
        second = self.plugin.context.calls[1]["prompt"]
        self.assertNotIn("上次输出未遵守结构", first)
        self.assertIn("上次输出未遵守结构", second)
        self.assertIn("<title>…</title>", second)
        self.assertIn("<explanation>…</explanation>", second)

    def test_no_retry_returns_partial(self):
        self.plugin.translate_parse_retry = 0
        self.plugin.context = _FakeContext(["<title>三维 67P</title>没有说明标签"])
        title, expl, _ = asyncio.run(
            self.plugin._translate_uncached("3D 67P", "English text.", "prov")
        )
        self.assertEqual(title, "三维 67P")
        self.assertIsNone(expl)

    def test_tomorrow_translated_in_same_request(self):
        self.plugin.context = _FakeContext(
            [
                "<title>三维 67P</title><explanation>说明译文。</explanation>"
                "<tomorrow>天空之眼</tomorrow>"
            ]
        )
        title, expl, tomorrow = asyncio.run(
            self.plugin._translate_uncached(
                "3D 67P", "English text.", "prov", "eye in the sky"
            )
        )
        self.assertEqual((title, expl, tomorrow), ("三维 67P", "说明译文。", "天空之眼"))
        self.assertEqual(len(self.plugin.context.calls), 1)
        self.assertIn(
            "<tomorrow>eye in the sky</tomorrow>",
            self.plugin.context.calls[0]["prompt"],
        )


class TranslationFieldFlowTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()
        self.store = {}

        async def get_cache(key):
            return self.store.get(key)

        async def put_cache(key, value):
            self.store[key] = value

        async def delete_cache(key):
            self.store.pop(key, None)

        async def resolve(umo=None):
            return "prov"

        self.plugin.get_cache = get_cache
        self.plugin.put_cache = put_cache
        self.plugin.delete_cache = delete_cache
        self.plugin._resolve_chat_provider_id = resolve
        self.plugin.context = _FakeContext(
            ["<title>三维 67P</title><explanation>说明译文。</explanation>"]
        )

    def test_translate_fields_uses_one_request_and_caches_per_field(self):
        title, expl, _ = asyncio.run(
            self.plugin._translate_fields(
                {"title": "3D 67P", "explanation": "English text."}
            )
        )
        self.assertEqual((title, expl), ("三维 67P", "说明译文。"))
        self.assertEqual(len(self.plugin.context.calls), 1)
        key_title = self.plugin._build_translation_cache_key("prov", "3D 67P")
        key_expl = self.plugin._build_translation_cache_key("prov", "English text.")
        self.assertEqual(self.store[key_title], "三维 67P")
        self.assertEqual(self.store[key_expl], "说明译文。")

    def test_translate_fields_caches_tomorrow(self):
        self.plugin.context = _FakeContext(
            [
                "<title>三维 67P</title><explanation>说明译文。</explanation>"
                "<tomorrow>天空之眼</tomorrow>"
            ]
        )
        payload = {
            "title": "3D 67P",
            "explanation": "English text.",
            "tomorrow_title": "eye in the sky",
        }
        title, expl, tomorrow = asyncio.run(self.plugin._translate_fields(payload))
        self.assertEqual((title, expl, tomorrow), ("三维 67P", "说明译文。", "天空之眼"))
        key = self.plugin._build_translation_cache_key("prov", "eye in the sky")
        self.assertEqual(self.store[key], "天空之眼")
        asyncio.run(self.plugin._translate_fields(payload))
        self.assertEqual(len(self.plugin.context.calls), 1)

    def test_second_call_hits_field_cache(self):
        payload = {"title": "3D 67P", "explanation": "English text."}
        asyncio.run(self.plugin._translate_fields(payload))
        asyncio.run(self.plugin._translate_fields(payload))
        self.assertEqual(len(self.plugin.context.calls), 1)

    def test_cache_translation_skips_stale_epoch(self):
        plugin = self.plugin
        plugin._cache_epoch = 5
        item = plugin._build_translation_cache_key("prov", "3D 67P")
        asyncio.run(plugin._cache_translation(item, "old", epoch=4))
        self.assertIsNone(self.store.get(item))
        asyncio.run(plugin._cache_translation(item, "new", epoch=5))
        self.assertEqual(self.store.get(item), "new")

    def test_only_title_is_requested_when_explanation_not_sent(self):
        self.plugin.send_explanation = False
        title, expl, _ = asyncio.run(
            self.plugin._translate_fields(
                {"title": "3D 67P", "explanation": "English text."}
            )
        )
        self.assertEqual(title, "三维 67P")
        self.assertIsNone(expl)
        prompt = self.plugin.context.calls[0]["system_prompt"]
        self.assertNotIn("<explanation>", prompt)


    def test_clear_bumps_epoch_without_cancelling_flights(self):
        import astrbot.core as core

        plugin = _make_plugin()

        async def fake_delete(key):
            return None

        plugin.delete_cache = fake_delete
        core.sp.prefs = []

        async def scenario():
            async def slow():
                await asyncio.sleep(0.05)
                return "x"

            task = asyncio.create_task(slow())
            plugin._translation_flights["k"] = task
            before = plugin._cache_epoch
            await plugin._clear_reusable_caches()
            return task.cancelled(), before, plugin._cache_epoch

        cancelled, before, after = asyncio.run(scenario())
        self.assertFalse(cancelled)
        self.assertEqual(after, before + 1)


class ConfigMigrationTests(unittest.TestCase):
    def _plugin_with_cache(self, config):
        plugin = _make_plugin(config)
        store = {}

        async def get_cache(key):
            return store.get(key)

        async def put_cache(key, value):
            store[key] = value

        async def put_kv_data(key, value):
            store[key] = value

        plugin.get_cache = get_cache
        plugin.put_cache = put_cache
        plugin.put_kv_data = put_kv_data
        return plugin, store

    def test_migrate_old_flat_config(self):
        config = _FakeConfig(
            {
                "image": False,
                "title": {"is_show": False, "is_translate": False},
                "explanation": {"is_show": False, "is_translate": False},
                "date": {"is_show": False},
                "provider": "liming/claude-opus-4-6",
                "push": {"send_image": False, "translate": False, "sessions": []},
            }
        )
        plugin, store = self._plugin_with_cache(config)
        asyncio.run(plugin._migrate_config())
        self.assertEqual(config["send"]["media"], False)
        self.assertEqual(config["send"]["title"], False)
        self.assertEqual(config["send"]["explanation"], False)
        self.assertEqual(config["send"]["date"], False)
        self.assertEqual(config["translate"]["title"], False)
        self.assertEqual(config["translate"]["explanation"], False)
        self.assertEqual(config["translate"]["provider"], "liming/claude-opus-4-6")
        self.assertTrue(store.get("config_layout_v2"))
        self.assertEqual(config.saved, 1)
        # 旧键已清除，避免标记丢失时再次覆盖新设置
        for key in ("image", "title", "explanation", "date", "provider"):
            self.assertNotIn(key, config)

    def test_new_config_not_clobbered_by_legacy_defaults(self):
        config = _FakeConfig(
            {
                "send": {
                    "media": False,
                    "title": False,
                    "explanation": False,
                    "date": False,
                },
                "translate": {
                    "title": False,
                    "explanation": False,
                    "provider": "keep/this",
                },
                "image": True,
                "title": {"is_show": True, "is_translate": True},
                "explanation": {"is_show": True, "is_translate": True},
                "date": {"is_show": True},
                "provider": "",
            }
        )
        plugin, _ = self._plugin_with_cache(config)
        asyncio.run(plugin._migrate_config())
        self.assertEqual(config["send"]["media"], False)
        self.assertEqual(config["send"]["title"], False)
        self.assertEqual(config["translate"]["title"], False)
        self.assertEqual(config["translate"]["provider"], "keep/this")

    def test_migration_is_idempotent(self):
        config = _FakeConfig({"send": {"media": False}, "translate": {"title": False}})
        plugin, store = self._plugin_with_cache(config)
        store["config_layout_v2"] = True
        asyncio.run(plugin._migrate_config())
        self.assertEqual(config.saved, 0)
        self.assertEqual(config["send"]["media"], False)

    def test_second_run_keeps_new_values(self):
        config = _FakeConfig({"image": False, "send": {"media": True}})
        plugin, _ = self._plugin_with_cache(config)
        asyncio.run(plugin._migrate_config())
        self.assertEqual(config["send"]["media"], False)
        # 用户在新分组里改回 true，再次加载不得被旧键覆盖。
        config["send"]["media"] = True
        asyncio.run(plugin._migrate_config())
        self.assertEqual(config["send"]["media"], True)

    def test_legacy_push_media_disabled_is_preserved(self):
        config = _FakeConfig(
            {"send": {"media": True}, "image": True, "push": {"send_image": False}}
        )
        plugin, _ = self._plugin_with_cache(config)
        asyncio.run(plugin._migrate_config())
        self.assertEqual(config["send"]["media"], False)

    def test_legacy_push_translate_disabled_is_preserved(self):
        config = _FakeConfig(
            {
                "translate": {"title": True, "explanation": True},
                "title": {"is_show": True, "is_translate": True},
                "explanation": {"is_show": True, "is_translate": True},
                "push": {"translate": False},
            }
        )
        plugin, _ = self._plugin_with_cache(config)
        asyncio.run(plugin._migrate_config())
        self.assertEqual(config["translate"]["title"], False)
        self.assertEqual(config["translate"]["explanation"], False)

    def test_migration_marker_only_after_save(self):
        class FailingConfig(dict):
            def __init__(self):
                super().__init__({"image": False})

            async def save_config_async(self):
                return False

        config = FailingConfig()
        plugin, store = self._plugin_with_cache(config)
        asyncio.run(plugin._migrate_config())
        self.assertFalse(store.get("config_layout_v2"))

    def test_migration_marker_write_failure_is_visible(self):
        config = _FakeConfig({"image": False})
        plugin, store = self._plugin_with_cache(config)

        async def failing_put(key, value):
            raise RuntimeError("kv down")

        plugin.put_kv_data = failing_put
        asyncio.run(plugin._migrate_config())
        self.assertFalse(store.get("config_layout_v2"))

    def test_cache_limit_migrates_from_legacy(self):
        config = _FakeConfig(
            {"apod_cache_limit": 1000, "translate_cache_limit": 250}
        )
        plugin, store = self._plugin_with_cache(config)
        asyncio.run(plugin._migrate_cache_limit())
        self.assertEqual(config["apod_cache_limit"], 250)
        self.assertTrue(store.get("config_layout_v3"))

    def test_cache_limit_migration_is_idempotent(self):
        config = _FakeConfig(
            {"apod_cache_limit": 1000, "translate_cache_limit": 250}
        )
        plugin, store = self._plugin_with_cache(config)
        store["config_layout_v3"] = True
        asyncio.run(plugin._migrate_cache_limit())
        self.assertEqual(config["apod_cache_limit"], 1000)


class ClearCacheTests(unittest.TestCase):
    def test_clear_keeps_push_state(self):
        import astrbot.core as core

        plugin = _make_plugin()
        removed = []

        async def fake_delete(key):
            removed.append(key)

        plugin.delete_cache = fake_delete
        core.sp.prefs = [
            SimpleNamespace(key="translate_cache:aaa"),
            SimpleNamespace(key="translate_cache_index"),
            SimpleNamespace(key="apod_cache"),
            SimpleNamespace(key="nasa_cache"),
            SimpleNamespace(key="last_pushed_apod"),
            SimpleNamespace(key="pushed_by_session"),
        ]
        plugin._stale_apod = {"x": 1}
        plugin._stale_nasa = {"y": 1}
        plugin._refresh_cooldowns["apod_cache"] = object()

        count = asyncio.run(plugin._clear_reusable_caches())

        self.assertEqual(count, 4)
        self.assertIn("translate_cache:aaa", removed)
        self.assertIn("translate_cache_index", removed)
        self.assertIn("apod_cache", removed)
        self.assertIn("nasa_cache", removed)
        self.assertNotIn("last_pushed_apod", removed)
        self.assertNotIn("pushed_by_session", removed)
        self.assertIsNone(plugin._stale_apod)
        self.assertIsNone(plugin._stale_nasa)
        self.assertEqual(plugin._refresh_cooldowns, {})


class ApodCachePolicyTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    def test_valid_until_next_publish_plus_grace(self):
        from datetime import datetime

        et = self.plugin._et_timezone()
        # 美东 10-17 12:00 抓取 → 10-18 00:05 + 10min == 10-18 00:15 ET
        valid = self.plugin._apod_cache_valid_until(
            datetime(2026, 10, 17, 12, 0, tzinfo=et)
        )
        self.assertEqual(valid, datetime(2026, 10, 18, 0, 15, tzinfo=et))

    def test_valid_until_same_day_when_before_publish(self):
        from datetime import datetime

        et = self.plugin._et_timezone()
        valid = self.plugin._apod_cache_valid_until(
            datetime(2026, 10, 17, 23, 50, tzinfo=et)
        )
        self.assertEqual(valid, datetime(2026, 10, 18, 0, 15, tzinfo=et))

    def test_fetch_inside_grace_window_expires_same_day(self):
        from datetime import datetime

        et = self.plugin._et_timezone()
        # 00:08（宽限窗口内）抓取 → 只算到当天 00:15，不会被推到明天
        valid = self.plugin._apod_cache_valid_until(
            datetime(2026, 10, 18, 0, 8, tzinfo=et)
        )
        self.assertEqual(valid, datetime(2026, 10, 18, 0, 15, tzinfo=et))

    def test_stale_dated_entry_is_short_lived(self):
        from datetime import datetime, timedelta, timezone

        et = self.plugin._et_timezone()
        # 用 -2 天，避免美东 00:00-00:05（昨日仍算最新）导致测试定时失败。
        stale_date = (datetime.now(et) - timedelta(days=2)).strftime("%Y-%m-%d")
        data = {
            "retrieved_at": (
                datetime.now(timezone.utc) - timedelta(minutes=10)
            ).isoformat(),
            "date": stale_date,
            "explanation": "x",
        }
        self.assertFalse(self.plugin._apod_cache_fresh(data))

    def test_stale_dated_but_just_fetched_is_fresh(self):
        from datetime import datetime, timedelta, timezone

        et = self.plugin._et_timezone()
        stale_date = (datetime.now(et) - timedelta(days=2)).strftime("%Y-%m-%d")
        data = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "date": stale_date,
            "explanation": "x",
        }
        # 刚抓到的旧一期只给短重试窗口，不直接判死。
        self.assertTrue(self.plugin._apod_cache_fresh(data))

    def test_just_fetched_is_fresh(self):
        from datetime import datetime, timezone

        data = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "date": "2026-10-17",
            "explanation": "x",
        }
        self.assertTrue(self.plugin._apod_cache_fresh(data))

    def test_two_days_old_is_stale(self):
        from datetime import datetime, timedelta, timezone

        data = {
            "retrieved_at": (
                datetime.now(timezone.utc) - timedelta(days=2)
            ).isoformat(),
            "date": "2026-10-16",
            "explanation": "x",
        }
        self.assertFalse(self.plugin._apod_cache_fresh(data))

    def test_missing_timestamp_is_stale(self):
        self.assertFalse(self.plugin._apod_cache_fresh({"date": "2026-10-17"}))

    def test_seconds_until_prefetch_within_a_day(self):
        seconds = self.plugin._seconds_until_apod_prefetch()
        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, 24 * 3600)


class ReplyPathVariableTests(unittest.TestCase):
    """验证指令传输与引用；媒体拆分矩阵由 RenderingTests 覆盖。"""

    def setUp(self):
        self.plugin = _make_plugin()
        self.event = _make_reply_event()

        self.plugin._translate_fields = AsyncMock(return_value=(None, None, None))

    def _reply(self, article, *, is_divided):
        self.plugin.is_divided = is_divided
        data = self.plugin._build_apod_data("APOD: t", LINK, article)
        asyncio.run(self.plugin._reply_apod(self.event, data))
        return self.event.sent

    def test_quote_reply_disabled_by_default(self):
        # 走真实配置初始化，不能让夹具写死的 False 掩盖默认值回归。
        self.plugin.config["cache_images"] = False
        with patch.object(self.plugin, "_migrate_config", new=AsyncMock()), \
             patch.object(self.plugin, "_migrate_cache_limit", new=AsyncMock()), \
             patch.object(self.plugin, "get_cache", new=AsyncMock(return_value=None)):
            asyncio.run(self.plugin.initialize())
        results = self._reply(IMAGE_ARTICLE, is_divided=True)
        self.assertFalse(any(
            isinstance(component, _COMP.Reply)
            for chain in results
            for component in chain.chain
        ))

    def test_quote_reply_only_marks_first_chain(self):
        self.plugin.quote_reply = True
        results = self._reply(IMAGE_ARTICLE, is_divided=True)
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0].chain[0], _COMP.Reply)
        self.assertEqual(results[0].chain[0].id, "msg-1")
        self.assertNotIsInstance(results[1].chain[0], _COMP.Reply)

    def test_failed_media_falls_back_to_quoted_text_and_stops_event(self):
        self.plugin.quote_reply = True
        self.event.send = AsyncMock(side_effect=[_pre_submit_error(), None])
        self._reply(IMAGE_ARTICLE, is_divided=False)
        calls = self.event.send.await_args_list
        self.assertEqual([
            [type(item).__name__ for item in call.args[0].chain] for call in calls
        ], [["Reply", "Image", "Plain"], ["Reply", "Plain"]])
        self.assertTrue(self.event.stopped)

    def test_missing_message_id_does_not_add_quote(self):
        self.plugin.quote_reply = True
        self.event.message_obj.message_id = ""
        results = self._reply(IMAGE_ARTICLE, is_divided=False)
        self.assertIsInstance(results[0].chain[0], _COMP.Image)

    def test_tool_delivery_uses_active_transport_and_reports_generic_failure(self):
        data = self.plugin._build_apod_data("APOD: t", LINK, IMAGE_ARTICLE)
        send = AsyncMock(return_value=None)
        self.plugin.context = SimpleNamespace(send_message=send)
        result = asyncio.run(self.plugin._deliver_apod(self.event, data, "今日APOD"))
        self.assertIn("已向当前会话发送", result)
        self.assertEqual([c.args[0] for c in send.await_args_list], ["umo", "umo"])
        self.assertFalse(self.event.sent)
        send.side_effect = _pre_submit_error()
        result = asyncio.run(self.plugin._deliver_apod(self.event, data, "今日APOD"))
        self.assertIn("发送失败", result)
        self.assertNotIn("未找到可用的消息平台", result)


class NasaArticleParsingTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    @staticmethod
    def _page(body, *, description="", date="2026-10-30T15:56:26Z"):
        head = (
            "<html><head>"
            '<meta property="og:image" '
            'content="https://www.nasa.gov/wp-content/uploads/2026/10/x.png"/>'
            f'<meta property="parsely-pub-date" content="{date}"/>'
        )
        if description:
            head += f'<meta property="og:description" content="{description}"/>'
        return f"{head}</head><body><article><h1>Test Title</h1>{body}</article></body></html>"

    def test_path_tag_does_not_swallow_body(self):
        # <p[^>]*> 会把 <path .../> 当段落开头，吞掉后面的正文并因
        # “Image Article” 前缀被过滤；收紧标签名后必须拿到真正文。
        page = self._page(
            '<p class="label">1 min read</p>'
            '<p class="article-author-name">HQ Web Team</p>'
            '<div class="icon"><svg><path d="M1 1"/></svg>'
            '<span>Image Article</span></div>'
            '<p class="wp-block-paragraph">Real body text.</p>'
            '<p><em>Image credit: NASA</em></p>'
        )
        data = self.plugin._build_nasa_data(page, LINK)
        self.assertIsNotNone(data)
        self.assertEqual(data["explanation"], "Real body text.")
        self.assertEqual(data["credit"], "NASA")

    def test_byline_classes_are_skipped(self):
        page = self._page(
            '<p class="article-author-name">HQ Web Team</p>'
            '<p class="article-author-title">Goddard Space Flight Center</p>'
            '<p class="wp-block-paragraph">Body.</p>'
        )
        data = self.plugin._build_nasa_data(page, LINK)
        self.assertEqual(data["explanation"], "Body.")

    def test_photo_credit_is_extracted(self):
        page = self._page(
            '<p class="wp-block-paragraph">Body.</p>'
            '<p>Photo credit: NASA/Someone</p>'
        )
        data = self.plugin._build_nasa_data(page, LINK)
        self.assertEqual(data["explanation"], "Body.")
        self.assertEqual(data["credit"], "NASA/Someone")

    def test_og_description_fallback(self):
        page = self._page(
            '<p class="article-author-name">HQ Web Team</p>',
            description="Fallback summary.",
        )
        data = self.plugin._build_nasa_data(page, LINK)
        self.assertEqual(data["explanation"], "Fallback summary.")

    def test_empty_body_without_summary_is_invalid(self):
        page = self._page('<p class="label">1 min read</p>')
        self.assertIsNone(self.plugin._build_nasa_data(page, LINK))


class NasaProbeTests(unittest.TestCase):
    def test_extract_latest_link_after_h1(self):
        page = (
            '<h1>Image of the Day</h1>'
            '<a href="https://www.nasa.gov/image-article/first/">x</a>'
        )
        self.assertEqual(
            apod_main.APOD._extract_nasa_latest_link(page),
            "https://www.nasa.gov/image-article/first/",
        )

    def test_extract_returns_none_without_link(self):
        self.assertIsNone(apod_main.APOD._extract_nasa_latest_link("<h1>x</h1>"))


    def test_concurrent_probes_are_coalesced(self):
        plugin = _make_plugin()
        calls = {"n": 0}

        async def fake_probe():
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return "https://www.nasa.gov/image-article/abc/"

        plugin._probe_nasa_latest_link = fake_probe

        async def scenario():
            return await asyncio.gather(
                *[plugin._coalesced_nasa_probe() for _ in range(5)]
            )

        results = asyncio.run(scenario())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(
            set(results), {"https://www.nasa.gov/image-article/abc/"}
        )


class NasaProbeTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    @staticmethod
    def _patch_session(status, body=""):
        import unittest.mock as mock

        class _Resp:
            def __init__(self):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def text(self):
                return body

        class _Session:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url):
                return _Resp()

        return mock.patch.object(apod_main.aiohttp, "ClientSession", _Session)

    def test_probe_returns_link_on_200(self):
        body = (
            '<h1>Image of the Day</h1>'
            '<a href="https://www.nasa.gov/image-article/abc/">x</a>'
        )
        with self._patch_session(200, body):
            link = asyncio.run(self.plugin._probe_nasa_latest_link())
        self.assertEqual(link, "https://www.nasa.gov/image-article/abc/")

    def test_probe_failure_returns_none(self):
        with self._patch_session(503):
            self.assertIsNone(asyncio.run(self.plugin._probe_nasa_latest_link()))


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()
        self.plugin.cache_images = True
        self.plugin.cache_limit = 2
        self.store = {}

        async def get_cache(key):
            return self.store.get(key)

        async def put_cache(key, value):
            self.store[key] = value

        self.plugin.get_cache = get_cache
        self.plugin.put_cache = put_cache

    def test_path_is_stable_and_keeps_suffix(self):
        p1 = self.plugin._image_cache_path("https://x/a.jpg?tok=1")
        p2 = self.plugin._image_cache_path("https://x/a.jpg?tok=1")
        self.assertEqual(p1, p2)
        self.assertTrue(p1.endswith(".jpg"))

    def test_localize_uses_existing_file_without_network(self):
        url = "https://x/pre.jpg"
        path = self.plugin._image_cache_path(url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"img")
        got = asyncio.run(self.plugin._localize_image(url))
        self.assertEqual(got, path)
        self.assertEqual(
            self.store.get("image_cache_index"), [os.path.basename(path)]
        )

    def test_touch_evicts_oldest(self):
        directory = self.plugin._image_cache_dir()
        os.makedirs(directory, exist_ok=True)
        old = time.time() - 1000
        paths = []
        for i in range(3):
            path = os.path.join(directory, f"f{i}.jpg")
            with open(path, "wb") as handle:
                handle.write(b"x")
            if i < 2:
                os.utime(path, (old, old))
            paths.append(path)
            asyncio.run(self.plugin._touch_image_cache(path))
        self.assertFalse(os.path.exists(paths[0]))
        self.assertTrue(os.path.exists(paths[2]))

    def test_touch_keeps_fresh_files(self):
        directory = self.plugin._image_cache_dir()
        os.makedirs(directory, exist_ok=True)
        paths = []
        for i in range(3):
            path = os.path.join(directory, f"n{i}.jpg")
            with open(path, "wb") as handle:
                handle.write(b"x")
            paths.append(path)
            asyncio.run(self.plugin._touch_image_cache(path))
        # 刚写入的文件可能还被待发送消息引用，不应被淘汰
        for path in paths:
            self.assertTrue(os.path.exists(path))

    def test_trim_keeps_fresh_but_reports_over(self):
        self.plugin.cache_limit = 1
        directory = self.plugin._image_cache_dir()
        os.makedirs(directory, exist_ok=True)
        paths = []
        for i in range(2):
            path = os.path.join(directory, f"g{i}.jpg")
            with open(path, "wb") as handle:
                handle.write(b"x")
            paths.append(path)
        names = [os.path.basename(p) for p in paths]
        self.assertTrue(self.plugin._trim_image_index(names))
        for path in paths:
            self.assertTrue(os.path.exists(path))

    def test_trim_reconciles_once_old_enough(self):
        self.plugin.cache_limit = 1
        directory = self.plugin._image_cache_dir()
        os.makedirs(directory, exist_ok=True)
        old = time.time() - 1000
        paths = []
        for i in range(2):
            path = os.path.join(directory, f"h{i}.jpg")
            with open(path, "wb") as handle:
                handle.write(b"x")
            os.utime(path, (old, old))
            paths.append(path)
        names = [os.path.basename(p) for p in paths]
        self.assertFalse(self.plugin._trim_image_index(names))
        self.assertEqual(len(names), 1)
        self.assertFalse(os.path.exists(paths[0]))

    def test_reconcile_reschedules_when_still_over(self):
        import unittest.mock as mock

        calls = {"n": 0}

        def fake_trim(names):
            calls["n"] += 1
            return calls["n"] == 1  # 第一轮仍超额，第二轮收敛

        self.plugin._trim_image_index = fake_trim

        async def scenario():
            self.plugin._schedule_image_reconcile()
            first = self.plugin._image_reconcile_task
            if first:
                await first
            second = self.plugin._image_reconcile_task
            if second and second is not first:
                await second

        with mock.patch.object(apod_main, "IMAGE_CACHE_MIN_AGE_SECONDS", -1):
            asyncio.run(scenario())
        self.assertGreaterEqual(calls["n"], 2)

    def test_localize_drops_file_when_cleared_during_download(self):
        import unittest.mock as mock

        plugin = self.plugin
        url = "https://x/new.jpg"
        path = plugin._image_cache_path(url)
        plugin._cache_epoch = 0

        async def fake_download(source, dest):
            # 模拟下载期间发生了清理
            plugin._cache_epoch += 1
            with open(dest, "wb") as handle:
                handle.write(b"data")

        with mock.patch.object(plugin, "_download_image", fake_download):
            got = asyncio.run(plugin._localize_image(url))
        self.assertIsNone(got)
        self.assertFalse(os.path.exists(path))

    def test_cancel_during_download_removes_partial_file(self):
        import unittest.mock as mock

        plugin = self.plugin
        url = "https://x/cancel.jpg"
        path = plugin._image_cache_path(url)
        directory = plugin._image_cache_dir()

        async def fake_download(source, dest):
            with open(dest, "wb") as handle:
                handle.write(b"data")
            raise asyncio.CancelledError()

        with mock.patch.object(plugin, "_download_image", fake_download):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(plugin._localize_image(url))
        prefix = os.path.basename(path) + "."
        leftovers = [
            name
            for name in os.listdir(directory)
            if name.startswith(prefix) and name.endswith(".part")
        ]
        self.assertEqual(leftovers, [])

    def test_localize_refreshes_mtime_on_hit(self):
        url = "https://x/hit.jpg"
        path = self.plugin._image_cache_path(url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x")
        old = time.time() - 5000
        os.utime(path, (old, old))
        asyncio.run(self.plugin._localize_image(url))
        self.assertGreater(os.path.getmtime(path), old + 1000)

    def test_stale_download_does_not_clobber_new_file(self):
        import unittest.mock as mock

        plugin = self.plugin
        url = "https://x/clash.jpg"
        path = plugin._image_cache_path(url)
        plugin._cache_epoch = 0

        async def fake_download(source, dest):
            # 模拟：下载期间发生清理，且新请求写出了有效文件
            plugin._cache_epoch += 1
            with open(path, "wb") as handle:
                handle.write(b"new")
            with open(dest, "wb") as handle:
                handle.write(b"old")

        with mock.patch.object(plugin, "_download_image", fake_download):
            got = asyncio.run(plugin._localize_image(url))
        self.assertIsNone(got)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"new")

    def test_localize_disabled_returns_none(self):
        self.plugin.cache_images = False
        self.assertIsNone(
            asyncio.run(self.plugin._localize_image("https://x/a.jpg"))
        )


class MediaDownloadRetryTests(unittest.TestCase):
    """图片下载边界：走 AstrBot download_file，按 retry_count 退避重试。"""

    def setUp(self):
        import astrbot.core.utils.io as io_utils

        self.io_utils = io_utils
        self.plugin = _make_plugin()
        self.plugin.cache_images = True
        self.plugin.retry_count = 3
        self.url = "https://x/retry.jpg"
        self.store = {}
        # 每个用例独立缓存目录，退出时删除：上一用例写过的文件会让下载直接命中缓存。
        _use_isolated_plugin_data_dir(self)

        async def get_cache(key):
            return self.store.get(key)

        async def put_cache(key, value):
            self.store[key] = value

        self.plugin.get_cache = get_cache
        self.plugin.put_cache = put_cache

    def _localize(self, download, sleeps):
        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch.object(self.io_utils, "download_file", download), \
             patch.object(apod_main.asyncio, "sleep", fake_sleep):
            return asyncio.run(self.plugin._localize_image(self.url))

    def test_transient_failure_retries_with_backoff(self):
        calls, sleeps = [], []

        async def flaky(url, dest, *args, **kwargs):
            calls.append(url)
            if len(calls) < 3:
                raise ConnectionResetError("reset")
            with open(dest, "wb") as handle:
                handle.write(b"img")

        path = self._localize(flaky, sleeps)
        self.assertEqual(path, self.plugin._image_cache_path(self.url))
        self.assertEqual(len(calls), 3)
        # 退避间隔照 _fetch_text：min(2**attempt, 4) 再加抖动。
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(1 <= sleeps[0] <= 2, sleeps)
        self.assertTrue(2 <= sleeps[1] <= 3, sleeps)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"img")

    def test_client_error_is_not_retried(self):
        calls, sleeps = [], []

        async def missing(url, dest, *args, **kwargs):
            calls.append(url)
            raise self.io_utils.DownloadFileHTTPError(
                "Failed to download file. HTTP status code: 404"
            )

        self.assertIsNone(self._localize(missing, sleeps))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_server_error_exhausts_retry_count(self):
        calls, sleeps = [], []

        async def down(url, dest, *args, **kwargs):
            calls.append(url)
            raise self.io_utils.DownloadFileHTTPError(
                "Failed to download file. HTTP status code: 503"
            )

        self.assertIsNone(self._localize(down, sleeps))
        self.assertEqual(len(calls), self.plugin.retry_count + 1)
        self.assertEqual(len(sleeps), self.plugin.retry_count)
        self.assertFalse(os.path.exists(self.plugin._image_cache_path(self.url)))

    def test_rate_limited_status_is_retried(self):
        calls, sleeps = [], []
        self.plugin.retry_count = 1

        async def limited(url, dest, *args, **kwargs):
            calls.append(url)
            raise self.io_utils.DownloadFileHTTPError(
                "Failed to download file. HTTP status code: 429"
            )

        self.assertIsNone(self._localize(limited, sleeps))
        self.assertEqual(len(calls), 2)

    def test_empty_response_is_treated_as_failure(self):
        calls, sleeps = [], []
        self.plugin.retry_count = 1

        async def empty(url, dest, *args, **kwargs):
            calls.append(url)
            with open(dest, "wb"):
                pass

        self.assertIsNone(self._localize(empty, sleeps))
        self.assertEqual(len(calls), 2)

    def test_insecure_ssl_fallback_is_disabled(self):
        calls, sleeps = [], []

        async def recording(url, dest, *args, **kwargs):
            calls.append(kwargs)
            with open(dest, "wb") as handle:
                handle.write(b"img")

        self._localize(recording, sleeps)
        # AstrBot 默认会在证书失败后改走 CERT_NONE 且不跟随代理的裸会话。
        self.assertEqual(calls, [{"allow_insecure_ssl_fallback": False}])

    def test_failure_logs_do_not_leak_urls(self):
        secret = "https://assets.science.nasa.gov/a.jpg?token=SECRET123"
        calls, sleeps = [], []

        async def leaky(url, dest, *args, **kwargs):
            calls.append(url)
            raise RuntimeError(f"cannot fetch {secret}")

        with self.assertLogs("apod-test", level="WARNING") as logs:
            self.assertIsNone(self._localize(leaky, sleeps))
        text = "\n".join(logs.output)
        self.assertNotIn("SECRET123", text)
        self.assertNotIn(secret, text)
        self.assertIn("<url>", text)
        self.assertIn("RuntimeError", text)

    def test_redaction_covers_paths_with_brackets(self):
        exc = RuntimeError(
            "see https://assets.science.nasa.gov/a(b).jpg?sig=SECRET456 now"
        )
        text = apod_main._safe_exc_text(exc)
        # 路径里的括号不能让脱敏在 ?sig= 之前收尾。
        self.assertNotIn("SECRET456", text)
        self.assertIn("<url>", text)

    def test_redaction_covers_quotes_and_wrapping_punctuation(self):
        # 单引号、括号都是合法的 URL 字符（RFC 3986），不能在它们处收尾。
        cases = [
            "https://cdn.example.invalid/today's.png?sig=TEST_SIGNATURE",
            "URL('https://cdn.example.invalid/a.jpg?sig=S2')",
            "(see https://cdn.example.invalid/b.jpg?sig=S3)",
            "https://cdn.example.invalid/c.png?sig=S4.",
            "失败：https://cdn.example.invalid/d.png?sig=S5",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                text = apod_main._safe_exc_text(RuntimeError(raw))
                self.assertNotIn("sig=", text)
                self.assertIn("<url>", text)

    def test_send_logs_do_not_leak_urls(self):
        # 用带单引号的路径走真实发送失败链路（评审给出的原例）。
        secret = "https://cdn.example.invalid/today's.png?sig=TEST_SIGNATURE"
        chain = apod_main.MessageChain(chain=[_COMP.Plain("body")])

        async def leaky_send(_chain):
            # 提交后断连：异常文本里带着签名 URL。
            raise RuntimeError(f"response payload error for {secret}")

        with self.assertLogs("apod-test", level="ERROR") as logs:
            report = asyncio.run(self.plugin._send_chains([chain], leaky_send))
        self.assertTrue(report.text_unknown)
        text = chr(10).join(logs.output)
        self.assertNotIn("TEST_SIGNATURE", text)
        self.assertNotIn("today's.png", text)
        self.assertIn("<url>", text)

    def test_shared_retry_helpers(self):
        self.assertEqual(
            [apod_main._retry_delay(attempt) for attempt in range(4)], [1, 2, 4, 4]
        )
        self.assertTrue(4 <= apod_main._retry_delay(5, jitter=True) <= 5)
        fatal = apod_main._is_fatal_client_status
        self.assertTrue(fatal(400) and fatal(404) and fatal(499))
        self.assertFalse(fatal(408) or fatal(429) or fatal(200) or fatal(500))


class PushMediaPrewarmTests(unittest.TestCase):
    """定时推送的媒体预热：图文都发、丢图只补图，汇总如实统计。"""

    def setUp(self):
        self.plugin = _make_plugin(_FakeConfig({"push": {"sessions": ["a"]}}))
        self.plugin.push_mode = "scheduled"
        self.plugin.is_divided = False  # 合并链：图片 + 正文一条
        self.plugin.cache_images = True
        # 每个用例独立缓存目录：图片命中缓存会跳过下载，不能跨用例共享。
        _use_isolated_plugin_data_dir(self)
        self.data = self.plugin._build_apod_data("APOD: t", LINK, IMAGE_ARTICLE)
        self.media_url = self.data["hdurl"] or self.data["url"]
        self.store = {}

        async def put(key, value):
            self.store[key] = value

        self.plugin.get_kv_data = AsyncMock(
            side_effect=lambda k, default=None: self.store.get(k, default)
        )
        self.plugin.put_kv_data = AsyncMock(side_effect=put)
        self.plugin._get_cached_data = AsyncMock(return_value=self.data)
        self.plugin._translate_fields = AsyncMock(return_value=(None, None, None))
        self.send = AsyncMock(return_value=None)
        self.plugin.context = SimpleNamespace(send_message=self.send)

    def _sent(self):
        return [
            [type(item).__name__ for item in call.args[1].chain]
            for call in self.send.await_args_list
        ]

    def test_download_failure_sends_text_only_and_keeps_media_pending(self):
        async def boom(url, dest):
            raise ConnectionResetError("reset")

        with patch.object(self.plugin, "_download_image", boom), \
             patch.object(apod_main.asyncio, "sleep", AsyncMock()):
            self.assertFalse(asyncio.run(self.plugin._push_check()))
        # 预热失败时不构造 Image，只发正文，不让平台二次下载后报错。
        self.assertEqual(self._sent(), [["Plain"]])
        self.assertEqual(
            self.store["pushed_by_session"],
            {"a": {"link": LINK, "media_sent": False}},
        )

    def test_fatal_download_error_is_not_retried_in_round(self):
        import astrbot.core.utils.io as io_utils

        calls = []

        async def not_found(url, dest):
            calls.append(url)
            raise io_utils.DownloadFileHTTPError(
                "Failed to download file. HTTP status code: 404"
            )

        with patch.object(self.plugin, "_download_image", not_found), \
             patch.object(apod_main.asyncio, "sleep", AsyncMock()):
            self.assertFalse(asyncio.run(self.plugin._push_check()))
        # 4xx 重试无意义：同一地址本轮只请求一次（旧行为会刷 1+retry_count 次）。
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._sent(), [["Plain"]])
        self.assertEqual(
            self.store["pushed_by_session"],
            {"a": {"link": LINK, "media_sent": False}},
        )

    def test_transient_download_error_uses_download_retries_only(self):
        calls = []

        async def reset(url, dest):
            calls.append(url)
            raise ConnectionResetError("reset")

        with patch.object(self.plugin, "_download_image", reset), \
             patch.object(apod_main.asyncio, "sleep", AsyncMock()):
            asyncio.run(self.plugin._push_check())
        # 暂时性失败由下载层自己重试 retry_count 次；轮内不再重复请求同一地址。
        self.assertEqual(len(calls), self.plugin.retry_count + 1)

    def test_timed_out_media_send_is_not_retried(self):
        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        sent = []

        async def timeout_on_media(umo, chain):
            kinds = [type(item).__name__ for item in chain.chain]
            sent.append(kinds)
            if "Image" in kinds:
                # 平台已收下图，只是回执没等到。
                raise asyncio.TimeoutError()
            return None

        self.plugin.context = SimpleNamespace(send_message=timeout_on_media)
        with patch.object(self.plugin, "_download_image", ok), \
             self.assertLogs("apod-test", level="WARNING") as logs:
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        # 超时属于“结果未知”：不重发，避免同一会话收到两张一样的图。
        self.assertEqual(sent, [["Image", "Plain"]])
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})
        self.assertTrue(any("结果未知" in line for line in logs.output))

    def test_post_submit_failure_is_not_retried(self):
        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        sent = []

        async def submitted_then_broken(umo, chain):
            kinds = [type(item).__name__ for item in chain.chain]
            sent.append(kinds)
            # 消息已经提交，读响应体时断连：不能当成“没发出去”。
            raise _post_submit_error()

        self.plugin.context = SimpleNamespace(send_message=submitted_then_broken)
        with patch.object(self.plugin, "_download_image", ok),              patch.object(apod_main.asyncio, "sleep", AsyncMock()),              self.assertLogs("apod-test", level="WARNING"):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        # 不重发：整期只尝试了一次，不再降级正文、也不补图。
        self.assertEqual(sent, [["Image", "Plain"]])
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})

    def test_unknown_media_does_not_swallow_text_retry(self):
        # 拆链：图片超时（未知）、正文明确失败 → 正文仍要进入下一轮补试。
        self.plugin.is_divided = True

        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        sent = []

        async def media_timeout_text_fails(umo, chain):
            kinds = [type(item).__name__ for item in chain.chain]
            sent.append(kinds)
            if kinds == ["Image"]:
                raise asyncio.TimeoutError()
            raise _pre_submit_error()

        self.plugin.context = SimpleNamespace(send_message=media_timeout_text_fails)
        logs = _capture_logs(self)
        with patch.object(self.plugin, "_download_image", ok):
            self.assertFalse(asyncio.run(self.plugin._push_check()))
        self.assertEqual(sent, [["Image"], ["Plain"]])
        # 图片按未知记账、正文仍然待补 → 检查点保留“只缺正文”。
        self.assertEqual(
            self.store["pushed_by_session"],
            {"a": {"link": LINK, "media_sent": True}},
        )
        summary = [line for line in logs.lines if "本次推送" in line]
        self.assertTrue(summary and "待补发" in summary[0], summary)

    def test_known_media_success_is_counted_when_text_is_unknown(self):
        # 拆链：图片成功、正文超时 → 图片仍应算作已送达（1/1）。
        self.plugin.is_divided = True

        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        async def media_ok_text_timeout(umo, chain):
            kinds = [type(item).__name__ for item in chain.chain]
            if kinds == ["Plain"]:
                raise asyncio.TimeoutError()
            return None

        self.plugin.context = SimpleNamespace(send_message=media_ok_text_timeout)
        with patch.object(self.plugin, "_download_image", ok),              self.assertLogs("apod-test", level="INFO") as logs:
            asyncio.run(self.plugin._push_check())
        summary = [line for line in logs.output if "本次推送" in line]
        self.assertEqual(len(summary), 1)
        self.assertIn("媒体 1/1", summary[0])
        self.assertIn("正文 0/1", summary[0])

    def test_missing_image_file_is_a_retryable_failure(self):
        # 组链后、适配器读取前文件被删：AstrBot 转换阶段抛 FileNotFoundError，
        # 说明还没上传，应判为可重试的明确失败而不是“结果未知”。
        async def vanish(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        sent = []
        path = self.plugin._image_cache_path(self.media_url)

        async def delete_then_raise(_umo, chain):
            kinds = [type(item).__name__ for item in chain.chain]
            sent.append(kinds)
            if "Image" in kinds and not sent.count(["Image"]):
                # 组链之后、适配器读取之前被淘汰：AstrBot 抛 FileNotFoundError。
                os.remove(path)
                raise FileNotFoundError("image vanished")
            return None

        self.plugin.context = SimpleNamespace(send_message=delete_then_raise)
        with patch.object(self.plugin, "_download_image", vanish),              patch.object(apod_main.asyncio, "sleep", AsyncMock()):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        # 首次图文失败 → 降级正文 → 轮内重新预热并补图。
        self.assertEqual(sent, [["Image", "Plain"], ["Plain"], ["Image"]])
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})

    def test_network_send_error_is_unknown_for_every_session(self):
        # 私聊的 POST 在适配器内部重试 3 次，群聊的 POST 也有 botpy 自己的重连：
        # 最后一次连接失败证明不了前面没提交，两类会话都只能按结果未知处理。
        self.plugin.config["push"]["sessions"] = [
            "qq_official:FriendMessage:abc",
            "qq_official:GroupMessage:g1",
        ]
        sent = []

        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        async def connect_error(umo, chain):
            sent.append((umo, [type(item).__name__ for item in chain.chain]))
            raise _network_error()

        self.plugin.context = SimpleNamespace(send_message=connect_error)
        with patch.object(self.plugin, "_download_image", ok),              patch.object(apod_main.asyncio, "sleep", AsyncMock()),              self.assertLogs("apod-test", level="WARNING"):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        # 每个会话只尝试一次，不再降级重发正文、也不补图。
        self.assertEqual(
            sent,
            [
                ("qq_official:FriendMessage:abc", ["Image", "Plain"]),
                ("qq_official:GroupMessage:g1", ["Image", "Plain"]),
            ],
        )
        self.assertEqual(
            self.store["pushed_by_session"],
            dict.fromkeys(
                ["qq_official:FriendMessage:abc", "qq_official:GroupMessage:g1"],
                LINK,
            ),
        )

    def test_summary_does_not_report_unknown_as_delivered(self):
        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        async def timeout_on_media(umo, chain):
            if "Image" in [type(item).__name__ for item in chain.chain]:
                raise asyncio.TimeoutError()
            return None

        self.plugin.context = SimpleNamespace(send_message=timeout_on_media)
        with patch.object(self.plugin, "_download_image", ok),              self.assertLogs("apod-test", level="INFO") as logs:
            asyncio.run(self.plugin._push_check())
        summary = [line for line in logs.output if "本次推送" in line]
        self.assertEqual(len(summary), 1)
        # 结果未知不能算成“已送达”。
        self.assertIn("正文 0/1、媒体 0/1", summary[0])
        self.assertIn("发送结果未知", summary[0])

    def test_tool_reports_unknown_delivery(self):
        # 关缓存避免工具路径真的去下载（本用例只关心“结果未知”怎么回报）。
        self.plugin.cache_images = False
        data = self.plugin._build_apod_data("APOD: t", LINK, IMAGE_ARTICLE)
        event = SimpleNamespace(unified_msg_origin="umo")

        async def timeout_send(_umo, _chain):
            raise asyncio.TimeoutError()

        self.plugin.context = SimpleNamespace(send_message=timeout_send)
        with self.assertLogs("apod-test", level="ERROR"):
            result = asyncio.run(self.plugin._deliver_apod(event, data, "今日APOD"))
        self.assertIn("结果未知", result)
        self.assertNotIn("已向当前会话发送", result)

    def test_media_pending_session_only_resends_media(self):
        self.store["pushed_by_session"] = {
            "a": {"link": LINK, "media_sent": False}
        }
        path = self.plugin._image_cache_path(self.media_url)

        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        with patch.object(self.plugin, "_download_image", ok):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        # 正文已送达：本轮只补媒体链，不重发正文。
        self.assertEqual(self._sent(), [["Image"]])
        self.assertEqual(
            self.send.await_args_list[0].args[1].chain[0].file, path
        )
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})

    def test_evicted_media_is_prewarmed_again_mid_round(self):
        # 群发期间共享文件被 FIFO 淘汰：后面的会话必须重新预热，不能只发正文。
        self.plugin.config["push"]["sessions"] = ["a", "b"]
        self.plugin.cache_limit = 1
        path = self.plugin._image_cache_path(self.media_url)
        sent, downloads = [], []

        async def counting_download(url, dest):
            downloads.append(url)
            with open(dest, "wb") as handle:
                handle.write(b"img")

        async def evict_after_first(umo, chain):
            kinds = [type(item).__name__ for item in chain.chain]
            sent.append((umo, kinds))
            if umo == "a" and "Image" in kinds:
                os.remove(path)  # 淘汰发生在两个会话之间
            return None

        self.plugin.context = SimpleNamespace(send_message=evict_after_first)
        with patch.object(self.plugin, "_download_image", counting_download):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        self.assertEqual(
            sent, [("a", ["Image", "Plain"]), ("b", ["Image", "Plain"])]
        )
        self.assertEqual(len(downloads), 2)
        self.assertEqual(self.store["pushed_by_session"], dict.fromkeys("ab", LINK))

    def test_media_downloaded_once_for_all_sessions(self):
        self.plugin.config["push"]["sessions"] = ["a", "b", "c"]
        calls = []

        async def ok(url, dest):
            calls.append(url)
            with open(dest, "wb") as handle:
                handle.write(b"img")

        with patch.object(self.plugin, "_download_image", ok):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._sent(), [["Image", "Plain"]] * 3)
        self.assertEqual(self.store["pushed_by_session"], dict.fromkeys("abc", LINK))

    def test_media_upload_failure_recovers_in_same_round(self):
        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        # 事故现场：合并链上传失败 → 降级正文；下一轮只补图，不重发正文。
        self.send.side_effect = [_pre_submit_error(), None, None]
        with patch.object(self.plugin, "_download_image", ok), \
             patch.object(apod_main.asyncio, "sleep", fake_sleep):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        self.assertEqual(
            self._sent(), [["Image", "Plain"], ["Plain"], ["Image"]]
        )
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})
        # 秒级补发：等待不能是外层那次 6 分钟的补试。
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(1 <= sleeps[0] <= 2, sleeps)

    def test_media_upload_failure_keeps_pending_after_fast_retries(self):
        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        async def fake_sleep(seconds):
            pass

        # 第一次成功（合并链上传失败、正文降级），之后每次只补图都失败。
        outcomes = [_pre_submit_error(), None] + [
            _pre_submit_error()
        ] * self.plugin.retry_count
        self.send.side_effect = outcomes
        with patch.object(self.plugin, "_download_image", ok), \
             patch.object(apod_main.asyncio, "sleep", fake_sleep):
            self.assertFalse(asyncio.run(self.plugin._push_check()))
        self.assertEqual(self._sent()[0], ["Image", "Plain"])
        self.assertEqual(self._sent()[1], ["Plain"])
        # 正文不重复发，只反复补图，直到用完 retry_count 轮。
        self.assertEqual(
            self._sent()[2:], [["Image"]] * self.plugin.retry_count
        )
        self.assertEqual(
            self.store["pushed_by_session"],
            {"a": {"link": LINK, "media_sent": False}},
        )

    def test_fast_retries_disabled_with_zero_retry_count(self):
        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        self.plugin.retry_count = 0
        self.send.side_effect = [_pre_submit_error(), None]
        with patch.object(self.plugin, "_download_image", ok):
            self.assertFalse(asyncio.run(self.plugin._push_check()))
        self.assertEqual(self._sent(), [["Image", "Plain"], ["Plain"]])
        self.assertEqual(
            self.store["pushed_by_session"],
            {"a": {"link": LINK, "media_sent": False}},
        )

    def test_download_failure_is_not_retried_in_round(self):
        self.plugin.config["push"]["sessions"] = ["a", "b", "c", "d"]
        attempts = []

        async def flaky(url, dest):
            attempts.append(url)
            if len(attempts) <= self.plugin.retry_count + 1:
                raise ConnectionResetError("reset")
            with open(dest, "wb") as handle:
                handle.write(b"img")

        with patch.object(self.plugin, "_download_image", flaky), \
             patch.object(apod_main.asyncio, "sleep", AsyncMock()):
            self.assertFalse(asyncio.run(self.plugin._push_check()))
        # 下载层已用完 retry_count 次，轮内不再叠加一轮下载（否则会有第 5 次请求）。
        self.assertEqual(len(attempts), self.plugin.retry_count + 1)
        self.assertEqual(self._sent(), [["Plain"]] * 4)
        self.assertEqual(
            self.store["pushed_by_session"],
            {umo: {"link": LINK, "media_sent": False} for umo in "abcd"},
        )

    def test_download_failure_is_recovered_for_all_sessions(self):
        self.plugin.config["push"]["sessions"] = ["a", "b", "c", "d"]

        async def boom(url, dest):
            raise ConnectionResetError("reset")

        with patch.object(self.plugin, "_download_image", boom), \
             patch.object(apod_main.asyncio, "sleep", AsyncMock()):
            self.assertFalse(asyncio.run(self.plugin._push_check()))
        # 下载全失败：四个会话都只收到正文，但都记下“媒体待补”。
        self.assertEqual(self._sent(), [["Plain"]] * 4)
        self.assertEqual(
            self.store["pushed_by_session"],
            {umo: {"link": LINK, "media_sent": False} for umo in "abcd"},
        )

        self.send.reset_mock()
        calls = []

        async def ok(url, dest):
            calls.append(url)
            with open(dest, "wb") as handle:
                handle.write(b"img")

        with patch.object(self.plugin, "_download_image", ok):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        # 下一轮：整期只下载一次，四个会话只补媒体链，不重发正文。
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._sent(), [["Image"]] * 4)
        self.assertEqual(
            self.store["pushed_by_session"], dict.fromkeys("abcd", LINK)
        )

    def test_text_only_issue_is_marked_delivered(self):
        self.plugin.send_media = False

        async def boom(url, dest):
            raise AssertionError("没有媒体就不该下载")

        with patch.object(self.plugin, "_download_image", boom):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        self.assertEqual(self._sent(), [["Plain"]])
        # 本来就没有媒体可发：不能写成 media_sent=False，否则会永远补媒体。
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})

    def test_disabling_media_converges_pending_media(self):
        self.store["pushed_by_session"] = {
            "a": {"link": LINK, "media_sent": False}
        }
        self.plugin.send_media = False
        with self.assertLogs("apod-test", level="WARNING") as logs:
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        # 没有可补的内容：收敛为整期送达，不能每轮空转。
        self.assertEqual(self.send.await_count, 0)
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})
        self.assertTrue(any("不发送媒体" in line for line in logs.output))

    def test_media_off_text_failure_keeps_no_checkpoint(self):
        self.plugin.send_media = False
        self.send.side_effect = _pre_submit_error()
        self.assertFalse(asyncio.run(self.plugin._push_check()))
        # 不能写 media_sent=True：重新开启媒体后会被当成“媒体已送达”而跳过补图。
        self.assertNotIn("pushed_by_session", self.store)

    def test_timed_out_fallback_text_is_not_resent(self):
        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        sent = []

        async def media_fail_then_text_timeout(umo, chain):
            kinds = [type(item).__name__ for item in chain.chain]
            sent.append(kinds)
            if kinds == ["Image", "Plain"]:
                raise _pre_submit_error()
            if kinds == ["Plain"]:
                # 降级正文被平台接收，只是回执超时。
                raise asyncio.TimeoutError()
            return None

        self.plugin.context = SimpleNamespace(
            send_message=media_fail_then_text_timeout
        )
        with patch.object(self.plugin, "_download_image", ok),              patch.object(apod_main.asyncio, "sleep", AsyncMock()),              self.assertLogs("apod-test", level="WARNING"):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        # 降级正文超时按“可能已送达”处理：本轮只补图，绝不重发正文。
        self.assertEqual(sent, [["Image", "Plain"], ["Plain"], ["Image"]])
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})

    def test_fast_retry_works_without_image_cache(self):
        self.plugin.cache_images = False
        sent = []

        async def send(umo, chain):
            kinds = [type(item).__name__ for item in chain.chain]
            sent.append(kinds)
            if kinds == ["Image", "Plain"]:
                raise _pre_submit_error()
            return None

        self.plugin.context = SimpleNamespace(send_message=send)
        with patch.object(apod_main.asyncio, "sleep", AsyncMock()):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        # 关缓存时直接发远端 URL，仍能在本轮补图（media=None 不等于“不可用”）。
        self.assertEqual(sent, [["Image", "Plain"], ["Plain"], ["Image"]])
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})

    def test_delivered_previous_issue_does_not_warn(self):
        # 上一期整期送达（字符串格式）→ 换刊只是正常发新一期，不是漏发。
        self.store["pushed_by_session"] = {"a": OLD_LINK}

        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        logs = _capture_logs(self)
        with patch.object(self.plugin, "_download_image", ok):
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        self.assertFalse(any("不再补发" in line for line in logs.lines))
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})

    def test_new_issue_warns_when_pending_media_is_dropped(self):
        self.store["pushed_by_session"] = {
            "a": {"link": OLD_LINK, "media_sent": False}
        }

        async def ok(url, dest):
            with open(dest, "wb") as handle:
                handle.write(b"img")

        with patch.object(self.plugin, "_download_image", ok), \
             self.assertLogs("apod-test", level="WARNING") as logs:
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        # 检查点只存当前一期：旧图无从补发，但必须看得见。
        self.assertTrue(any("不再补发" in line for line in logs.output))
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})

    def test_summary_counts_only_parts_that_need_sending(self):
        self.store["pushed_by_session"] = {
            "a": {"link": LINK, "media_sent": False}
        }

        async def boom(url, dest):
            raise ConnectionResetError("reset")

        with patch.object(self.plugin, "_download_image", boom), \
             patch.object(apod_main.asyncio, "sleep", AsyncMock()), \
             self.assertLogs("apod-test", level="INFO") as logs:
            self.assertFalse(asyncio.run(self.plugin._push_check()))
        summary = [line for line in logs.output if "本次推送" in line]
        self.assertEqual(len(summary), 1)
        # 正文已送达、本轮只缺媒体：分母不能算成需发正文。
        self.assertIn("正文 0/0、媒体 0/1", summary[0])


class PushLoopTests(unittest.TestCase):
    """定时推送的等待顺序：每次推送前只能有一次“等到点”的 sleep。"""

    def test_scheduled_loop_sleeps_once_per_push(self):
        plugin = _make_plugin(_FakeConfig({"push": {"enabled": True}}))
        plugin.push_mode = "scheduled"
        plugin.push_time = "01:00"
        plugin._seconds_until = lambda hhmm: 3600.0
        events = []

        async def fake_sleep(seconds):
            events.append(("sleep", seconds))
            if len([e for e in events if e[0] == "sleep"]) >= 2:
                raise asyncio.CancelledError()

        async def fake_check():
            events.append(("push", None))
            return True

        plugin._push_check = fake_check
        with patch.object(apod_main.asyncio, "sleep", fake_sleep):
            asyncio.run(plugin._push_loop())
        # 重复 sleep 会让推送周期从一天变成两天，也会让“距下次推送”日志失真。
        self.assertEqual(
            events[:3],
            [("sleep", 3600.0), ("push", None), ("sleep", 3600.0)],
        )

    def test_failed_push_retries_once_then_gives_up(self):
        plugin = _make_plugin(_FakeConfig({"push": {"enabled": True}}))
        plugin.push_mode = "scheduled"
        plugin.push_time = "01:00"
        plugin._seconds_until = lambda hhmm: 3600.0
        events = []

        async def fake_sleep(seconds):
            events.append(("sleep", seconds))
            if len([e for e in events if e[0] == "sleep"]) >= 3:
                raise asyncio.CancelledError()

        async def fake_check():
            events.append(("push", None))
            return False

        plugin._push_check = fake_check
        with patch.object(apod_main.asyncio, "sleep", fake_sleep), \
             self.assertLogs("apod-test", level="WARNING") as logs:
            asyncio.run(plugin._push_loop())
        # 到点 → 失败 → 6 分钟后只补试一次 → 放弃 → 等下一次到点。
        self.assertEqual(
            events[:5],
            [
                ("sleep", 3600.0),
                ("push", None),
                ("sleep", 360.0),
                ("push", None),
                ("sleep", 3600.0),
            ],
        )
        text = "\n".join(logs.output)
        self.assertIn("6 分钟后重试一次", text)
        self.assertIn("已放弃", text)
        self.assertIn("/apod 今日", text)


class CheckpointStateTests(unittest.TestCase):
    """检查点只认显式的 True/False，缺失或 null 一律当“未知”。"""

    def setUp(self):
        self.plugin = _make_plugin()
        self.link = "https://x/link"

    def test_missing_or_null_media_state_is_not_trusted(self):
        for entry in (
            {"link": self.link},
            {"link": self.link, "media_sent": None},
            {"media_sent": False},
            "",
            None,
        ):
            with self.subTest(entry=entry):
                self.assertEqual(
                    self.plugin._decode_checkpoint(entry),
                    (apod_main.CHECKPOINT_UNKNOWN, ""),
                )

    def test_explicit_states_are_recognised(self):
        pending_media = self.plugin._encode_checkpoint(self.link, media_sent=False)
        pending_text = self.plugin._encode_checkpoint(self.link, media_sent=True)
        self.assertEqual(
            self.plugin._decode_checkpoint(pending_media),
            (apod_main.CHECKPOINT_MEDIA_PENDING, self.link),
        )
        self.assertEqual(
            self.plugin._decode_checkpoint(pending_text),
            (apod_main.CHECKPOINT_TEXT_PENDING, self.link),
        )
        # 旧格式（链接字符串）直接表示整期送达。
        self.assertEqual(
            self.plugin._decode_checkpoint(self.link),
            (apod_main.CHECKPOINT_DONE, self.link),
        )


class PushPerSessionChainTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin(_FakeConfig({"push": {"sessions": ["a"]}}))
        self.plugin.push_mode = "scheduled"
        self.plugin.is_divided = False
        self.data = self.plugin._build_apod_data("APOD: t", LINK, VIDEO_ARTICLE)
        self.store = {}

        async def put(key, value):
            self.store[key] = value

        self.plugin.get_kv_data = AsyncMock(side_effect=lambda k, default=None: self.store.get(k, default))
        self.plugin.put_kv_data = AsyncMock(side_effect=put)
        self.plugin._get_cached_data = AsyncMock(return_value=self.data)
        self.plugin._translate_fields = AsyncMock(return_value=(None, None, None))
        self.send = AsyncMock(return_value=None)
        self.plugin.context = SimpleNamespace(send_message=self.send)

    def test_chain_rebuilt_per_session(self):
        self.plugin.config["push"]["sessions"] = ["a", "b", "c", "d"]
        with patch.object(self.plugin, "_build_apod_chains", wraps=self.plugin._build_apod_chains) as build:
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        self.assertEqual(build.await_count, 4)
        # 四个会话分别送达视频与正文，且写入各自检查点。
        self.assertEqual([c.args[0] for c in self.send.await_args_list], list("aabbccdd"))
        self.assertEqual(self.store["pushed_by_session"], dict.fromkeys("abcd", LINK))

    def test_partial_delivery_retries_only_text_without_building_media(self):
        self.send.side_effect = [None, _pre_submit_error(), False, None]
        self.assertFalse(asyncio.run(self.plugin._push_check()))
        partial = {"a": {"link": LINK, "media_sent": True}}
        self.assertEqual(self.store["pushed_by_session"], partial)
        # 连续两轮正文失败也必须保留媒体进度；重试不能重新构建或下载媒体。
        with patch.object(self.plugin, "_build_media_chain", side_effect=AssertionError("media rebuilt")):
            self.assertFalse(asyncio.run(self.plugin._push_check()))
            self.assertEqual(self.store["pushed_by_session"], partial)
            self.assertTrue(asyncio.run(self.plugin._push_check()))
        self.assertEqual([
            [type(item).__name__ for item in call.args[1].chain]
            for call in self.send.await_args_list
        ], [["Video"], ["Plain"], ["Plain"], ["Plain"]])
        self.assertEqual(self.store["pushed_by_session"], {"a": LINK})
        # 完整送达的旧格式字符串仍然生效。
        self.assertTrue(asyncio.run(self.plugin._push_check()))
        self.assertEqual(self.send.await_count, 4)

    def test_other_issue_partial_checkpoint_does_not_skip_new_media(self):
        self.store["pushed_by_session"] = {"a": {"link": "old", "media_sent": True}}
        self.assertTrue(asyncio.run(self.plugin._push_check()))
        self.assertIsInstance(self.send.await_args_list[0].args[1].chain[0], _COMP.Video)

    def test_checkpoint_storage_failure_aborts_remaining_sessions(self):
        self.plugin.config["push"]["sessions"] = ["a", "b"]
        for fail_read in (True, False):
            with self.subTest(fail_read=fail_read):
                self.send.reset_mock()
                target = "get_kv_data" if fail_read else "put_kv_data"
                with patch.object(self.plugin, target, side_effect=RuntimeError("storage down")):
                    self.assertFalse(asyncio.run(self.plugin._push_check()))
                self.assertEqual(self.send.await_count, 0 if fail_read else 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
