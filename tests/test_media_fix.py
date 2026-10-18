"""APOD 视频型条目修复的回归测试。

本机没有安装 astrbot，测试前用最小桩替换 `astrbot.*` 依赖，
再直接从源码路径加载 main.py，只覆盖纯解析/渲染逻辑。
"""
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path

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

    class Video(_Component):
        @staticmethod
        def fromURL(url):
            return Video(file=url)

    comp = mod("astrbot.api.message_components")
    comp.Plain, comp.Image, comp.Video = Plain, Image, Video
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

    # aiohttp 只在网络层使用，解析/渲染测试不需要真实实现。
    aiohttp = mod("aiohttp")

    class ClientTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class ClientError(Exception):
        pass

    class ClientSession:
        def __init__(self, *args, **kwargs):
            pass

    aiohttp.ClientTimeout = ClientTimeout
    aiohttp.ClientError = ClientError
    aiohttp.ClientSession = ClientSession
    return comp


_COMP = _install_astrbot_stubs()
_spec = importlib.util.spec_from_file_location("apod_main", MAIN_PATH)
apod_main = importlib.util.module_from_spec(_spec)
sys.modules["apod_main"] = apod_main
_spec.loader.exec_module(apod_main)


def _make_plugin():
    plugin = apod_main.APOD(None, {})
    plugin.image = True
    plugin.is_divided = True
    plugin.title = {"is_show": True}
    plugin.date = {"is_show": True}
    plugin.explanation = {"is_show": True}
    return plugin


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

    def test_image_entry_still_parses_as_image(self):
        data = self.plugin._build_apod_data(
            "APOD: 2026 October 17 \u2013 3D 67P", LINK, IMAGE_ARTICLE
        )
        self.assertEqual(data["media_type"], "image")
        self.assertTrue(data["url"].endswith("67p.jpg"))
        self.assertEqual(data["credit"], "Jane Doe")

    def test_no_media_entry_stays_other(self):
        data = self.plugin._build_apod_data("APOD: title", LINK, NO_MEDIA_ARTICLE)
        self.assertEqual(data["media_type"], "other")
        self.assertIsNone(data["url"])

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

    def test_display_text_other_shows_link_warning(self):
        data = self.plugin._build_apod_data("APOD: title", LINK, NO_MEDIA_ARTICLE)
        text = self.plugin._build_display_text(data, None, None)
        self.assertIn("⚠️", text)
        self.assertIn(f"原文链接：{LINK}", text)

    def test_message_chain_uses_video_component(self):
        chain = self.plugin._build_message_chain(self._video_data(), None, None)
        self.assertIsInstance(chain.chain[0], _COMP.Video)
        self.assertIsInstance(chain.chain[-1], _COMP.Plain)
        self.assertEqual(chain.chain[0].file, MP4)

    def test_message_chain_with_image_false_is_text_only(self):
        chain = self.plugin._build_message_chain(
            self._video_data(), None, None, with_image=False
        )
        self.assertEqual(len(chain.chain), 1)
        self.assertIsInstance(chain.chain[0], _COMP.Plain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
