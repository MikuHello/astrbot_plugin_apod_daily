"""APOD 插件修复的回归测试。

本机没有安装 astrbot，测试前用最小桩替换 `astrbot.*` 依赖，
再直接从源码路径加载 main.py，只覆盖纯解析/渲染/编排逻辑。
"""
import asyncio
import importlib.util
import logging
import os
import sys
import tempfile
import types
import unittest
import zoneinfo
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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


class _FakeSP:
    def __init__(self):
        self.prefs = []
        self.removed = []

    async def range_get_async(self, scope, scope_id, key=None):
        return list(self.prefs)

    async def remove_async(self, scope, scope_id, key):
        self.removed.append(key)


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

    core = mod("astrbot.core")
    core.sp = _FakeSP()
    core_utils = mod("astrbot.core.utils")
    core_utils.__path__ = []
    core.utils = core_utils
    astrbot_path = mod("astrbot.core.utils.astrbot_path")
    _PLUGIN_DATA_DIR = tempfile.mkdtemp(prefix="apod-plugin-data-")
    astrbot_path.get_astrbot_plugin_data_path = lambda: _PLUGIN_DATA_DIR
    core_utils.astrbot_path = astrbot_path

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

# 本机测试环境没有 tzdata（运行时 AstrBot 自带），统一固定为同一个 ET 时区。
try:
    _ET = zoneinfo.ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 - 本地无 tzdata 时退化为固定 EDT
    _ET = timezone(timedelta(hours=-4))
apod_main.APOD._et_timezone = staticmethod(lambda: _ET)


class _FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    async def save_config_async(self):
        self.saved += 1


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
    plugin.timeout = 30
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

    def test_message_chain_uses_video_component(self):
        chain = asyncio.run(
            self.plugin._build_message_chain(self._video_data(), None, None)
        )
        self.assertIsInstance(chain.chain[0], _COMP.Video)
        self.assertIsInstance(chain.chain[-1], _COMP.Plain)
        self.assertEqual(chain.chain[0].file, MP4)

    def test_message_chain_with_media_false_is_text_only(self):
        chain = asyncio.run(
            self.plugin._build_message_chain(
                self._video_data(), None, None, with_media=False
            )
        )
        self.assertEqual(len(chain.chain), 1)
        self.assertIsInstance(chain.chain[0], _COMP.Plain)


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


class TranslationParseTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin()

    def test_parse_both_fields(self):
        title, expl = self.plugin._parse_translation_response(
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
        title, expl = self.plugin._parse_translation_response(raw, True, True)
        self.assertEqual(title, "67P 彗星的三维图")
        self.assertEqual(expl, "这是说明。")

    def test_title_takes_first_line_and_drops_period(self):
        title, _ = self.plugin._parse_translation_response(
            "<title>67P 彗星的三维图。\n多余第二行</title>", True, False
        )
        self.assertEqual(title, "67P 彗星的三维图")

    def test_single_field_falls_back_to_whole_text(self):
        title, _ = self.plugin._parse_translation_response("3D 67P", True, False)
        self.assertEqual(title, "3D 67P")

    def test_single_field_essay_fallback_is_rejected(self):
        essay = (
            "3D 67P 是一个天文学术语的组合，需要更多上下文才能准确翻译。"
            "如果是指 67P/Churyumov-Gerasimenko，则可译为 67P彗星的三维图。"
        )
        title, _ = self.plugin._parse_translation_response(essay, True, False)
        self.assertIsNone(title)

    def test_unclosed_tag_is_a_failure(self):
        title, expl = self.plugin._parse_translation_response(
            "<title>67P 彗星的三维图</title><explanation>被截断的说明", True, True
        )
        self.assertEqual(title, "67P 彗星的三维图")
        self.assertIsNone(expl)


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
        title, expl = asyncio.run(
            self.plugin._translate_uncached("3D 67P", "English text.", "prov")
        )
        self.assertEqual(title, "三维 67P")
        self.assertEqual(expl, "说明译文。")
        self.assertEqual(len(self.plugin.context.calls), 1)

    def test_parse_failure_retries_then_succeeds(self):
        self.plugin.translate_parse_retry = 1
        self.plugin.context = _FakeContext(
            [
                "3D 67P 是一个天文学术语的组合，需要更多上下文……",
                "<title>三维 67P</title><explanation>说明译文。</explanation>",
            ]
        )
        title, expl = asyncio.run(
            self.plugin._translate_uncached("3D 67P", "English text.", "prov")
        )
        self.assertEqual(title, "三维 67P")
        self.assertEqual(len(self.plugin.context.calls), 2)

    def test_retry_appends_correction_notice(self):
        self.plugin.translate_parse_retry = 1
        self.plugin.context = _FakeContext(
            [
                "3D 67P 是一个天文学术语的组合，需要更多上下文……",
                "<title>三维 67P</title><explanation>说明译文。</explanation>",
            ]
        )
        asyncio.run(
            self.plugin._translate_uncached("3D 67P", "English text.", "prov")
        )
        first = self.plugin.context.calls[0]["prompt"]
        second = self.plugin.context.calls[1]["prompt"]
        self.assertNotIn("上次输出未遵守结构", first)
        self.assertIn("上次输出未遵守结构", second)
        self.assertIn("<title>…</title>", second)
        self.assertIn("<explanation>…</explanation>", second)

    def test_no_retry_returns_partial(self):
        self.plugin.translate_parse_retry = 0
        self.plugin.context = _FakeContext(["<title>三维 67P</title>没有说明标签"])
        title, expl = asyncio.run(
            self.plugin._translate_uncached("3D 67P", "English text.", "prov")
        )
        self.assertEqual(title, "三维 67P")
        self.assertIsNone(expl)


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
        title, expl = asyncio.run(
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

    def test_second_call_hits_field_cache(self):
        payload = {"title": "3D 67P", "explanation": "English text."}
        asyncio.run(self.plugin._translate_fields(payload))
        asyncio.run(self.plugin._translate_fields(payload))
        self.assertEqual(len(self.plugin.context.calls), 1)

    def test_only_title_is_requested_when_explanation_not_sent(self):
        self.plugin.send_explanation = False
        title, expl = asyncio.run(
            self.plugin._translate_fields(
                {"title": "3D 67P", "explanation": "English text."}
            )
        )
        self.assertEqual(title, "三维 67P")
        self.assertIsNone(expl)
        prompt = self.plugin.context.calls[0]["system_prompt"]
        self.assertNotIn("<explanation>", prompt)


class ConfigMigrationTests(unittest.TestCase):
    def _plugin_with_cache(self, config):
        plugin = _make_plugin(config)
        store = {}

        async def get_cache(key):
            return store.get(key)

        async def put_cache(key, value):
            store[key] = value

        plugin.get_cache = get_cache
        plugin.put_cache = put_cache
        return plugin, store

    def test_migrate_old_flat_config(self):
        config = _FakeConfig(
            {
                "image": False,
                "title": {"is_show": True, "is_translate": False},
                "explanation": {"is_show": False, "is_translate": True},
                "date": {"is_show": True},
                "provider": "liming/claude-opus-4-6",
                "push": {"send_image": False, "translate": True, "sessions": []},
            }
        )
        plugin, store = self._plugin_with_cache(config)
        asyncio.run(plugin._migrate_config())
        self.assertEqual(config["send"]["media"], False)
        self.assertEqual(config["send"]["title"], True)
        self.assertEqual(config["send"]["explanation"], False)
        self.assertEqual(config["send"]["date"], True)
        self.assertEqual(config["translate"]["title"], False)
        self.assertEqual(config["translate"]["explanation"], True)
        self.assertEqual(config["translate"]["provider"], "liming/claude-opus-4-6")
        self.assertTrue(store.get("config_layout_v2"))
        self.assertEqual(config.saved, 1)

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
        paths = []
        for i in range(3):
            path = os.path.join(directory, f"f{i}.jpg")
            with open(path, "wb") as handle:
                handle.write(b"x")
            paths.append(path)
            asyncio.run(self.plugin._touch_image_cache(path))
        self.assertFalse(os.path.exists(paths[0]))
        self.assertTrue(os.path.exists(paths[2]))

    def test_localize_disabled_returns_none(self):
        self.plugin.cache_images = False
        self.assertIsNone(
            asyncio.run(self.plugin._localize_image("https://x/a.jpg"))
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
