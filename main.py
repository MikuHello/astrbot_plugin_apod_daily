# ruff: noqa: UP006, UP035, UP045
import asyncio
import hashlib
import html as html_lib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Dict, Optional
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import PermissionType
from astrbot.api.star import Context, Star, register

# 数据层说明：
#   最新一期：science.nasa.gov 站点 RSS feed（无需 API Key、无速率限制，
#     每天约 UTC 04:05 发布标题形如 "APOD: 2026 October 11 – ..." 的条目），
#     拉取失败时回退缓存（KV + 内存）。
#   随机一期：官网 Random APOD 同款 embed 接口，单次 POST 返回与 feed
#     content 同构的完整 HTML，不做缓存。
#   解析：两类内容共用同一套解析器，来源差异只体现为 URL 与模式常量。
# 已知坑：feed 的 XML 头畸形（两个 xmlns 声明之间缺空格），解析前需修复。
FEED_URL = "https://science.nasa.gov/feed/"
FEED_XML_FIX_PATTERN = re.compile(r'"(?=xmlns:)')
FEED_TITLE_DATE_PATTERN = re.compile(
    r"(\d{4})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})"
)
FEED_TITLE_PREFIX_PATTERN = re.compile(r"APOD:\s*\d{4}\s+\S+\s+\d{1,2}\s*[–—-]\s*(.+)")
MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}
# 官网 Random APOD 的随机即由该接口在后端完成（randomizePost=True）。
RANDOM_EMBED_URL = "https://science.nasa.gov/wp-json/smd/v1/embed-post"
RANDOM_EMBED_ATTRIBUTES = {
    "selectedPostTypes": ["image-article"],
    "selectedCategory": "22766",
    "selectedInternalTerm": "0",
    "selectedOrg": "22768",
    "displayFeaturedImageAndTitleOnly": False,
    "randomizePost": True,
}
# 内容正文里的结构化 meta 表格：<th>标签</th><td>值</td>（Date/Credit 等）。
META_ROW_PATTERN = re.compile(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", re.S)
META_DATE_PATTERN = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")
APOD_SLUG_DATE_PATTERN = re.compile(
    r"apod-(\d{4})-(january|february|march|april|may|june|july|august|september|october|november|december)-(\d{1,2})",
    re.IGNORECASE,
)
# 随机内容中文档顺序的第一个 image-article 链接即主帖（实测确认）。
RANDOM_POST_LINK_PATTERN = re.compile(
    r"https://science\.nasa\.gov/image-article/[^\s\"'<>]+"
)
CREDIT_PREFIX_PATTERN = re.compile(
    r"^(?:(?:Image|Video)\s+)?Credit(?:\s*&\s*Copyright)?\s*:\s*", re.IGNORECASE
)
# NASA 每日一图（IOTD）：无 feed/REST/日期元数据，只能抓两级页面；
# og:image 即主图，展示日期按美东抓取日（与官网“当日”语义一致）。
NASA_PAGE_URL = "https://www.nasa.gov/image-of-the-day/"
NASA_POST_LINK_PATTERN = re.compile(
    r"https://www\.nasa\.gov/image-article/[^\s\"'<>]+"
)
# 视频型条目（如 2026-10-16 的猎鹰9号延时摄影）：science.nasa.gov 不给海报帧，
# og:image 会退化成 NASA logo，真正的媒体是 og:video / <video><source src> 直链。
# 注意 logo 的 URL 实际是 NASA_logo-1.png（下划线），两种写法都要拦截。
PLACEHOLDER_MARKS = ("nasa-logo", "nasa_logo", "/themes/")

# APOD 每天美东 00:05 发布（feed pubDate 实测逐秒固定 04:05 UTC / EDT）。
# 因此缓存只按这个固定时刻过期，不再用「12h TTL + 1h 提前刷新」的启发式。
APOD_PUBLISH_ET_HOUR = 0
APOD_PUBLISH_ET_MINUTE = 5
# 固定过期点 = 下一次美东 00:05 + 宽限；宽限用于躲过 feed 的 CDN 缓存窗口（max-age=300）。
APOD_CACHE_GRACE_MINUTES = 10
# 抓到旧一期（源站/CDN 尚未更新）时只短时有效，尽快重试。
APOD_STALE_RETRY_MINUTES = 5
# 图片缓存淘汰前的最小存活时间：待发送的消息可能还引用着刚写入的文件。
IMAGE_CACHE_MIN_AGE_SECONDS = 120


# 文章发布时间只认这两种 meta 名，不拿更新时间冒充。
PUBLISHED_META_KEYS = ("parsely-pub-date", "article:published_time")


class _HTMLAttrParser(HTMLParser):
    """收集 og:image、发布时间 meta 与 <img> src：属性顺序、引号风格与大小写无关。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.og_image = ""
        self.og_video = ""
        self.published_meta = ""
        self.img_srcs = []
        self.video_srcs = []

    def handle_starttag(self, tag, attrs):
        attrs_map = {name.lower(): value or "" for name, value in attrs}
        if tag == "meta":
            key = attrs_map.get("property") or attrs_map.get("name")
            key = key.lower() if key else ""
            if key == "og:image" and not self.og_image:
                self.og_image = attrs_map.get("content", "")
            elif key in OG_VIDEO_META_KEYS and not self.og_video:
                self.og_video = attrs_map.get("content", "")
            elif key in PUBLISHED_META_KEYS and not self.published_meta:
                self.published_meta = attrs_map.get("content", "")
        elif tag == "img" and attrs_map.get("src"):
            self.img_srcs.append(attrs_map["src"])
        elif tag in ("video", "source") and attrs_map.get("src"):
            # <video src> 与 <video><source src> 都算候选视频直链。
            self.video_srcs.append(attrs_map["src"])
IMAGE_URL_SOURCES = (
    "assets.science.nasa.gov",
    "apod.nasa.gov/apod/image",
    "images-assets.nasa.gov",
    "wp-content/uploads",
)
# 视频直链：优先看扩展名，同时兼容 og:video 的多种 property 写法。
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".ogv", ".m3u8")
OG_VIDEO_META_KEYS = ("og:video", "og:video:url", "og:video:secure_url")
EXPLANATION_STOP_MARKERS = (
    "APOD's main NASA site is moving",
    "Tomorrow's picture",
    "Yesterday's Image",
    "Tomorrow's Image",
    "Image Credit:",
    "Video Credit:",
    "Credit &",
    "Credit:",
    "Authors & editors",
    "A service of",
    "The post ",
    "appeared first on",
    "Random APOD Generator",
)
CREDIT_STOP_MARKERS = (
    "Authors & editors",
    "A service of",
    "Random APOD Generator",
    "Yesterday's Image",
    "The post ",
)
PLUGIN_DISPLAY_NAME = "天文每日一图"
NASA_DISPLAY_NAME = "NASA 每日一图"
CREDIT_LABEL = "图片来源与版权"
APOD_HELP = chr(10).join(
    [
        "✨ 天文每日一图 ✨",
        "",
        "📷 /apod 今日 |—— 最新一期",
        "🎲 /apod 随机 |—— 随机一期历史天文图",
        "🌌 /nasa      |—— 最新 NASA 每日一图",
        "",
        "🔔 /apod管理  |—— 推送订阅设置",
    ]
)


@register("apod", "Cysheper", "天文每日一图插件：APOD + NASA 每日一图，订阅推送、随机、LLM 翻译与函数工具。", "1.1.0")
class APOD(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.context = context
        self.last_apod_error: Optional[str] = None
        self._stale_apod: Optional[dict] = None
        self._stale_nasa: Optional[dict] = None
        self._content_locks: Dict[str, asyncio.Lock] = {}
        self._translation_flights: Dict[str, asyncio.Task] = {}
        self._refresh_cooldowns: Dict[str, datetime] = {}
        self._translate_index_lock = asyncio.Lock()
        self._image_index_lock = asyncio.Lock()
        self._image_reconcile_task: Optional[asyncio.Task] = None
        self._cache_epoch = 0

    @staticmethod
    def _ensure_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _is_valid_apod_data(apod_data: Any) -> bool:
        return (
            isinstance(apod_data, dict)
            and "date" in apod_data
            and "explanation" in apod_data
        )

    @staticmethod
    def _build_translation_cache_key(provider_id: Optional[str], text: str) -> str:
        digest = hashlib.sha256(
            f"{provider_id or ''}\x00{text}".encode("utf-8")
        ).hexdigest()
        return f"translate_cache:{digest}"

    async def _migrate_config(self) -> None:
        """旧版扁平配置 → 新版「发送配置 / 翻译配置」分组的一次性迁移。

        AstrBot 会在插件加载前按新 schema 归一化配置，并删除未声明的键，
        因此旧键必须保留在 schema 里（invisible）。用 KV 标记保证只迁移一次，
        之后不再覆盖用户在新分组上的改动。
        """
        if await self.get_cache("config_layout_v2"):
            return

        send_cfg = self._ensure_dict(self.config.setdefault("send", {}))
        translate_cfg = self._ensure_dict(self.config.setdefault("translate", {}))
        push_cfg = self._ensure_dict(self.config.get("push", {}))
        old_title = self._ensure_dict(self.config.get("title"))
        old_expl = self._ensure_dict(self.config.get("explanation"))
        old_date = self._ensure_dict(self.config.get("date"))

        if "image" in self.config and self.config.get("image") is not True:
            send_cfg["media"] = bool(self.config.get("image"))
        if "send_image" in push_cfg and push_cfg.get("send_image") is not True:
            # send.media 已由默认值占位，setdefault 无效；旧值 False 必须直接写入。
            send_cfg["media"] = False
        if "is_show" in old_title and old_title.get("is_show") is not True:
            send_cfg["title"] = bool(old_title.get("is_show"))
        if "is_show" in old_expl and old_expl.get("is_show") is not True:
            send_cfg["explanation"] = bool(old_expl.get("is_show"))
        if "is_show" in old_date and old_date.get("is_show") is not True:
            send_cfg["date"] = bool(old_date.get("is_show"))
        if "is_translate" in old_title and old_title.get("is_translate") is not True:
            translate_cfg["title"] = bool(old_title.get("is_translate"))
        if "is_translate" in old_expl and old_expl.get("is_translate") is not True:
            translate_cfg["explanation"] = bool(old_expl.get("is_translate"))
        if self.config.get("provider"):
            translate_cfg["provider"] = str(self.config.get("provider"))
        # 旧推送翻译开关：false 表示当时不想翻译，归一为全局翻译关闭。
        if push_cfg.get("translate") is False:
            translate_cfg["title"] = False
            translate_cfg["explanation"] = False

        await self.put_cache("config_layout_v2", True)
        await self.config.save_config_async()
        logger.info("APOD 配置已迁移到新的发送/翻译分组。")

    async def _migrate_cache_limit(self) -> None:
        """把旧 translate_cache_limit 并入 apod_cache_limit（同样用 KV 标记只跑一次）。"""
        if await self.get_cache("config_layout_v3"):
            return
        legacy_limit = int(self.config.get("translate_cache_limit", 1000))
        # 旧键为默认值时不算用户意图，避免覆盖导入的新配置。
        if "translate_cache_limit" in self.config and legacy_limit != 1000:
            self.config["apod_cache_limit"] = max(0, legacy_limit)
        await self.put_cache("config_layout_v3", True)
        await self.config.save_config_async()

    async def _clear_reusable_caches(self) -> int:
        """清空可复用缓存（翻译 + 内容），保留推送状态（避免重复群发）。

        先停生产者再枚举：递增缓存代际（在途翻译的写回会被丢弃），
        并在两个内容锁内枚举/删除，避免删除后被并发刷新写回。
        不取消翻译 flight：取消会把 CancelledError 传播给等待方，跳过原文降级。
        翻译缓存用范围读枚举：插件自带的 FIFO 索引可能漏键（已实测 43 vs 25），
        而 sp.range_get_async 能拿到本插件全部 KV。返回删除项数。"""
        from astrbot.core import sp  # 本地导入：避免模块级依赖核心内部对象

        # 1) 递增代际：等待方拿到结果也不会再写回缓存。
        self._cache_epoch += 1
        deleted = 0
        # 2) 持有内容锁：等正在进行的刷新结束（它会写完），再枚举删除。
        async with self._content_lock("apod_cache"), self._content_lock(
            "nasa_cache"
        ):
            try:
                prefs = await sp.range_get_async("plugin", self.plugin_id)
            except Exception as exc:
                logger.error(f"枚举插件缓存失败，仅清理已知键：{exc}")
                prefs = []
            for pref in prefs:
                key = getattr(pref, "key", None)
                if isinstance(key, str) and key.startswith("translate_cache"):
                    await self.delete_cache(key)
                    deleted += 1

            for key in ("apod_cache", "nasa_cache"):
                await self.delete_cache(key)
                deleted += 1

            # 图片缓存（若开启）：删文件 + 删索引。
            try:
                cache_dir = self._image_cache_dir()
                if os.path.isdir(cache_dir):
                    for name in os.listdir(cache_dir):
                        try:
                            os.remove(os.path.join(cache_dir, name))
                            deleted += 1
                        except OSError:
                            pass
            except Exception as exc:
                logger.error(f"清理图片缓存目录失败：{exc}")
            await self.delete_cache("image_cache_index")

        # 3) 内存态一并复位，避免清库后仍从 stale/cooldown 回填。
        self._stale_apod = None
        self._stale_nasa = None
        self._refresh_cooldowns.clear()
        logger.info(f"已清理 APOD 可复用缓存，共 {deleted} 项。")
        return deleted

    async def initialize(self):
        logger.info("正在初始化 NASA APOD 插件...")

        # 旧版配置先迁移到 send/translate 分组，再读取。
        await self._migrate_config()
        await self._migrate_cache_limit()

        send_cfg = self._ensure_dict(self.config.get("send", {}))
        self.send_media = bool(send_cfg.get("media", True))
        self.send_title = bool(send_cfg.get("title", True))
        self.send_explanation = bool(send_cfg.get("explanation", True))
        self.send_date = bool(send_cfg.get("date", True))

        translate_cfg = self._ensure_dict(self.config.get("translate", {}))
        self.translate_title = bool(translate_cfg.get("title", True))
        self.translate_explanation = bool(translate_cfg.get("explanation", True))
        # 翻译模型：优先用插件配置的 provider，未配置时回落到 AstrBot 默认对话模型。
        self.provider = str(translate_cfg.get("provider", "") or "")
        self.translate_parse_retry = (
            1 if bool(translate_cfg.get("parse_retry", True)) else 0
        )

        self.is_divided = bool(self.config.get("is_divided", True))
        self.timeout = max(1, int(self.config.get("timeout", 120)))
        self.retry_count = max(0, int(self.config.get("retry_count", 3)))
        # 统一条数上限：翻译缓存与图片缓存各自按它淘汰最旧。
        self.cache_limit = max(
            0,
            int(
                self.config.get(
                    "apod_cache_limit",
                    self.config.get("translate_cache_limit", 1000),
                )
            ),
        )
        self.cache_images = bool(self.config.get("cache_images", True))
        # 定时预缓存（默认关）：每天 APOD 发布后主动抓取 + 翻译，预热缓存。
        self.prefetch_enabled = bool(self.config.get("prefetch", False))
        self._prefetch_task: Optional[asyncio.Task] = None

        push_cfg = self._ensure_dict(self.config.get("push", {}))
        self.push_enabled = bool(push_cfg.get("enabled", False))
        self.push_mode = str(push_cfg.get("mode", "scheduled")).strip().lower()
        if self.push_mode not in ("scheduled", "upstream"):
            self.push_mode = "scheduled"
        self.push_time = str(push_cfg.get("push_time", "01:00")).strip() or "01:00"
        self.push_interval_minutes = max(5, int(push_cfg.get("interval_minutes", 30)))
        self._push_task: Optional[asyncio.Task] = None

        # 历史版本把订阅名单存在 KV；现以插件配置为唯一数据源，启动时做一次性迁移。
        kv_sessions = await self.get_cache("push_sessions")
        if isinstance(kv_sessions, list) and kv_sessions:
            merged = list(dict.fromkeys(self._get_push_sessions() + [str(s) for s in kv_sessions]))
            await self._save_push_sessions(merged)
            await self.put_cache("push_sessions", [])
            logger.info(f"APOD 订阅名单已从 KV 迁移到插件配置（共 {len(merged)} 个会话）。")

        if self.push_enabled:
            self._push_task = asyncio.create_task(self._push_loop())
            if self.push_mode == "upstream":
                logger.info(
                    f"APOD 上游推送已启动：每 {self.push_interval_minutes} 分钟巡检一次 feed。"
                )
            else:
                logger.info(
                    f"APOD 定时推送已启动：每天 {self.push_time} 推送最新一期。"
                )

        if self.prefetch_enabled:
            self._prefetch_task = asyncio.create_task(self._prefetch_loop())
            logger.info("APOD 定时预缓存已启动。")

    @filter.command("apod")
    async def apod(self, event: AstrMessageEvent):
        """输出帮助菜单，可用子命令：今日 / 随机"""
        tokens = (event.message_str or "").strip().split()
        if tokens and tokens[0].lower() == "apod":
            tokens = tokens[1:]
        sub = tokens[0].lower() if tokens else ""

        if sub in ("今日", "today", "latest", "最新"):
            async for result in self._reply_latest_apod(event):
                yield result
        elif sub in ("随机", "random"):
            async for result in self._reply_random_apod(event):
                yield result
        else:
            yield event.plain_result(APOD_HELP)

    async def _reply_latest_apod(self, event: AstrMessageEvent):
        logger.info("正在获取最新 APOD ...")
        apod_data = await self._get_latest_apod()
        if apod_data is None:
            yield event.plain_result(
                self.last_apod_error or "获取 APOD 数据失败，请稍后重试。"
            )
            return
        async for result in self._reply_apod(event, apod_data):
            yield result

    async def _reply_random_apod(self, event: AstrMessageEvent):
        logger.info("正在获取随机 APOD ...")
        apod_data = await self._fetch_random_apod()
        if apod_data is None:
            yield event.plain_result(
                self.last_apod_error or "获取随机 APOD 失败，请稍后重试。"
            )
            return
        async for result in self._reply_apod(event, apod_data):
            yield result

    async def _reply_apod(
        self, event: AstrMessageEvent, apod_data: dict, display_name: str = PLUGIN_DISPLAY_NAME
    ):
        """按展示配置渲染并发送某一期内容（apod 今日/随机、nasa 共用）。"""
        media_type = str(apod_data.get("media_type") or "").strip().lower()
        url = apod_data.get("hdurl") or apod_data.get("url")
        # 图片/视频都可以直接作为媒体发送；没有可用媒体时降级为文字+链接，
        # 不再把视频型条目当成错误回退。
        title_zh, explanation_zh = await self._translate_fields(
            apod_data, umo=event.unified_msg_origin
        )
        display_text = self._build_display_text(
            apod_data, title_zh, explanation_zh, display_name=display_name
        )

        # 翻译可能较慢；放在本地化之前，缩短“待发送文件被淘汰”的窗口。
        media_component = None
        media_path = None
        if self.send_media and url:
            if media_type == "video":
                media_component = Comp.Video.fromURL(url)
            else:
                media_path = await self._localize_image(url)
                media_component = (
                    Comp.Image.fromFileSystem(media_path)
                    if media_path
                    else Comp.Image.fromURL(url)
                )

        # 图片可按配置合并；视频始终分开发送：平台发不了视频时，
        # 也不能把正文和原文链接一起拖下水。
        if self.is_divided or media_type == "video":
            # 图片一条、文本一条。
            has_output = False
            if media_component is not None:
                has_output = True
                if media_type == "video":
                    yield event.chain_result([media_component])
                else:
                    yield event.image_result(media_path or url)
            if display_text:
                has_output = True
                yield event.plain_result(display_text)
            if not has_output:
                yield event.plain_result("当前配置未启用任何可返回的内容。")
            return

        # 图/视频 + 文聚合为一条消息链。
        chain = []
        if media_component is not None:
            chain.append(media_component)
        if display_text:
            chain.append(Comp.Plain(display_text))
        if not chain:
            yield event.plain_result("当前配置未启用任何可返回的内容。")
            return

        yield event.chain_result(chain)

    @filter.command("apod管理")
    @filter.permission_type(PermissionType.GROUP_ADMIN)
    async def apod_manage(self, event: AstrMessageEvent):
        """输出管理帮助菜单，可用子命令：订阅 / 退订 / 清理"""
        tokens = (event.message_str or "").strip().split()
        if tokens and tokens[0] in ("apod管理", "APOD管理"):
            tokens = tokens[1:]
        sub = tokens[0].lower() if tokens else ""

        if sub in ("subscribe", "sub", "订阅"):
            sessions = self._get_push_sessions()
            umo = event.unified_msg_origin
            if umo in sessions:
                yield event.plain_result("本会话已经订阅过 APOD 推送啦～")
            else:
                sessions.append(umo)
                await self._save_push_sessions(sessions)
                yield event.plain_result(
                    "订阅成功！APOD 发布新一期时会自动推送到本会话喵～"
                )
            return

        if sub in ("unsubscribe", "unsub", "退订"):
            sessions = self._get_push_sessions()
            umo = event.unified_msg_origin
            if umo in sessions:
                sessions.remove(umo)
                await self._save_push_sessions(sessions)
                yield event.plain_result("已退订 APOD 推送～")
            else:
                yield event.plain_result("本会话还没有订阅 APOD 推送喵。")
            return

        if sub in ("清理", "清理缓存", "clear"):
            deleted = await self._clear_reusable_caches()
            yield event.plain_result(
                f"已清理可复用缓存（翻译/内容）共 {deleted} 项。"
            )
            return

        # 裸指令：输出支持的子命令与填写方式。
        usage = chr(10).join(
            [
                "🔔 推送管理",
                "",
                "✅ /apod管理 订阅 |—— 订阅本会话推送",
                "🚫 /apod管理 退订 |—— 退订本会话推送",
                "🧹 /apod管理 清理 |—— 清空翻译与内容缓存",
                "",
                "订阅名单可在插件配置页「推送/订阅名单 (UMO)」维护",
            ]
        )
        yield event.plain_result(usage)

    @filter.command("nasa")
    async def nasa(self, event: AstrMessageEvent):
        """获取 NASA 每日一图"""
        logger.info("正在获取 NASA 每日一图 ...")
        nasa_data = await self._get_nasa()
        if nasa_data is None:
            yield event.plain_result(
                self.last_apod_error or "获取 NASA 每日一图失败，请稍后重试。"
            )
            return
        async for result in self._reply_apod(
            event, nasa_data, display_name=NASA_DISPLAY_NAME
        ):
            yield result

    # ---------- LLM 函数工具 ----------

    @filter.llm_tool(name="send_apod_today")
    async def send_apod_today(self, event: AstrMessageEvent):
        """获取今日天文每日一图（APOD）并发送到当前会话，含图片与中文说明。"""
        return await self._deliver_apod(event, await self._get_latest_apod(), "今日APOD")

    @filter.llm_tool(name="send_apod_random")
    async def send_apod_random(self, event: AstrMessageEvent):
        """随机获取一期历史天文每日一图（APOD）并发送到当前会话，含图片与中文说明。"""
        return await self._deliver_apod(
            event, await self._fetch_random_apod(), "随机APOD"
        )

    @filter.llm_tool(name="send_nasa_today")
    async def send_nasa_today(self, event: AstrMessageEvent):
        """获取今日 NASA 每日一图（Image of the Day）并发送到当前会话，含图片与中文说明。"""
        return await self._deliver_apod(event, await self._get_nasa(), "NASA每日一图")

    async def _deliver_apod(
        self, event: AstrMessageEvent, apod_data: Optional[dict], label: str
    ) -> str:
        if apod_data is None:
            return self.last_apod_error or f"获取{label}失败，请稍后重试。"
        # 没有可用媒体（视频/无图）时不再判失败：消息链只带文字+原文链接。
        title_zh, explanation_zh = await self._translate_fields(
            apod_data, umo=event.unified_msg_origin
        )
        display_name = (
            NASA_DISPLAY_NAME if label.startswith("NASA") else PLUGIN_DISPLAY_NAME
        )
        message = await self._build_message_chain(
            apod_data,
            title_zh,
            explanation_zh,
            with_media=self.send_media,
            display_name=display_name,
        )
        sent = False
        try:
            sent = await self.context.send_message(
                event.unified_msg_origin, message
            )
        except Exception as exc:
            logger.error(f"发送{label}消息链失败：{exc}")
        if (
            sent is False
            and self.send_media
            and str(apod_data.get("media_type") or "").lower() == "video"
        ):
            # 视频组件不受平台支持时，降级为“文字+原文链接”。
            fallback = await self._build_message_chain(
                apod_data,
                title_zh,
                explanation_zh,
                with_media=False,
                display_name=display_name,
            )
            try:
                sent = await self.context.send_message(
                    event.unified_msg_origin, fallback
                )
                logger.info(f"{label}视频发送失败，已降级为文字+链接。")
            except Exception as exc:
                logger.error(f"发送{label}降级消息链失败：{exc}")
                sent = False
        if sent is False:
            logger.warning(f"发送{label}：未匹配到消息平台。")
            return f"{label}发送失败：未找到可用的消息平台。"
        title = (title_zh or apod_data.get("title") or "").strip()
        return (
            f"已向当前会话发送{label}："
            f"{apod_data.get('date', '')}《{title}》，可向用户做简要介绍。"
        )

    # 插件自带的 KV 存储足够保存 APOD 数据和翻译结果。
    async def put_cache(self, key: str, value: Any):
        try:
            await self.put_kv_data(key, value)
        except Exception as exc:
            logger.error(f"写入缓存失败，键：{key}，错误：{exc}")

    async def get_cache(self, key: str) -> Optional[Any]:
        try:
            return await self.get_kv_data(key, None)
        except Exception as exc:
            logger.error(f"读取缓存失败，键：{key}，错误：{exc}")
            return None

    async def delete_cache(self, key: str) -> None:
        try:
            await self.delete_kv_data(key)
        except Exception as exc:
            logger.error(f"删除缓存失败，键：{key}，错误：{exc}")

    async def _load_push_checkpoint(self) -> dict:
        """读取投递检查点；存储故障向上抛出，与“无记录”严格区分。"""
        try:
            checkpoint = await self.get_kv_data("pushed_by_session", {})
        except Exception as exc:
            raise RuntimeError(f"读取推送检查点失败：{exc}") from exc
        return (
            {k: v for k, v in checkpoint.items() if isinstance(k, str)}
            if isinstance(checkpoint, dict)
            else {}
        )

    async def _save_push_checkpoint(self, pushed: dict) -> None:
        try:
            await self.put_kv_data("pushed_by_session", pushed)
        except Exception as exc:
            raise RuntimeError(f"写入推送检查点失败：{exc}") from exc

    async def _cache_translation(self, key: str, value: str) -> None:
        """写入翻译缓存并按配置上限淘汰最旧条目（FIFO 索引，真删除）。

        索引在任何配置下都维护：限量关闭期间的写入也要参与未来的淘汰。"""
        if not value:
            return
        async with self._translate_index_lock:
            await self.put_cache(key, value)
            index = await self.get_cache("translate_cache_index")
            keys = (
                [k for k in index if isinstance(k, str)]
                if isinstance(index, list)
                else []
            )
            if key not in keys:
                keys.append(key)
            limit = self.cache_limit
            while limit > 0 and len(keys) > limit:
                await self.delete_cache(keys.pop(0))
            await self.put_cache("translate_cache_index", keys)

    async def _translate_fields(
        self,
        apod_data: Dict[str, Any],
        umo: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """按发送/翻译配置翻译标题与说明。

        一条请求可同时翻译两个字段；缓存仍按字段各自存放（键不变），
        因此开关变化不会产生版本化/迁移，最多重译。
        """
        explanation = (apod_data.get("explanation") or "").strip()
        title = (apod_data.get("title") or "").strip()

        want_title = bool(title) and self.translate_title and self.send_title
        want_explanation = (
            bool(explanation)
            and self.translate_explanation
            and self.send_explanation
        )
        if not want_title and not want_explanation:
            return None, None

        provider_id = await self._resolve_chat_provider_id(umo)
        if not provider_id:
            logger.warning("未能解析翻译模型，本次使用原文。")
            return None, None

        title_zh = explanation_zh = None
        if want_title:
            title_zh = await self.get_cache(
                self._build_translation_cache_key(provider_id, title)
            )
        if want_explanation:
            explanation_zh = await self.get_cache(
                self._build_translation_cache_key(provider_id, explanation)
            )

        need_title = want_title and title_zh is None
        need_explanation = want_explanation and explanation_zh is None
        if not need_title and not need_explanation:
            return title_zh, explanation_zh

        epoch = self._cache_epoch
        got_title, got_explanation = await self._translate_batch(
            title if need_title else None,
            explanation if need_explanation else None,
            provider_id,
        )
        # 清理（_cache_epoch 变化）之后不再写回，避免“清完又被填回”。
        if need_title and got_title:
            title_zh = got_title
            if epoch == self._cache_epoch:
                await self._cache_translation(
                    self._build_translation_cache_key(provider_id, title), got_title
                )
        if need_explanation and got_explanation:
            explanation_zh = got_explanation
            if epoch == self._cache_epoch:
                await self._cache_translation(
                    self._build_translation_cache_key(provider_id, explanation),
                    got_explanation,
                )
        return title_zh, explanation_zh

    async def _translate_batch(
        self,
        title: Optional[str],
        explanation: Optional[str],
        provider_id: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """一条请求翻译缺失字段；single-flight 合并并发，失败降级 (None, None)。"""
        request_key = self._build_translation_cache_key(
            provider_id, f"batch\x00{title or ''}\x00{explanation or ''}"
        )
        flight = self._translation_flights.get(request_key)
        if flight is None:
            flight = asyncio.create_task(
                self._run_translation_flight(
                    request_key, title, explanation, provider_id
                )
            )
            self._translation_flights[request_key] = flight

        try:
            # shield 隔离等待方取消：单个会话超时不终止共享 worker。
            return await asyncio.wait_for(
                asyncio.shield(flight), timeout=self.timeout
            )
        except Exception as exc:
            logger.warning(f"翻译未在预算内完成，本次降级使用原文：{exc}")
            return None, None

    async def _run_translation_flight(
        self,
        request_key: str,
        title: Optional[str],
        explanation: Optional[str],
        provider_id: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """single-flight 工作体：模型调用限时，失败与异常统一降级为 (None, None)。"""
        try:
            return await asyncio.wait_for(
                self._translate_uncached(title, explanation, provider_id),
                timeout=self.timeout,
            )
        except Exception as exc:
            # 在 worker 内消费异常：即使已无等待者，
            # 也不会出现 “Future exception was never retrieved”。
            logger.warning(f"翻译执行失败，降级为原文：{exc}")
            return None, None
        finally:
            self._translation_flights.pop(request_key, None)

    async def _translate_uncached(
        self,
        title: Optional[str],
        explanation: Optional[str],
        provider_id: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """调用模型并按结构化标签解析；解析失败按配置重试，不缓存失败结果。"""
        want_title = title is not None
        want_explanation = explanation is not None
        system_prompt = self._build_translation_system_prompt(
            want_title, want_explanation
        )
        prompt = self._build_translation_input(title, explanation)

        best_title: Optional[str] = None
        best_explanation: Optional[str] = None
        retry_notice = self._build_translation_retry_notice(
            want_title, want_explanation
        )
        attempts = self.translate_parse_retry + 1
        for attempt in range(attempts):
            # 第 2 次起附上纠正提示：同一 prompt 纯重复收益很低，
            # 把「上次错在哪」告诉模型，重试才有意义。
            attempt_prompt = prompt if attempt == 0 else f"{prompt}\n\n{retry_notice}"
            raw = await self._call_translation_llm(
                system_prompt, attempt_prompt, provider_id
            )
            got_title, got_explanation = self._parse_translation_response(
                raw, want_title, want_explanation
            )
            best_title = got_title or best_title
            best_explanation = got_explanation or best_explanation
            title_ok = not want_title or got_title is not None
            explanation_ok = not want_explanation or got_explanation is not None
            if title_ok and explanation_ok:
                return best_title, best_explanation
            logger.warning(f"翻译未按标签结构返回（{attempt + 1}/{attempts}）。")
        return best_title, best_explanation

    @staticmethod
    def _build_translation_input(
        title: Optional[str], explanation: Optional[str]
    ) -> str:
        # 输入与输出使用同一组标签：模型只需把标签里的内容翻译后放回。
        parts = []
        if title is not None:
            parts.append(f"<title>{title}</title>")
        if explanation is not None:
            parts.append(f"<explanation>{explanation}</explanation>")
        return chr(10).join(parts)

    @staticmethod
    def _build_translation_system_prompt(
        want_title: bool, want_explanation: bool
    ) -> str:
        if want_title and want_explanation:
            task = "把标签内的标题与说明分别翻译为简体中文"
        elif want_title:
            task = "把标签内的标题翻译为简体中文"
        else:
            task = "把标签内的说明翻译为简体中文"

        lines = [
            "你是一位精通天文学的翻译专家。",
            f"一、遵循「信、达、雅」，{task}；",
            "二、专有名词与编号保留原形（如 67P、ISS、APOD）；",
        ]
        if want_title:
            lines.append("三、标题：不可译则不译，原样输出；单行，不加句号；")
        lines.append(
            f"{'四' if want_title else '三'}、禁止项：不要添加原文没有的 markdown、"
            "引号、列表或解释文字。"
        )
        lines.append("只输出以下结构，标签之外不要有任何字符：")
        lines.append("```")
        if want_title:
            lines.append("<title>译好的标题</title>")
        if want_explanation:
            lines.append("<explanation>译好的说明</explanation>")
        lines.append("```")
        lines.append("以上内容将直接用于发布，不经过二次编辑。")
        return chr(10).join(lines)

    @staticmethod
    def _build_translation_retry_notice(
        want_title: bool, want_explanation: bool
    ) -> str:
        tags = []
        if want_title:
            tags.append("<title>…</title>")
        if want_explanation:
            tags.append("<explanation>…</explanation>")
        return (
            "上次输出未遵守结构。请只输出 "
            + " 和 ".join(tags)
            + "，标签之外不要有任何字符。"
        )

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        # 模型偶尔自作主张包代码块；剥掉最外层围栏，标签外内容反正会被忽略。
        return re.sub(r"^```[A-Za-z0-9_-]*\s*|\s*```$", "", text.strip()).strip()

    @classmethod
    def _parse_translation_response(
        cls, text: Optional[str], want_title: bool, want_explanation: bool
    ) -> tuple[Optional[str], Optional[str]]:
        cleaned = cls._strip_code_fence(text or "")
        if not cleaned:
            return None, None

        def grab(tag: str) -> Optional[str]:
            matched = re.search(
                rf"<{tag}\b[^>]*>(.*?)</{tag}>", cleaned, re.S | re.I
            )
            return matched.group(1).strip() if matched else None

        title = grab("title") if want_title else None
        explanation = grab("explanation") if want_explanation else None
        # 残留未闭合标签 = 这次输出是残缺的，不能走纯文本兜底。
        leftover_tag = bool(re.search(r"</?(?:title|explanation)\b", cleaned, re.I))
        # 单字段请求且模型没写完整标签 → 整体兜底；但标题必须是短单行，
        # 否则视为解析失败（防小作文从无标签路径混进来）。
        if (
            want_title
            and not want_explanation
            and title is None
            and not leftover_tag
        ):
            candidate = cleaned.splitlines()[0].strip() if cleaned else ""
            if candidate and len(candidate) <= 40:
                title = candidate
        if (
            want_explanation
            and not want_title
            and explanation is None
            and not leftover_tag
        ):
            explanation = cleaned
        # 空标签（<title></title>）会产生空串，splitlines()[0] 会抛 IndexError，
        # 所以取首行前先允许空值。
        if title is not None:
            first_line = next(iter(title.splitlines()), "")
            title = first_line.strip().rstrip("。.．") or None
        if explanation is not None:
            explanation = explanation.strip() or None
        return title, explanation

    @staticmethod
    def _format_date_cn(date_str: str) -> str:
        """把 YYYY-MM-DD 格式化成 YYYY年M月D日；无法解析时原样返回。"""
        matched = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_str.strip())
        if not matched:
            return date_str
        year, month, day = matched.groups()
        return f"{year}年{int(month)}月{int(day)}日"

    def _build_display_text(
        self,
        apod_data: Dict[str, Any],
        title_zh: Optional[str],
        explanation_zh: Optional[str],
        display_name: str = PLUGIN_DISPLAY_NAME,
    ) -> str:
        """生成“品牌头 + 标题/日期/版权 + 空行 + 说明”的正文。"""
        show_title = self.send_title
        show_date = self.send_date
        show_explanation = self.send_explanation

        title = (title_zh or apod_data.get("title") or "").strip()
        title_raw = str(apod_data.get("title") or "").strip()
        date_cn = self._format_date_cn(str(apod_data.get("date") or "").strip())
        credit = str(apod_data.get("credit") or "").strip()
        explanation = (explanation_zh or apod_data.get("explanation") or "").strip()

        newline = chr(10)
        lines = [display_name]
        media_type = str(apod_data.get("media_type") or "").strip().lower()
        link = str(apod_data.get("link") or "").strip()
        if show_title and title:
            # 有译文且与原文不同时，展示“译文（原文）”；否则只展示其一
            if title_zh and title_raw and title_zh.strip() != title_raw:
                lines.append(f"标题：{title_zh.strip()}（{title_raw}）")
            else:
                lines.append(f"标题：{title}")
        if show_date and date_cn:
            lines.append(f"日期：{date_cn}")
        if media_type == "video":
            lines.append("▶ 本期为视频")
        elif media_type not in ("image", ""):
            lines.append("⚠️ 本期无可用图片，可查看原文")
        if credit:
            lines.append(f"{CREDIT_LABEL}：{credit}")
        # 非图片条目（视频/无媒体）必须把原文链接带上，避免只剩文字。
        if link and media_type != "image":
            lines.append(f"原文链接：{link}")
        text = newline.join(lines)
        if show_explanation and explanation:
            text = (
                f"{text}{newline}{newline}说明：{explanation}"
                if text
                else f"说明：{explanation}"
            )
        return text

    async def _resolve_chat_provider_id(
        self, umo: Optional[str] = None
    ) -> Optional[str]:
        """优先插件配置的 provider；否则回落到当前会话/全局对话模型。"""
        if self.provider:
            return self.provider
        try:
            pid = await self.context.get_current_chat_provider_id(umo=umo)
            if pid:
                return pid
            logger.warning("未能解析当前对话模型。")
        except Exception as exc:
            logger.warning(f"获取 AstrBot 当前对话模型失败：{exc}")
        return None

    async def _call_translation_llm(
        self, system_prompt: str, prompt: str, provider_id: str
    ) -> Optional[str]:
        logger.info(f"正在翻译 APOD 内容（provider={provider_id}）...")
        llm_resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=system_prompt,
            prompt=prompt,
        )
        return llm_resp.completion_text if llm_resp else None

    # “拉取并写入缓存”集中到这里，apod / nasa 最新内容共用同一套缓存语义。
    async def _refresh_cached_data(
        self, cache_key: str, fetch, stale_attr: str
    ) -> Optional[dict]:
        apod_data = await fetch()
        if apod_data is None:
            return None

        if not self._is_valid_apod_data(apod_data):
            logger.error(f"获取到的数据无效：{apod_data}")
            return None

        apod_data["retrieved_at"] = datetime.now(timezone.utc).isoformat()
        setattr(self, stale_attr, apod_data)
        await self.put_cache(cache_key, apod_data)
        return apod_data

    def _content_lock(self, cache_key: str) -> asyncio.Lock:
        lock = self._content_locks.get(cache_key)
        if lock is None:
            lock = self._content_locks[cache_key] = asyncio.Lock()
        return lock

    async def _attempt_refresh(
        self, cache_key: str, refresh, *, respect_cooldown: bool
    ) -> Optional[dict]:
        """统一刷新入口：冷却期内跳过抓取；任何分支的抓取失败都记入
        5 分钟冷却期，成功则清除。

        返回 None 表示“处于冷却未抓取”或“抓取失败”，由调用方按各自
        语义回退（冷缓存回退 stale、损坏缓存不回传、投递不发送）。
        """
        if respect_cooldown:
            cooldown_until = self._refresh_cooldowns.get(cache_key)
            if (
                cooldown_until is not None
                and datetime.now(timezone.utc) < cooldown_until
            ):
                logger.info("刷新处于失败冷却期，跳过本次抓取。")
                return None
        fresh = await refresh()
        if fresh is not None:
            self._refresh_cooldowns.pop(cache_key, None)
            return fresh
        self._refresh_cooldowns[cache_key] = datetime.now(timezone.utc) + timedelta(
            minutes=5
        )
        logger.warning("刷新失败，进入冷却期。")
        return None

    async def _get_cached_data(
        self,
        cache_key: str,
        fetch,
        stale_attr: str,
        force_refresh: bool = False,
        freshness=None,
    ) -> Optional[dict]:
        """内容缓存层：默认「12h 硬 TTL + 超 1h 日期落后即刷新」；
        传入 freshness 时改用它判定（如 APOD 按固定发布时间过期）。
        刷新失败回退 KV 旧值或内存 stale，避免直接对用户报错。
        按来源加锁双检：并发冷启动只触发一次抓取；force_refresh 供推送
        强制刷新并同步写回共享缓存。
        失败冷却统一收口在 _attempt_refresh：冷缓存、数据无效、时间戳
        缺失与过期刷新共用同一入口，冷却内一律复用旧内容。"""

        async def refresh() -> Optional[dict]:
            return await self._refresh_cached_data(cache_key, fetch, stale_attr)

        async with self._content_lock(cache_key):
            if force_refresh:
                # 投递语义：强制刷新必须真正尝试抓取（不受冷却拦截）；
                # 抓取失败返回 None，由调用方决定不发送，不拿旧缓存当新内容。
                return await self._attempt_refresh(
                    cache_key, refresh, respect_cooldown=False
                )

            apod_data = await self.get_cache(cache_key)
            if apod_data is None:
                fresh = await self._attempt_refresh(
                    cache_key, refresh, respect_cooldown=True
                )
                return fresh or getattr(self, stale_attr, None)

            if not self._is_valid_apod_data(apod_data):
                logger.warning("缓存中的数据无效，正在刷新缓存。")
                fresh = await self._attempt_refresh(
                    cache_key, refresh, respect_cooldown=True
                )
                return fresh or getattr(self, stale_attr, None)

            # 时间元数据损坏只应触发刷新：刷新失败仍可使用手里的旧内容，
            # 不因新鲜度信息缺失而把可用正文判定为失败。
            retrieved_at = None
            retrieved_at_raw = apod_data.get("retrieved_at")
            if retrieved_at_raw:
                try:
                    retrieved_at = datetime.fromisoformat(retrieved_at_raw)
                    if retrieved_at.tzinfo is None:
                        # 历史 naive 值为服务器本地时间，按本地时区补齐。
                        retrieved_at = retrieved_at.astimezone()
                except (TypeError, ValueError):
                    logger.warning("缓存中的时间格式无效，将刷新缓存。")
                    retrieved_at = None
            if retrieved_at is None:
                logger.info("缓存缺少有效 retrieved_at，正在刷新缓存。")
                fresh = await self._attempt_refresh(
                    cache_key, refresh, respect_cooldown=True
                )
                return fresh or apod_data

            age = (
                datetime.now(timezone.utc) - retrieved_at.astimezone(timezone.utc)
            ).total_seconds()
            if freshness is not None:
                # 固定时刻过期（APOD）：不用 12h/1h 启发式。
                expired = not freshness(apod_data)
            else:
                # 12 小时硬 TTL；超 1h 且日期落后美东今天时提前刷新。
                expired = age > 12 * 3600
                if not expired and age > 3600:
                    cached_date = str(apod_data.get("date") or "")
                    if cached_date and cached_date != self._expected_latest_date():
                        logger.info("缓存日期落后于最新一期，正在刷新缓存。")
                        expired = True
            if expired:
                logger.info("缓存中的数据已过期，正在刷新缓存。")
                fresh = await self._attempt_refresh(
                    cache_key, refresh, respect_cooldown=True
                )
                if fresh is not None:
                    return fresh
                logger.warning("刷新失败或处于冷却期，回退使用过期缓存。")
            setattr(self, stale_attr, apod_data)
            return apod_data

    async def _get_latest_apod(self) -> Optional[dict]:
        return await self._get_cached_data(
            "apod_cache",
            self._fetch_latest_apod,
            "_stale_apod",
            freshness=self._apod_cache_fresh,
        )

    def _apod_cache_valid_until(self, retrieved_at: datetime) -> datetime:
        """有效期到「下一次美东 00:05 + 宽限」。

        直接按 00:05+宽限 这个边界算：在宽限窗口内（如 00:08）抓取时只算到
        当天的边界，不会因“下一个 00:05”被推到明天而锁一整天。"""
        tz = self._et_timezone()
        local = retrieved_at.astimezone(tz)
        cutoff = local.replace(
            hour=APOD_PUBLISH_ET_HOUR,
            minute=APOD_PUBLISH_ET_MINUTE,
            second=0,
            microsecond=0,
        ) + timedelta(minutes=APOD_CACHE_GRACE_MINUTES)
        if cutoff <= local:
            cutoff += timedelta(days=1)
        return cutoff

    def _expected_latest_apod_date(self) -> str:
        """美东当前应有的最新 APOD 日期：00:05 发布前仍算前一天。"""
        tz = self._et_timezone()
        now = datetime.now(tz)
        if (now.hour, now.minute) < (
            APOD_PUBLISH_ET_HOUR,
            APOD_PUBLISH_ET_MINUTE,
        ):
            now -= timedelta(days=1)
        return now.strftime("%Y-%m-%d")

    def _apod_cache_fresh(self, apod_data: dict) -> bool:
        """APOD 缓存是否仍有效：过期点 + 内容日期双重判断。"""
        retrieved_at_raw = apod_data.get("retrieved_at")
        if not retrieved_at_raw:
            return False
        try:
            retrieved_at = datetime.fromisoformat(str(retrieved_at_raw))
        except (TypeError, ValueError):
            return False
        if retrieved_at.tzinfo is None:
            retrieved_at = retrieved_at.astimezone()
        now = datetime.now(timezone.utc)
        if now >= self._apod_cache_valid_until(retrieved_at):
            return False
        cached_date = str(apod_data.get("date") or "")
        if cached_date and cached_date != self._expected_latest_apod_date():
            # 抓到的是旧一期：只短时有效，尽快重试，不锁到下一个过期点。
            retry_until = retrieved_at.astimezone(timezone.utc) + timedelta(
                minutes=APOD_STALE_RETRY_MINUTES
            )
            return now < retry_until
        return True

    def _seconds_until_apod_prefetch(self) -> float:
        """距下一次过期边界（美东 00:05 + 宽限）的秒数。"""
        tz = self._et_timezone()
        now = datetime.now(tz)
        return (self._apod_cache_valid_until(now) - now).total_seconds()

    async def _prefetch_loop(self) -> None:
        """定时预缓存：每天 APOD 发布后主动抓取 + 翻译，预热缓存。"""
        try:
            while True:
                delay = self._seconds_until_apod_prefetch()
                logger.info(f"APOD 定时预缓存：距下次约 {delay / 3600:.1f} 小时。")
                await asyncio.sleep(delay)
                try:
                    apod_data = await self._get_cached_data(
                        "apod_cache",
                        self._fetch_latest_apod,
                        "_stale_apod",
                        force_refresh=True,
                    )
                    if apod_data is not None:
                        await self._translate_fields(apod_data)
                        logger.info(
                            f"APOD 定时预缓存完成：{apod_data.get('date')}"
                        )
                except Exception as exc:
                    logger.error(f"APOD 定时预缓存失败：{exc}")
        except asyncio.CancelledError:
            pass

    async def _get_nasa(self) -> Optional[dict]:
        """NASA IOTD：记录也缓存，但**每次请求都核对列表页指纹**。

        核对只花 1 次 GET（列表页）；指纹未变直接用缓存记录，
        变了才抓文章页重新解析并覆盖写入（旧记录自然过期）。
        核对失败时退回缓存，不冒险重抓也不报错。"""
        cache_key = "nasa_cache"
        async with self._content_lock(cache_key):
            cached = await self.get_cache(cache_key)
            if cached is not None and not self._is_valid_apod_data(cached):
                cached = None
            cached_link = str(cached.get("link") or "") if cached else ""

            latest_link = await self._probe_nasa_latest_link()
            if latest_link and cached and latest_link == cached_link:
                self._stale_nasa = cached
                return cached
            if latest_link is None and cached is not None:
                # 核对失败：保持现有记录，避免源站抖动导致报错。
                self._stale_nasa = cached
                return cached

            async def refresh() -> Optional[dict]:
                return await self._refresh_cached_data(
                    cache_key,
                    lambda: self._refresh_nasa(latest_link),
                    "_stale_nasa",
                )

            fresh = await self._attempt_refresh(
                cache_key, refresh, respect_cooldown=True
            )
            if fresh is not None:
                return fresh
            return cached or self._stale_nasa

    # ---------- 数据获取主入口 ----------
    # 最新一期走站点 RSS（无需 token、无速率限制），失败时由缓存层兜底
    # （KV / 内存 stale）；随机一期走官网同款 embed 接口，不做缓存。

    async def _fetch_latest_apod(self) -> Optional[dict]:
        self.last_apod_error = None
        timeout = aiohttp.ClientTimeout(total=self.timeout)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            feed_data = await self._fetch_from_feed(session)
            if feed_data is not None:
                return feed_data

        self.last_apod_error = self.last_apod_error or (
            "NASA feed 获取失败，请稍后重试。"
        )
        return None

    async def _fetch_random_apod(self) -> Optional[dict]:
        self.last_apod_error = None
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(self.retry_count + 1):
                try:
                    async with session.post(
                        RANDOM_EMBED_URL,
                        json={"attributes": RANDOM_EMBED_ATTRIBUTES, "currentPostId": 0},
                    ) as response:
                        if response.status != 200:
                            logger.error(f"获取随机 APOD 失败：状态码={response.status}")
                            self.last_apod_error = "NASA 随机接口暂时不可用，请稍后重试。"
                        else:
                            payload = await response.json(content_type=None)
                            content_html = (
                                payload.get("html", "")
                                if isinstance(payload, dict)
                                else ""
                            )
                            apod_data = self._build_random_apod(content_html)
                            if apod_data is not None:
                                return apod_data
                            self.last_apod_error = (
                                "NASA 随机接口未返回有效条目，请稍后重试。"
                            )
                except asyncio.TimeoutError:
                    logger.error(f"获取随机 APOD 超时：请求在 {self.timeout} 秒后超时。")
                    self.last_apod_error = "请求 NASA 随机接口超时，请稍后重试。"
                except (aiohttp.ClientError, ValueError) as exc:
                    logger.error(f"获取随机 APOD 时发生网络或解析错误：{exc}")
                    self.last_apod_error = "连接 NASA 随机接口失败，请稍后重试。"

                if attempt < self.retry_count:
                    await asyncio.sleep(min(2**attempt, 4))
        self.last_apod_error = self.last_apod_error or (
            "获取随机 APOD 失败，请稍后重试。"
        )
        return None

    def _build_random_apod(self, content_html: str) -> Optional[dict]:
        match = RANDOM_POST_LINK_PATTERN.search(content_html or "")
        if not match:
            return None
        link = html_lib.unescape(match.group(0))
        if "apod-" not in link.lower():
            return None
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", content_html, re.S)
        title = self._strip_html(title_match.group(1)) if title_match else ""
        return self._build_apod_data(title, link, content_html)

    @staticmethod
    def _extract_nasa_latest_link(page_html: str) -> Optional[str]:
        """从 IOTD 列表页取第一个 image-article 链接（正文 h1 之后）。"""
        h1_index = (page_html or "").find("<h1")
        match = NASA_POST_LINK_PATTERN.search(
            page_html or "", h1_index + 1 if h1_index >= 0 else 0
        )
        return html_lib.unescape(match.group(0)) if match else None

    async def _probe_nasa_latest_link(self) -> Optional[str]:
        """每次请求的指纹核对：1 次 GET 列表页，失败返回 None。"""
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            page_html = await self._fetch_text(
                session, NASA_PAGE_URL, "NASA 每日一图页面"
            )
        if page_html is None:
            return None
        return self._extract_nasa_latest_link(page_html)

    async def _refresh_nasa(self, link: Optional[str]) -> Optional[dict]:
        """抓取指定文章页并解析；link 为空时先重新解析列表页。"""
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if not link:
                page_html = await self._fetch_text(
                    session, NASA_PAGE_URL, "NASA 每日一图页面"
                )
                if page_html is None:
                    return None
                link = self._extract_nasa_latest_link(page_html)
                if not link:
                    self.last_apod_error = (
                        "NASA 每日一图页面未找到今日条目，请稍后重试。"
                    )
                    return None
            post_html = await self._fetch_text(session, link, "NASA 每日一图文章")
            if post_html is None:
                return None
            return self._build_nasa_data(post_html, link)

    @staticmethod
    def _extract_article_region(page_html: str) -> str:
        """截取主内容 <article> 容器：页眉导航与页脚在容器之外。"""
        start = (page_html or "").find("<article")
        if start < 0:
            return page_html or ""
        end = page_html.find("</article>", start)
        return (
            page_html[start : end + len("</article>")]
            if end >= 0
            else page_html[start:]
        )

    def _build_nasa_data(self, page_html: str, link: str) -> Optional[dict]:
        """og:image 取主图；日期取文章发布时间元数据，缺失时保留未知。"""
        region = self._extract_article_region(page_html)
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", region, re.S)
        title = self._strip_html(title_match.group(1)) if title_match else ""
        page_parser = _HTMLAttrParser()
        page_parser.feed(page_html)
        image_url = html_lib.unescape(page_parser.og_image).strip()
        if image_url and not self._is_usable_image(image_url):
            image_url = ""
        video_url = html_lib.unescape(page_parser.og_video).strip()
        if video_url and not self._is_usable_video(video_url):
            video_url = ""
        if not image_url:
            region_parser = _HTMLAttrParser()
            region_parser.feed(region)
            for src in region_parser.img_srcs:
                candidate = html_lib.unescape(src).strip()
                if self._is_usable_image(candidate):
                    image_url = candidate
                    break
        # 说明与版权只在文章主内容容器里找，按原文顺序保留全部段落；
        # 页眉导航与页脚在容器之外，不参与拼接，也不做长度过滤。
        explanation_parts = []
        credit = ""
        for p in re.findall(r"<p[^>]*>(.*?)</p>", region, re.S):
            text = self._strip_html(p)
            lowered = text.lower()
            if not credit and lowered.startswith(("image credit:", "video credit:")):
                credit = text.split(":", 1)[1].strip()
                continue
            if lowered.startswith(("learn more", "image article")):
                continue
            explanation_parts.append(text)
        explanation = " ".join(part for part in explanation_parts if part)
        # 发布时间与 og:image 同源：meta 由同一解析器按属性解析收集
        # （顺序/引号/大小写无关）；只认发布时间，不拿更新时间冒充。
        published_raw = html_lib.unescape(page_parser.published_meta)
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", published_raw)
        # 通用正文构造器只接收 article region：主图解析到此为止，article
        # 内找不到就诚实返回无图，不回退扫描全页误选页脚或相关文章图片；
        # og:image 与发布时间仍在此前从全页提取。
        return self._build_apod_data(
            title,
            link,
            region,
            date_str=date_match.group(1) if date_match else "",
            explanation=explanation,
            credit=credit or None,
            image_url=image_url,
            video_url=video_url,
        )

    async def _fetch_text(
        self, session: aiohttp.ClientSession, url: str, label: str
    ) -> Optional[str]:
        for attempt in range(self.retry_count + 1):
            try:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.error(f"获取{label}失败：状态码={response.status}")
                        self.last_apod_error = f"{label}暂时不可用，请稍后重试。"
                        if 400 <= response.status < 500 and response.status not in (408, 429):
                            # 客户端错误重试无意义，直接结束本次获取。
                            break
                    else:
                        return await response.text()
            except asyncio.TimeoutError:
                logger.error(f"获取{label}超时：请求在 {self.timeout} 秒后超时。")
                self.last_apod_error = f"请求{label}超时，请稍后重试。"
            except aiohttp.ClientError as exc:
                logger.error(f"获取{label}时发生网络错误：{exc}")
                self.last_apod_error = f"连接{label}失败，请稍后重试。"

            if attempt < self.retry_count:
                await asyncio.sleep(min(2**attempt, 4))
        return None

    async def _fetch_from_feed(
        self, session: aiohttp.ClientSession, feed_url: str = FEED_URL
    ) -> Optional[dict]:
        raw_text = await self._fetch_text(session, feed_url, "NASA feed")
        if raw_text is None:
            return None
        try:
            apod_data = self._parse_feed(raw_text)
        except ElementTree.ParseError as exc:
            logger.error(f"解析 NASA feed XML 失败：{exc}")
            self.last_apod_error = "NASA feed 数据格式异常，请稍后重试。"
            return None
        if apod_data is None:
            self.last_apod_error = "NASA feed 中未找到有效的 APOD 条目，请稍后重试。"
        return apod_data

    def _parse_feed(self, raw_text: str) -> Optional[dict]:
        repaired = FEED_XML_FIX_PATTERN.sub('" ', raw_text)
        root = ElementTree.fromstring(repaired)
        for item in root.findall(".//{*}item"):
            title_raw = (item.findtext("{*}title") or "").strip()
            link = (item.findtext("{*}link") or "").strip()
            if not (title_raw.startswith("APOD:") or "/apod-" in link):
                continue
            content_html = ""
            pub_date_raw = ""
            enclosure_url = ""
            for child in item:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "encoded":
                    content_html = child.text or ""
                elif tag == "pubDate":
                    pub_date_raw = (child.text or "").strip()
                elif tag in ("enclosure", "content") and not enclosure_url:
                    # RSS enclosure / media:content 里 type="video/..." 或
                    # medium="video" 的 url 就是视频直链。
                    media_kind = " ".join(
                        (
                            child.attrib.get("type", ""),
                            child.attrib.get("medium", ""),
                        )
                    ).lower()
                    candidate = child.attrib.get("url", "")
                    if candidate and "video" in media_kind:
                        enclosure_url = candidate
            return self._build_apod_data(
                title_raw, link, content_html, pub_date_raw, video_url=enclosure_url
            )
        logger.warning("NASA feed 中未找到 APOD 条目。")
        return None

    # ---------- 内容解析（feed 条目与随机内容共用） ----------

    @staticmethod
    def _strip_html(fragment: str) -> str:
        return re.sub(
            r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))
        ).strip()

    def _fallback_explanation(self, content_html: str) -> str:
        """无 Explanation 标记时的兜底：拼接正文 <p> 段落，避免整页纯文本混入导航与表格。"""
        paragraphs = []
        total = 0
        for p in re.findall(r"<p[^>]*>(.*?)</p>", content_html or "", re.S):
            text = self._strip_html(p)
            if len(text) < 80:
                continue
            paragraphs.append(text)
            total += len(text)
            if total >= 800:
                break
        return " ".join(paragraphs) if paragraphs else self._strip_html(content_html)

    @classmethod
    def _parse_meta_rows(cls, content_html: str) -> list:
        rows = []
        for th_html, td_html in META_ROW_PATTERN.findall(content_html or ""):
            rows.append((cls._strip_html(th_html), cls._strip_html(td_html)))
        return rows

    @staticmethod
    def _month_number(month_name: str) -> Optional[int]:
        return MONTH_NAMES.get(month_name.capitalize())

    def _resolve_apod_date(
        self, title_raw: str, link: str, content_html: str, pub_date_raw: str
    ) -> str:
        """日期解析优先级：标题 → meta 表格 → 链接 slug → pubDate。"""
        matched = FEED_TITLE_DATE_PATTERN.search(title_raw or "")
        if matched:
            year, month_name, day = matched.groups()
            return f"{int(year):04d}-{MONTH_NAMES[month_name]:02d}-{int(day):02d}"

        for label, value in self._parse_meta_rows(content_html or ""):
            if label.lower() != "date":
                continue
            matched = META_DATE_PATTERN.search(value)
            month_number = self._month_number(matched.group(1)) if matched else None
            if matched and month_number:
                return (
                    f"{int(matched.group(3)):04d}-{month_number:02d}"
                    f"-{int(matched.group(2)):02d}"
                )
            break

        matched = APOD_SLUG_DATE_PATTERN.search(link or "")
        if matched:
            month_number = self._month_number(matched.group(2))
            if month_number:
                return (
                    f"{int(matched.group(1)):04d}-{month_number:02d}"
                    f"-{int(matched.group(3)):02d}"
                )

        if pub_date_raw:
            try:
                return (
                    parsedate_to_datetime(pub_date_raw)
                    .astimezone(self._et_timezone())
                    .strftime("%Y-%m-%d")
                )
            except (TypeError, ValueError):
                pass
        return ""

    @classmethod
    def _credit_from_meta_rows(cls, content_html: str) -> str:
        for label, value in cls._parse_meta_rows(content_html or ""):
            if "credit" in label.lower():
                return CREDIT_PREFIX_PATTERN.sub("", value).strip()
        return ""

    @staticmethod
    def _credit_from_markers(normalized_text: str, plain_text: str) -> str:
        for credit_marker in (
            "Video Credit:",
            "Credit & Copyright:",
            "Image Credit:",
            "Credit:",
        ):
            marker_index = normalized_text.find(credit_marker)
            if marker_index < 0:
                continue
            start = marker_index + len(credit_marker)
            stop_index = len(normalized_text)
            for stop_marker in CREDIT_STOP_MARKERS:
                found = normalized_text.find(stop_marker, start)
                if found >= 0:
                    stop_index = min(stop_index, found)
            return plain_text[start:stop_index].strip()
        return ""

    # 图片、说明、版权与日期全部从 content HTML 提取；feed 条目与随机
    # 内容的差异只体现在标题与日期的取值入口上。
    def _build_apod_data(
        self,
        title_raw: str,
        link: str,
        content_html: str,
        pub_date_raw: str = "",
        *,
        date_str: Optional[str] = None,
        explanation: Optional[str] = None,
        credit: Optional[str] = None,
        image_url: Optional[str] = None,
        video_url: Optional[str] = None,
    ) -> Optional[dict]:
        plain_text = self._strip_html(META_ROW_PATTERN.sub(" ", content_html or ""))
        # 原文的撇号是 U+2019（’），标记匹配前统一映射为 ASCII 撇号；
        # 替换是等长的，可直接用同一索引回切原文，不影响展示内容。
        normalized_text = plain_text.replace("\u2019", "'")

        if explanation is None:
            explanation = ""
            marker_index = normalized_text.find("Explanation:")
            if marker_index >= 0:
                explanation = plain_text[marker_index + len("Explanation:") :].strip()
                normalized_tail = explanation.replace("\u2019", "'")
                for stop_marker in EXPLANATION_STOP_MARKERS:
                    stop_index = normalized_tail.find(stop_marker)
                    if stop_index > 0:
                        explanation = explanation[:stop_index].strip()
            if not explanation:
                explanation = self._fallback_explanation(content_html)

        content_parser = _HTMLAttrParser()
        content_parser.feed(content_html or "")
        if not image_url:
            for src in content_parser.img_srcs:
                candidate = html_lib.unescape(src).strip()
                if self._is_usable_image(candidate):
                    image_url = candidate
                    break

        # 视频直链候选：显式传入（feed enclosure / NASA og:video）→ og:video →
        # 正文 <video>/<source>；任何一个可用即按视频条目处理。
        video_candidates = (
            video_url,
            content_parser.og_video,
            *content_parser.video_srcs,
        )
        for candidate in video_candidates:
            candidate = html_lib.unescape(candidate or "").strip()
            if self._is_usable_video(candidate):
                video_url = candidate
                break
        else:
            video_url = ""

        title_matched = FEED_TITLE_PREFIX_PATTERN.match(title_raw or "")
        title = (
            title_matched.group(1).strip()
            if title_matched
            else (title_raw or "").strip()
        )
        date_str = date_str or self._resolve_apod_date(
            title_raw, link, content_html, pub_date_raw
        )
        credit = credit or self._credit_from_meta_rows(
            content_html
        ) or self._credit_from_markers(normalized_text, plain_text)

        # 日期允许未知（如 NASA 页面缺失发布时间）：展示层自动省略日期行。
        if not explanation:
            logger.error(f"内容信息不完整：link={link!r}")
            return None

        # 直链视频（已经 _is_usable_video 过滤）优先于图片：APOD/IOTD 里
        # 出现可用直链视频时几乎就是视频条目；图片可能只是海报帧。
        if video_url:
            media_type, media_url = "video", video_url
        elif image_url:
            media_type, media_url = "image", image_url
        else:
            media_type, media_url = "other", None

        return {
            "date": date_str,
            "title": title,
            "explanation": explanation,
            "credit": credit,
            "url": media_url,
            "hdurl": media_url,
            "media_type": media_type,
            "service_version": "science.nasa.gov",
            "link": link,
        }

    @staticmethod
    def _is_usable_image(url: str) -> bool:
        if not url.startswith("http"):
            return False
        lowered = url.lower()
        if any(mark in lowered for mark in PLACEHOLDER_MARKS):
            return False
        return any(source in lowered for source in IMAGE_URL_SOURCES)

    @staticmethod
    def _is_usable_video(url: str) -> bool:
        """视频直链判定：只认 http(s) 且路径以已知视频扩展名结尾，拦截 logo 占位。"""
        if not url.startswith("http"):
            return False
        lowered = url.lower()
        if any(mark in lowered for mark in PLACEHOLDER_MARKS):
            return False
        path = lowered.split("?", 1)[0].split("#", 1)[0]
        return path.endswith(VIDEO_EXTENSIONS)

    # ---------- 时区工具（pubDate 兜底换算与“最新一期”探测用） ----------

    @staticmethod
    def _et_timezone():
        # APOD/IOTD 均以美东时间发布；IANA 数据缺失时由插件依赖 tzdata 提供。
        return ZoneInfo("America/New_York")

    def _expected_latest_date(self) -> str:
        # APOD 约在美东 00:05 发布当日条目，因此“美东今天”就是期望的最新日期。
        return datetime.now(self._et_timezone()).strftime("%Y-%m-%d")

    # ---------- 订阅与推送 ----------

    # 订阅名单以插件配置页为唯一数据源：指令订阅/退订即时回写配置，
    # 配置页手改保存并重载插件后同样生效。
    def _get_push_sessions(self) -> list:
        """读取订阅名单：校验容器与条目类型、去首尾空格并保序去重。"""
        push_cfg = self._ensure_dict(self.config.get("push", {}))
        raw_sessions = push_cfg.get("sessions")
        if raw_sessions is None:
            return []
        if not isinstance(raw_sessions, list):
            # 容器类型错误（如手改配置存成字符串）整体忽略，不按字符遍历。
            logger.warning(
                f"订阅名单配置不是列表，已忽略：{type(raw_sessions).__name__}"
            )
            return []
        sessions = []
        for item in raw_sessions:
            if not isinstance(item, str):
                logger.warning(f"订阅名单存在非字符串条目，已跳过：{item!r}")
                continue
            umo = item.strip()
            if umo and umo not in sessions:
                sessions.append(umo)
        return sessions

    async def _save_push_sessions(self, sessions: list) -> None:
        self._ensure_dict(self.config.setdefault("push", {}))["sessions"] = list(
            dict.fromkeys(sessions)
        )
        await self.config.save_config_async()

    async def _push_loop(self) -> None:
        try:
            if self.push_mode == "upstream":
                # 上游推送：按固定间隔巡检 feed，发现新一期立刻推送。
                while True:
                    await asyncio.sleep(self.push_interval_minutes * 60)
                    try:
                        await self._push_check()
                    except Exception as exc:
                        logger.error(f"APOD 推送巡检出错：{exc}")
            else:
                # 定时推送：每天在 push_time 推送最新一期（已推送的不重复）。
                while True:
                    delay = self._seconds_until(self.push_time)
                    logger.info(
                        f"APOD 定时推送：距下次推送约 {delay / 3600:.1f} 小时。"
                    )
                    await asyncio.sleep(delay)
                    # 拉取失败时退避重试，最多 6 次（约 1 小时），避免整期漏发。
                    for attempt in range(6):
                        try:
                            ok = await self._push_check()
                        except Exception as exc:
                            logger.error(f"APOD 定时推送出错：{exc}")
                            ok = False
                        if ok:
                            break
                        logger.warning(
                            f"APOD 定时推送拉取失败，10 分钟后重试（{attempt + 1}/6）。"
                        )
                        await asyncio.sleep(600)
        except asyncio.CancelledError:
            pass

    def _seconds_until(self, hhmm: str) -> float:
        """计算距离下一个 hhmm（服务器本地时间）的秒数。"""
        try:
            target = datetime.strptime(hhmm, "%H:%M").time()
        except ValueError:
            target = datetime.strptime("01:00", "%H:%M").time()
        now = datetime.now()
        run_at = datetime.combine(now.date(), target)
        if run_at <= now:
            run_at += timedelta(days=1)
        return (run_at - now).total_seconds()

    async def _push_check(self) -> bool:
        sessions = self._get_push_sessions()
        if not sessions:
            return True
        apod_data = await self._get_cached_data(
            "apod_cache", self._fetch_latest_apod, "_stale_apod", force_refresh=True
        )
        if apod_data is None:
            return False
        link = str(apod_data.get("link") or "")
        date = str(apod_data.get("date") or "")
        if not link:
            logger.warning("feed 条目缺少 link，跳过本次推送检查。")
            return True

        # 按会话记录已成功推送的链接：部分失败时仅补发失败会话，不重复打扰已收到者。
        # 投递检查点用严格读写：存储故障与“无记录”必须区分，否则会重复群发。
        try:
            pushed = await self._load_push_checkpoint()
        except RuntimeError as exc:
            logger.error(f"{exc}：为避免重复群发，本轮跳过推送检查。")
            return False
        pending = [umo for umo in sessions if pushed.get(umo) != link]

        last = await self.get_cache("last_pushed_apod")
        last_link = str(last.get("link") or "") if isinstance(last, dict) else ""
        last_delivered = (
            bool(last.get("delivered", True)) if isinstance(last, dict) else True
        )

        if not last_link and self.push_mode == "upstream":
            # 上游模式首次巡检只建立观察基准：旧一期不向订阅者分发。
            await self.put_cache(
                "last_pushed_apod", {"date": date, "link": link, "delivered": False}
            )
            logger.info(f"APOD 推送基准已建立：{date}")
            return True

        if self.push_mode == "upstream" and link == last_link and not last_delivered:
            # 上游观察基准尚未作为“新刊”分发：链接未变化前保持静默。
            # 定时模式不受上游观察基准影响，避免模式切换后当期漏发。
            return True

        if not pending:
            return True

        title_zh, explanation_zh = await self._translate_fields(apod_data)
        chain = await self._build_message_chain(
            apod_data,
            title_zh,
            explanation_zh,
            with_media=self.send_media,
        )
        fallback = None
        if (
            self.send_media
            and str(apod_data.get("media_type") or "").lower() == "video"
        ):
            # 视频组件发送不成功时降级为“文字+原文链接”，保证推送不丢消息。
            fallback = await self._build_message_chain(
                apod_data,
                title_zh,
                explanation_zh,
                with_media=False,
            )
        delivered = 0
        for umo in pending:
            if not await self._send_chain_with_fallback(umo, chain, fallback):
                continue
            pushed[umo] = link
            try:
                await self._save_push_checkpoint(pushed)
            except RuntimeError as exc:
                # 进度落盘失败立即中止本轮：未确认会话留待下轮补发，避免盲目重发。
                logger.error(f"{exc}：本轮推送中止。")
                return False
            delivered += 1
        await self.put_cache(
            "last_pushed_apod", {"date": date, "link": link, "delivered": True}
        )
        missed = len(pending) - delivered
        logger.info(
            f"APOD 新一期已推送：{date}，本次送达 {delivered}/{len(pending)} 个会话"
            + (f"，{missed} 个待补发。" if missed else "。")
        )
        return missed == 0

    # ---------- 图片本地缓存（可选） ----------

    def _image_cache_dir(self) -> str:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return os.path.join(
            get_astrbot_plugin_data_path(),
            "astrbot_plugin_apod_daily",
            "image_cache",
        )

    def _image_cache_path(self, url: str) -> str:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        suffix = os.path.splitext(url.split("?", 1)[0])[1][:8] or ".img"
        return os.path.join(self._image_cache_dir(), digest + suffix)

    @staticmethod
    def _image_index_names(index) -> list:
        return (
            [n for n in index if isinstance(n, str)]
            if isinstance(index, list)
            else []
        )

    def _trim_image_index(self, names: list) -> bool:
        """FIFO 淘汰，跳过保护窗口内的新文件。返回是否仍超额。"""
        now = time.time()
        while self.cache_limit > 0 and len(names) > self.cache_limit:
            victim = None
            for name in names:
                try:
                    too_fresh = (
                        now
                        - os.path.getmtime(
                            os.path.join(self._image_cache_dir(), name)
                        )
                        < IMAGE_CACHE_MIN_AGE_SECONDS
                    )
                except OSError:
                    too_fresh = False  # 文件已不在，直接移出索引
                if not too_fresh:
                    victim = name
                    break
            if victim is None:
                return True
            names.remove(victim)
            try:
                os.remove(os.path.join(self._image_cache_dir(), victim))
            except OSError:
                pass
        return False

    def _schedule_image_reconcile(self) -> None:
        """超额且全是新文件时，等保护窗口过后再回收一次。"""
        task = self._image_reconcile_task
        if task is not None and not task.done():
            return

        async def worker() -> None:
            try:
                await asyncio.sleep(IMAGE_CACHE_MIN_AGE_SECONDS + 1)
                async with self._image_index_lock:
                    names = self._image_index_names(
                        await self.get_cache("image_cache_index")
                    )
                    self._trim_image_index(names)
                    await self.put_cache("image_cache_index", names)
            except asyncio.CancelledError:
                pass

        self._image_reconcile_task = asyncio.create_task(worker())

    async def _touch_image_cache(self, path: str) -> None:
        """维护图片缓存 FIFO 索引，超出上限时删最旧的文件。"""
        async with self._image_index_lock:
            names = self._image_index_names(await self.get_cache("image_cache_index"))
            name = os.path.basename(path)
            if name in names:
                names.remove(name)
            names.append(name)
            still_over = self._trim_image_index(names)
            await self.put_cache("image_cache_index", names)
        if still_over:
            self._schedule_image_reconcile()

    async def _localize_image(self, url: str) -> Optional[str]:
        """按需把图片下载到本地缓存并返回本地路径；关闭/失败时返回 None（用 URL）。"""
        if not self.cache_images or not url:
            return None
        path = self._image_cache_path(url)
        try:
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                # 唯一临时名：同一 URL 并发下载不会互写同一个 .part。
                tmp_path = f"{path}.{os.urandom(6).hex()}.part"
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                try:
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(url) as response:
                            if response.status != 200:
                                raise RuntimeError(f"状态码={response.status}")
                            written = 0
                            with open(tmp_path, "wb") as handle:
                                async for chunk in response.content.iter_chunked(
                                    65536
                                ):
                                    handle.write(chunk)
                                    written += len(chunk)
                    if not written:
                        raise RuntimeError("空响应")
                    os.replace(tmp_path, path)
                except Exception:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    raise
            await self._touch_image_cache(path)
            return path
        except Exception as exc:
            logger.warning(f"图片缓存失败，改用原始链接：{exc}")
            return None

    async def _build_message_chain(
        self,
        apod_data: Dict[str, Any],
        title_zh: Optional[str],
        explanation_zh: Optional[str],
        with_media: bool = True,
        display_name: str = PLUGIN_DISPLAY_NAME,
    ) -> MessageChain:
        chain = []
        url = apod_data.get("hdurl") or apod_data.get("url")
        media_type = str(apod_data.get("media_type") or "").strip().lower()
        if with_media and url:
            if media_type == "video":
                chain.append(Comp.Video.fromURL(url))
            else:
                local_path = await self._localize_image(url)
                if local_path:
                    chain.append(Comp.Image.fromFileSystem(local_path))
                else:
                    chain.append(Comp.Image.fromURL(url))
        text = self._build_display_text(
            apod_data,
            title_zh,
            explanation_zh,
            display_name=display_name,
        )
        if text:
            chain.append(Comp.Plain(text))
        return MessageChain(chain=chain)

    async def _send_chain_with_fallback(
        self, umo: str, chain: MessageChain, fallback: Optional[MessageChain]
    ) -> bool:
        """发送消息链并返回是否成功；发送异常或返回 False 时，
        若提供了降级链（视频→文字+链接）则再试一次。"""
        try:
            sent = await asyncio.wait_for(
                self.context.send_message(umo, chain), timeout=60
            )
        except Exception as exc:
            logger.error(f"APOD 推送到 {umo} 失败：{exc}")
            sent = False
        if sent is False and fallback is not None:
            logger.warning(f"APOD 推送到 {umo} 的视频消息失败，降级为文字+链接。")
            try:
                sent = await asyncio.wait_for(
                    self.context.send_message(umo, fallback), timeout=60
                )
            except Exception as exc:
                logger.error(f"APOD 推送降级到 {umo} 失败：{exc}")
                sent = False
        if sent is False:
            logger.error(f"APOD 推送到 {umo} 失败：未匹配到消息平台。")
            return False
        return True

    async def terminate(self):
        logger.info("正在终止 NASA APOD 插件...")
        for attr in ("_push_task", "_prefetch_task", "_image_reconcile_task"):
            task = getattr(self, attr, None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        # 翻译 worker 持有强引用：卸载时统一取消并等待退出，
        # 避免悬挂的模型调用越过插件生命周期继续运行。
        flights = list(self._translation_flights.values())
        for flight in flights:
            flight.cancel()
        if flights:
            await asyncio.gather(*flights, return_exceptions=True)
        self._translation_flights.clear()
