# ruff: noqa: UP006, UP035, UP045
import asyncio
import hashlib
import html as html_lib
import os
import random
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from html.parser import HTMLParser
from typing import Any, Dict, NamedTuple, Optional
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
#     2026-10 起站点根 feed 只含新闻条目，按 APOD 分类 feed → image-article
#     feed 兜底；全部失败时回退缓存（KV + 内存）。
#   随机一期：官网 Random APOD 同款 embed 接口，单次 POST 返回与 feed
#     content 同构的完整 HTML，不做缓存。
#   解析：两类内容共用同一套解析器，来源差异只体现为 URL 与模式常量。
# 已知坑：feed 的 XML 头畸形（两个 xmlns 声明之间缺空格），解析前需修复。
FEED_URLS = (
    "https://science.nasa.gov/category/apod/feed/",
    "https://science.nasa.gov/feed/?post_type=image-article",
)
FEED_XML_FIX_PATTERN = re.compile(r'"(?=xmlns:)')
MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}
# 全称与三字母缩写共用同一张月份表、日期解析与标题剥离规则。
MONTH_NAMES.update({name[:3]: number for name, number in MONTH_NAMES.items()})
_MONTHS = "(?:" + "|".join(MONTH_NAMES) + ")"
# feed 标题里的日期实测有两种顺序：NASA 正常输出 “2026 October 29”，
# 但 2026-10-29 那期写成了 “29 October 2026”。两种都得认，否则前缀剥不掉
# （标题带着 “APOD: 日期 –” 进译文）、日期也解析不出（只能靠 meta/slug 兜底）。
FEED_TITLE_DATE_PATTERN = re.compile(
    rf"\b(?:"
    rf"(?P<ymd_year>\d{{4}})\s+(?P<ymd_month>{_MONTHS})\s+(?P<ymd_day>\d{{1,2}})"
    rf"|(?P<dmy_day>\d{{1,2}})\s+(?P<dmy_month>{_MONTHS})\s+(?P<dmy_year>\d{{4}})"
    rf"|(?P<mdy_month>{_MONTHS})\s+(?P<mdy_day>\d{{1,2}}),?\s+(?P<mdy_year>\d{{4}})"
    rf")\b"
)
FEED_TITLE_APOD_PREFIX_PATTERN = re.compile(r"^APOD:\s*")
FEED_TITLE_SEPARATOR_PATTERN = re.compile(r"^[–—-]\s*")
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
CREDIT_PREFIX_PATTERN = re.compile(
    r"^(?:(?:Image|Video)\s+)?Credit(?:\s*&\s*Copyright)?\s*:\s*", re.IGNORECASE
)
# NASA 每日一图（IOTD）：无 feed/REST/日期元数据，只能抓两级页面；
# og:image 即主图，展示日期按美东抓取日（与官网“当日”语义一致）。
NASA_PAGE_URL = "https://www.nasa.gov/image-of-the-day/"
# 指纹核对是每次请求的前置检查，必须短：失败就直接回退缓存。
NASA_PROBE_TIMEOUT_SECONDS = 15
NASA_POST_LINK_PATTERN = re.compile(
    r"https://www\.nasa\.gov/image-article/[^\s\"'<>]+"
)
# NASA 新版文章页把阅读时长、作者也放在 <p> 里，解析正文时必须按 class 排除。
NASA_AUTHOR_CLASSES = ("article-author-name", "article-author-title")
NASA_MIN_READ_PATTERN = re.compile(r"\d+\s*min\s*read", re.IGNORECASE)
NASA_CREDIT_PREFIXES = ("image credit:", "video credit:", "photo credit:")
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
# 预缓存遇到源站未更新时的重试次数（有上限，避免死循环）。
APOD_PREFETCH_RETRY_ATTEMPTS = 3
# 图片缓存淘汰前的最小存活时间：待发送的消息可能还引用着刚写入的文件。
IMAGE_CACHE_MIN_AGE_SECONDS = 120
# 单条消息发送超时：视频上传慢，给足时间，但不能无限挂住推送循环。
SEND_TIMEOUT_SECONDS = 60
# 单条消息的发送结果：成功 / 明确失败（能确认没提交）/ 结果未知（可能已送达）。
SEND_OK = "ok"
SEND_FAILED = "failed"
SEND_UNKNOWN = "unknown"
# 本地媒体读不出来（文件没了/权限坏）：可以补发，但这份文件不能再用。
SEND_STALE = "stale"
# 只有“本地媒体/参数准备阶段”的失败能证明消息还没提交过，才值得自动补发：
#   本地图片读不出来（FileNotFoundError / PermissionError，单独归为 SEND_STALE）、
#   URL 本身非法（InvalidURL）—— 这时连上传都还没开始。
# 网络类失败（连接被拒、连接重置、超时、读响应体失败）一律按“结果未知”处理：
# QQ 官方私聊的 POST 在适配器内部重试 3 次，群聊的 POST 也有 botpy 自己的重连，
# 最后一次连接失败证明不了前面几次都没提交，重发就是重复刷同一张图/同一段文案。
_PRE_SUBMIT_ERRORS = (aiohttp.InvalidURL,)
# 本地媒体文件读不出来：说明还没上传，可以补发；但同一份文件不能反复用。
_STALE_MEDIA_ERRORS = (FileNotFoundError, PermissionError)
# 一次推送检查里，为“坏文件”重新下载的次数上限（多会话共用一份预算）。
STALE_MEDIA_RECOVERY_LIMIT = 1
# 投递检查点状态（每个会话一条）：
#   done          = 整期送达（旧格式直接存链接字符串）
#   text_pending  = 媒体已送达、正文待补
#   media_pending = 正文已送达、媒体待补
#   unknown       = 记录缺失/损坏/字段不明确：当作没送过，整条重发
CHECKPOINT_DONE = "done"
CHECKPOINT_TEXT_PENDING = "text_pending"
CHECKPOINT_MEDIA_PENDING = "media_pending"
CHECKPOINT_UNKNOWN = "unknown"
# 同一状态的两个方向，避免各处各自拼元组。
CHECKPOINT_TEXT_SENT = (CHECKPOINT_DONE, CHECKPOINT_MEDIA_PENDING)
CHECKPOINT_MEDIA_SENT = (CHECKPOINT_DONE, CHECKPOINT_TEXT_PENDING)
# 定时推送未完成时只补试一次（日更任务丢了就永久没有）；6 分钟足够躲过
# 夜间常见抖动，又不会在凌晨反复打扰。
PUSH_RETRY_DELAY_SECONDS = 360
# 日志脱敏：异常文本可能带完整 URL（query 上可能有签名）。单引号、括号都是
# 合法的 URL 字符（RFC 3986 sub-delims），在这些字符处收尾会把 ?sig=… 漏出去，
# 因此只按空白切分，再把紧贴 URL 前后的标点还原。
_URL_IN_TEXT_PATTERN = re.compile(r"https?://\S+")
_URL_WRAPPING_PUNCT = ")]}>'\",.;:："


def _redact_urls(text: str) -> str:
    """把文本里的 URL 换成 <url>，保留紧贴它的包裹标点。"""
    parts = []
    position = 0
    for match in _URL_IN_TEXT_PATTERN.finditer(text):
        url = match.group(0)
        trimmed = url.rstrip(_URL_WRAPPING_PUNCT)
        parts.append(text[position:match.start()])
        parts.append("<url>")
        parts.append(url[len(trimmed):])
        position = match.end()
    parts.append(text[position:])
    return "".join(parts)


def _is_fatal_client_status(status: int) -> bool:
    """4xx 里只有限流 429 与超时 408 值得重试，其余客户端错误重试无意义。"""
    return 400 <= status < 500 and status not in (408, 429)


def _retry_delay(attempt: int, jitter: bool = False) -> float:
    """指数退避，封顶 4 秒；图片下载额外加抖动，避免多会话同时重连。"""
    delay = min(2**attempt, 4)
    return delay + random.uniform(0, 1) if jitter else delay


def _safe_exc_text(exc: BaseException) -> str:
    """异常文本入日志前脱敏：保留异常类型与内容，只把 URL 换成占位符。"""
    return f"{type(exc).__name__}: {_redact_urls(str(exc))}"


class MediaPlan(NamedTuple):
    """本期媒体的发法：send=是否发送媒体，localize=需要本地缓存的 URL，url=原始地址。

    localize 为空串时表示无需本地文件（视频直链交由平台上传）。
    """

    send: bool
    localize: str
    url: str


class MediaReady(NamedTuple):
    """一次媒体预热的结果，由推送路径一次下载、多会话共享。

    ok=False 表示本轮没有可发的媒体（下载失败/本就无媒体）；
    path 为空表示无需本地文件（视频直链）。
    """

    ok: bool
    path: str = ""


class ChainSendReport(NamedTuple):
    """投递结果：正文与媒体分开记账。

    text_sent / media_sent = 这一项不用再发（确认送达，或结果未知按送达处理）；
    text_unknown / media_unknown = 这一项没拿到结果（超时、提交后断连），
    只影响统计口径，不再触发补发。两项分开记，避免“图片未知”把“正文明确
    失败”的补试吞掉。
    """

    text_sent: bool
    media_sent: bool
    text_unknown: bool = False
    media_unknown: bool = False
    # 媒体失败的原因是本地文件读不出来：文件必须作废后重新准备。
    media_stale: bool = False


# 文章发布时间只认这两种 meta 名，不拿更新时间冒充。
PUBLISHED_META_KEYS = ("parsely-pub-date", "article:published_time")


class _HTMLAttrParser(HTMLParser):
    """收集 og:image、发布时间 meta 与媒体 src：属性顺序、引号风格与大小写无关。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.og_image = ""
        self.og_video = ""
        self.og_description = ""
        self.published_meta = ""
        self.embed_post_id = ""
        self.img_srcs = []
        self.video_srcs = []
        self.iframe_srcs = []

    def handle_starttag(self, tag, attrs):
        attrs_map = {name.lower(): value or "" for name, value in attrs}
        if tag == "meta":
            key = attrs_map.get("property") or attrs_map.get("name")
            key = key.lower() if key else ""
            if key == "og:image" and not self.og_image:
                self.og_image = attrs_map.get("content", "")
            elif key == "og:description" and not self.og_description:
                self.og_description = attrs_map.get("content", "")
            elif key in OG_VIDEO_META_KEYS and not self.og_video:
                self.og_video = attrs_map.get("content", "")
            elif key in PUBLISHED_META_KEYS and not self.published_meta:
                self.published_meta = attrs_map.get("content", "")
        elif tag == "article" and not self.embed_post_id:
            self.embed_post_id = attrs_map.get("data-smd-embed-post-id", "")
        elif tag == "img" and attrs_map.get("src"):
            self.img_srcs.append(attrs_map["src"])
        elif tag in ("video", "source") and attrs_map.get("src"):
            # <video src> 与 <video><source src> 都算候选视频直链。
            self.video_srcs.append(attrs_map["src"])
        elif tag == "iframe" and attrs_map.get("src"):
            # 嵌入播放器（YouTube 等）不会出现在 video/source 里。
            self.iframe_srcs.append(attrs_map["src"])
IMAGE_URL_SOURCES = (
    "assets.science.nasa.gov",
    "apod.nasa.gov/apod/image",
    "images-assets.nasa.gov",
    "wp-content/uploads",
)
# 视频直链：优先看扩展名，同时兼容 og:video 的多种 property 写法。
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".ogv", ".m3u8")
OG_VIDEO_META_KEYS = ("og:video", "og:video:url", "og:video:secure_url")
# YouTube 嵌入条目：og:video 与 <iframe src> 给的是 embed 页而非视频文件，
# 只对外提供归一化后的 watch 链接。锚定开头，避免把查询参数里的链接误判。
YOUTUBE_ID_PATTERN = re.compile(
    r"^(?:https?:)?//(?:www\.|m\.)?"
    r"(?:youtube(?:-nocookie)?\.com/(?:embed/|shorts/|live/|watch\?(?:[^#]*&)?v=)"
    r"|youtu\.be/)([A-Za-z0-9_-]{6,})"
)
VIDEO_PLATFORM_YOUTUBE = "youtube"
VIDEO_PLATFORM_DIRECT = "direct"
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
# 说明末尾的「明日一图」预告：现代站点写作
# <strong>Tomorrow&#8217;s picture:</strong> 短语，与说明同在一个 <p> 里。
TOMORROW_TITLE_PATTERN = re.compile(
    r"<(?:strong|b)>\s*Tomorrow[^<]{0,30}?picture\s*:\s*</(?:strong|b)>(.*?)</p>",
    re.S | re.I,
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


@register("apod", "Cysheper", "天文每日一图插件：APOD + NASA 每日一图，订阅推送、随机、LLM 翻译与函数工具。", "1.2.2")
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
        self._nasa_probe_flight: Optional[asyncio.Task] = None
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

        # 迁移完成前清掉旧键：标记丢失时它们就不会再覆盖新设置。
        for key in ("image", "title", "explanation", "date", "provider"):
            self.config.pop(key, None)
        push_cfg.pop("translate", None)
        push_cfg.pop("send_image", None)

        if not await self._persist_config():
            logger.error("配置保存失败，迁移标记留待下次重试。")
            return
        if not await self._write_migration_marker("config_layout_v2"):
            logger.error("迁移标记写入失败，将在下次重试迁移。")
            return
        logger.info("APOD 配置已迁移到新的发送/翻译分组。")

    async def _persist_config(self) -> bool:
        """保存插件配置；返回是否确认落盘（异常与 False 都算失败）。"""
        try:
            saved = await self.config.save_config_async()
        except Exception as exc:
            logger.error(f"保存插件配置失败：{exc}")
            return False
        return saved is not False

    async def _write_migration_marker(self, key: str) -> bool:
        """迁移标记用会抛异常的 KV 写入：写失败不能被当成迁移完成。"""
        try:
            await self.put_kv_data(key, True)
        except Exception as exc:
            logger.error(f"迁移标记写入失败（{key}）：{exc}")
            return False
        return True

    async def _migrate_cache_limit(self) -> None:
        """把旧 translate_cache_limit 并入 apod_cache_limit（同样用 KV 标记只跑一次）。"""
        if await self.get_cache("config_layout_v3"):
            return
        legacy_limit = int(self.config.get("translate_cache_limit", 1000))
        # 旧键为默认值时不算用户意图，避免覆盖导入的新配置。
        if "translate_cache_limit" in self.config and legacy_limit != 1000:
            self.config["apod_cache_limit"] = max(0, legacy_limit)
        self.config.pop("translate_cache_limit", None)
        if not await self._persist_config():
            logger.error("配置保存失败，缓存上限迁移将在下次重试。")
            return
        await self._write_migration_marker("config_layout_v3")

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

            # 图片缓存（若开启）：持图片锁删文件 + 删索引，避免与下载/登记交叉。
            async with self._image_index_lock:
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
        # 指令回复的引用气泡：默认关；开启时只在本次回复首条消息上引用。
        self.quote_reply = bool(self.config.get("quote_reply", False))
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
            await self._reply_latest_apod(event)
        elif sub in ("随机", "random"):
            await self._reply_random_apod(event)
        else:
            yield event.plain_result(APOD_HELP)

    async def _send_reply_chains(
        self,
        event: AstrMessageEvent,
        chains: list[MessageChain],
        label: str = PLUGIN_DISPLAY_NAME,
    ) -> bool:
        """指令回复直接发送，绕开 result_decorate 的 At/引用/t2i/合并转发。

        result_decorate 会在 Plain/Image 链首插 At（QQ 官方会把它序列化成
        content 里的 <qqbot-at-user> 字面量），并按平台设置加引用气泡。
        直接调用 event.send 走 msg_id 被动回复，引用改由 quote_reply 控制。
        代价是同时跳过 reply_prefix、TTS、内容安全与 on_decorating_result 钩子。
        """
        sent_any = False
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)

        async def send(chain: MessageChain):
            nonlocal sent_any
            if self.quote_reply and not sent_any and message_id:
                chain = MessageChain(
                    chain=[Comp.Reply(id=str(message_id)), *chain.chain]
                )
            result = await event.send(chain)
            if result is not False:
                sent_any = True
            return result

        report = await self._send_chains(chains, send, f"{label} 指令回复")
        return report.text_sent

    async def _reply_text(
        self,
        event: AstrMessageEvent,
        text: str,
        label: str = PLUGIN_DISPLAY_NAME,
    ) -> None:
        await self._send_reply_chains(
            event, [MessageChain(chain=[Comp.Plain(text)])], label
        )
        event.stop_event()

    async def _reply_latest_apod(self, event: AstrMessageEvent):
        logger.info("正在获取最新 APOD ...")
        apod_data = await self._get_latest_apod()
        if apod_data is None:
            await self._reply_text(
                event, self.last_apod_error or "获取 APOD 数据失败，请稍后重试。"
            )
            return
        await self._reply_apod(event, apod_data)

    async def _reply_random_apod(self, event: AstrMessageEvent):
        logger.info("正在获取随机 APOD ...")
        apod_data = await self._fetch_random_apod()
        if apod_data is None:
            await self._reply_text(
                event, self.last_apod_error or "获取随机 APOD 失败，请稍后重试。"
            )
            return
        await self._reply_apod(event, apod_data)

    async def _reply_apod(
        self,
        event: AstrMessageEvent,
        apod_data: dict,
        display_name: str = PLUGIN_DISPLAY_NAME,
    ) -> None:
        """按展示配置渲染并直接发送某一期内容（apod 今日/随机、nasa 共用）。"""
        try:
            title_zh, explanation_zh, tomorrow_zh = await self._translate_fields(
                apod_data, umo=event.unified_msg_origin
            )
            # 翻译可能较慢；放在本地化之前，缩短“待发送文件被淘汰”的窗口。
            # 组链与推送/工具路径共用 _build_apod_chains：视频恒拆两条，
            # 图片跟随 is_divided，三处不再各自拼链。
            chains = await self._build_apod_chains(
                apod_data,
                title_zh,
                explanation_zh,
                tomorrow_zh=tomorrow_zh,
                display_name=display_name,
            )
            if not chains:
                await self._reply_text(
                    event, "当前配置未启用任何可返回的内容。", display_name
                )
                return
            await self._send_reply_chains(event, chains, display_name)
        finally:
            event.stop_event()

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
            await self._reply_text(
                event,
                self.last_apod_error or "获取 NASA 每日一图失败，请稍后重试。",
                NASA_DISPLAY_NAME,
            )
            return
        await self._reply_apod(event, nasa_data, display_name=NASA_DISPLAY_NAME)

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
        title_zh, explanation_zh, tomorrow_zh = await self._translate_fields(
            apod_data, umo=event.unified_msg_origin
        )
        display_name = (
            NASA_DISPLAY_NAME if label.startswith("NASA") else PLUGIN_DISPLAY_NAME
        )
        # 推送/工具路径与指令路径共用同一套拆分规则：
        # 视频恒拆两条，图片跟随 is_divided，避免正文被富媒体气泡吞掉。
        chains = await self._build_apod_chains(
            apod_data,
            title_zh,
            explanation_zh,
            tomorrow_zh=tomorrow_zh,
            display_name=display_name,
        )
        sent = await self._send_chains(
            chains,
            partial(self.context.send_message, event.unified_msg_origin),
            label=f"{label}发送",
        )
        if sent.text_unknown or sent.media_unknown:
            return (
                f"{label}发送结果未知（可能已送达，已停止重发）："
                "请以当前会话的实际消息为准。"
            )
        if not sent.text_sent:
            return f"{label}发送失败：请检查平台连接或发送日志后重试。"
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

    @staticmethod
    def _decode_checkpoint(entry: Any) -> tuple[str, str]:
        """解码一条检查点记录：返回 (状态, 记录里的链接)。

        只认显式的 media_sent True/False：字段缺失或为 null 一律当未知，
        宁可整条重发，也不把未知状态当成“已送达”而静默跳过。
        """
        if isinstance(entry, str):
            return (CHECKPOINT_DONE, entry) if entry else (CHECKPOINT_UNKNOWN, "")
        if not isinstance(entry, dict):
            return CHECKPOINT_UNKNOWN, ""
        link = str(entry.get("link") or "")
        if not link:
            return CHECKPOINT_UNKNOWN, ""
        if entry.get("media_sent") is True:
            return CHECKPOINT_TEXT_PENDING, link
        if entry.get("media_sent") is False:
            return CHECKPOINT_MEDIA_PENDING, link
        return CHECKPOINT_UNKNOWN, ""

    @staticmethod
    def _encode_checkpoint(link: str, *, media_sent: bool) -> dict:
        """编码检查点：媒体已送达（只缺正文）或正文已送达（只缺媒体）。"""
        return {"link": link, "media_sent": media_sent}

    async def _cache_translation(
        self, key: str, value: str, epoch: Optional[int] = None
    ) -> None:
        """写入翻译缓存并按配置上限淘汰最旧条目（FIFO 索引，真删除）。

        索引在任何配置下都维护：限量关闭期间的写入也要参与未来的淘汰。
        epoch 校验在索引锁内完成，避免等锁期间被清理却仍然写回。"""
        if not value:
            return
        async with self._translate_index_lock:
            if epoch is not None and epoch != self._cache_epoch:
                return
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
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """按发送/翻译配置翻译标题、说明与「明日一图」。

        一条请求可同时翻译三个字段；缓存仍按字段各自存放（键不变），
        因此开关变化不会产生版本化/迁移，最多重译。
        """
        explanation = (apod_data.get("explanation") or "").strip()
        title = (apod_data.get("title") or "").strip()
        tomorrow = (apod_data.get("tomorrow_title") or "").strip()

        want_title = bool(title) and self.translate_title and self.send_title
        want_explanation = (
            bool(explanation)
            and self.translate_explanation
            and self.send_explanation
        )
        # 「明日一图」跟着说明一起显示/翻译，不额外增加开关。
        want_tomorrow = (
            bool(tomorrow)
            and self.translate_explanation
            and self.send_explanation
        )
        if not want_title and not want_explanation and not want_tomorrow:
            logger.debug(
                "翻译未启用："
                f"title={self.translate_title}/send={self.send_title}，"
                f"explanation={self.translate_explanation}/send={self.send_explanation}"
            )
            return None, None, None

        provider_id = await self._resolve_chat_provider_id(umo)
        if not provider_id:
            logger.warning("未能解析翻译模型，本次使用原文。")
            return None, None, None

        title_zh = explanation_zh = tomorrow_zh = None
        if want_title:
            title_zh = await self.get_cache(
                self._build_translation_cache_key(provider_id, title)
            )
        if want_explanation:
            explanation_zh = await self.get_cache(
                self._build_translation_cache_key(provider_id, explanation)
            )
        if want_tomorrow:
            tomorrow_zh = await self.get_cache(
                self._build_translation_cache_key(provider_id, tomorrow)
            )

        need_title = want_title and title_zh is None
        need_explanation = want_explanation and explanation_zh is None
        need_tomorrow = want_tomorrow and tomorrow_zh is None
        if not need_title and not need_explanation and not need_tomorrow:
            return title_zh, explanation_zh, tomorrow_zh

        epoch = self._cache_epoch
        got_title, got_explanation, got_tomorrow = await self._translate_batch(
            title if need_title else None,
            explanation if need_explanation else None,
            provider_id,
            tomorrow=tomorrow if need_tomorrow else None,
        )
        # 清理（_cache_epoch 变化）之后不再写回；校验在写入锁内进行。
        if need_title and not got_title:
            logger.warning("标题翻译未返回，本期展示原文。")
        if need_explanation and not got_explanation:
            logger.warning("说明翻译未返回，本期展示原文。")
        if need_tomorrow and not got_tomorrow:
            logger.warning("「明日一图」翻译未返回，本期展示原文。")
        if need_title and got_title:
            title_zh = got_title
            await self._cache_translation(
                self._build_translation_cache_key(provider_id, title), got_title, epoch
            )
        if need_explanation and got_explanation:
            explanation_zh = got_explanation
            await self._cache_translation(
                self._build_translation_cache_key(provider_id, explanation),
                got_explanation,
                epoch,
            )
        if need_tomorrow and got_tomorrow:
            tomorrow_zh = got_tomorrow
            await self._cache_translation(
                self._build_translation_cache_key(provider_id, tomorrow),
                got_tomorrow,
                epoch,
            )
        return title_zh, explanation_zh, tomorrow_zh

    async def _translate_batch(
        self,
        title: Optional[str],
        explanation: Optional[str],
        provider_id: str,
        tomorrow: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """一条请求翻译缺失字段；single-flight 合并并发，失败降级为 (None, None, None)。"""
        request_key = self._build_translation_cache_key(
            provider_id,
            f"batch\x00{title or ''}\x00{explanation or ''}\x00{tomorrow or ''}",
        )
        flight = self._translation_flights.get(request_key)
        if flight is None:
            flight = asyncio.create_task(
                self._run_translation_flight(
                    request_key, title, explanation, provider_id, tomorrow
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
            return None, None, None

    async def _run_translation_flight(
        self,
        request_key: str,
        title: Optional[str],
        explanation: Optional[str],
        provider_id: str,
        tomorrow: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """single-flight 工作体：模型调用限时，失败与异常统一降级。"""
        try:
            return await asyncio.wait_for(
                self._translate_uncached(title, explanation, provider_id, tomorrow),
                timeout=self.timeout,
            )
        except Exception as exc:
            # 在 worker 内消费异常：即使已无等待者，
            # 也不会出现 “Future exception was never retrieved”。
            logger.warning(f"翻译执行失败，降级为原文：{exc}")
            return None, None, None
        finally:
            self._translation_flights.pop(request_key, None)

    async def _translate_uncached(
        self,
        title: Optional[str],
        explanation: Optional[str],
        provider_id: str,
        tomorrow: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """调用模型并按结构化标签解析；解析失败按配置重试，不缓存失败结果。"""
        want_title = title is not None
        want_explanation = explanation is not None
        want_tomorrow = tomorrow is not None
        system_prompt = self._build_translation_system_prompt(
            want_title, want_explanation, want_tomorrow
        )
        prompt = self._build_translation_input(title, explanation, tomorrow)

        best_title: Optional[str] = None
        best_explanation: Optional[str] = None
        best_tomorrow: Optional[str] = None
        retry_notice = self._build_translation_retry_notice(
            want_title, want_explanation, want_tomorrow
        )
        attempts = self.translate_parse_retry + 1
        for attempt in range(attempts):
            # 第 2 次起附上纠正提示：同一 prompt 纯重复收益很低，
            # 把「上次错在哪」告诉模型，重试才有意义。
            attempt_prompt = prompt if attempt == 0 else f"{prompt}\n\n{retry_notice}"
            raw = await self._call_translation_llm(
                system_prompt, attempt_prompt, provider_id
            )
            got_title, got_explanation, got_tomorrow = (
                self._parse_translation_response(
                    raw, want_title, want_explanation, want_tomorrow
                )
            )
            best_title = got_title or best_title
            best_explanation = got_explanation or best_explanation
            best_tomorrow = got_tomorrow or best_tomorrow
            title_ok = not want_title or got_title is not None
            explanation_ok = not want_explanation or got_explanation is not None
            tomorrow_ok = not want_tomorrow or got_tomorrow is not None
            if title_ok and explanation_ok and tomorrow_ok:
                return best_title, best_explanation, best_tomorrow
            logger.warning(f"翻译未按标签结构返回（{attempt + 1}/{attempts}）。")
        return best_title, best_explanation, best_tomorrow

    @staticmethod
    def _build_translation_input(
        title: Optional[str],
        explanation: Optional[str],
        tomorrow: Optional[str] = None,
    ) -> str:
        # 输入与输出使用同一组标签：模型只需把标签里的内容翻译后放回。
        parts = []
        if title is not None:
            parts.append(f"<title>{title}</title>")
        if explanation is not None:
            parts.append(f"<explanation>{explanation}</explanation>")
        if tomorrow is not None:
            parts.append(f"<tomorrow>{tomorrow}</tomorrow>")
        return chr(10).join(parts)

    @staticmethod
    def _build_translation_system_prompt(
        want_title: bool,
        want_explanation: bool,
        want_tomorrow: bool = False,
    ) -> str:
        targets = []
        if want_title:
            targets.append("标题")
        if want_explanation:
            targets.append("说明")
        if want_tomorrow:
            targets.append("「明日一图」预告")
        task = f"把标签内的{'、'.join(targets)}翻译为简体中文"

        lines = [
            "你是一位精通天文学的翻译专家。",
            f"一、遵循「信、达、雅」，{task}；",
            "二、专有名词与编号保留原形（如 67P、ISS、APOD）；",
        ]
        rule_no = 3
        if want_title:
            lines.append(
                f"{rule_no}、标题：不可译则不译，原样输出；单行，不加句号；"
            )
            rule_no += 1
        if want_tomorrow:
            lines.append(f"{rule_no}、明日一图：译成简短的中文短语，不加句号；")
            rule_no += 1
        lines.append(
            f"{rule_no}、禁止项：不要添加原文没有的 markdown、"
            "引号、列表或解释文字。"
        )
        lines.append("只输出以下结构，标签之外不要有任何字符：")
        lines.append("```")
        if want_title:
            lines.append("<title>译好的标题</title>")
        if want_explanation:
            lines.append("<explanation>译好的说明</explanation>")
        if want_tomorrow:
            lines.append("<tomorrow>译好的明日一图预告</tomorrow>")
        lines.append("```")
        lines.append("以上内容将直接用于发布，不经过二次编辑。")
        return chr(10).join(lines)

    @staticmethod
    def _build_translation_retry_notice(
        want_title: bool,
        want_explanation: bool,
        want_tomorrow: bool = False,
    ) -> str:
        tags = []
        if want_title:
            tags.append("<title>…</title>")
        if want_explanation:
            tags.append("<explanation>…</explanation>")
        if want_tomorrow:
            tags.append("<tomorrow>…</tomorrow>")
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
        cls,
        text: Optional[str],
        want_title: bool,
        want_explanation: bool,
        want_tomorrow: bool = False,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        cleaned = cls._strip_code_fence(text or "")
        if not cleaned:
            return None, None, None

        def grab(tag: str) -> Optional[str]:
            matched = re.search(
                rf"<{tag}\b[^>]*>(.*?)</{tag}>", cleaned, re.S | re.I
            )
            return matched.group(1).strip() if matched else None

        title = grab("title") if want_title else None
        explanation = grab("explanation") if want_explanation else None
        tomorrow = grab("tomorrow") if want_tomorrow else None
        # 残留未闭合标签 = 这次输出是残缺的，不能走纯文本兜底。
        leftover_tag = bool(
            re.search(r"</?(?:title|explanation|tomorrow)\b", cleaned, re.I)
        )
        # 单字段请求且模型没写完整标签 → 整体兜底；但标题必须是短单行，
        # 否则视为解析失败（防小作文从无标签路径混进来）。
        if (
            want_title
            and not want_explanation
            and not want_tomorrow
            and title is None
            and not leftover_tag
        ):
            candidate = cleaned.splitlines()[0].strip() if cleaned else ""
            if candidate and len(candidate) <= 40:
                title = candidate
        if (
            want_explanation
            and not want_title
            and not want_tomorrow
            and explanation is None
            and not leftover_tag
        ):
            explanation = cleaned
        if (
            want_tomorrow
            and not want_title
            and not want_explanation
            and tomorrow is None
            and not leftover_tag
        ):
            candidate = cleaned.splitlines()[0].strip() if cleaned else ""
            if candidate and len(candidate) <= 80:
                tomorrow = candidate
        # 空标签（<title></title>）会产生空串，splitlines()[0] 会抛 IndexError，
        # 所以取首行前先允许空值。
        if title is not None:
            first_line = next(iter(title.splitlines()), "")
            title = first_line.strip().rstrip("。.．") or None
        if explanation is not None:
            explanation = explanation.strip() or None
        if tomorrow is not None:
            first_line = next(iter(tomorrow.splitlines()), "")
            tomorrow = first_line.strip().rstrip("。．") or None
        return title, explanation, tomorrow

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
        tomorrow_zh: Optional[str] = None,
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
        video_platform = str(apod_data.get("video_platform") or "").strip().lower()
        video_url = str(apod_data.get("url") or "").strip()
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
            if video_platform == VIDEO_PLATFORM_YOUTUBE and video_url:
                # YouTube 条目没有文件直链，直接给可点击的观看地址。
                lines.append(f"▶ 本期为视频（YouTube）：{video_url}")
            else:
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
        tomorrow_title = str(apod_data.get("tomorrow_title") or "").strip()
        if show_explanation and tomorrow_title:
            tomorrow_zh = (tomorrow_zh or "").strip()
            tomorrow_line = (
                f"明日一图：{tomorrow_zh}（{tomorrow_title}）"
                if tomorrow_zh and tomorrow_zh != tomorrow_title
                else f"明日一图：{tomorrow_zh or tomorrow_title}"
            )
            text = (
                f"{text}{newline}{newline}{tomorrow_line}"
                if text
                else tomorrow_line
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
                # 源站晚发时会抓到旧一期：按 stale 间隔重试若干次，
                # 否则用户首个请求还得自己抓取 + 翻译。
                apod_data = None
                for _ in range(APOD_PREFETCH_RETRY_ATTEMPTS):
                    try:
                        apod_data = await self._get_cached_data(
                            "apod_cache",
                            self._fetch_latest_apod,
                            "_stale_apod",
                            force_refresh=True,
                        )
                    except Exception as exc:
                        logger.error(f"APOD 定时预缓存失败：{exc}")
                        apod_data = None
                    if (
                        apod_data is not None
                        and str(apod_data.get("date") or "")
                        == self._expected_latest_apod_date()
                    ):
                        break
                    logger.warning(
                        "APOD 定时预缓存：源站尚未更新，"
                        f"{APOD_STALE_RETRY_MINUTES} 分钟后重试。"
                    )
                    await asyncio.sleep(APOD_STALE_RETRY_MINUTES * 60)
                if apod_data is not None:
                    try:
                        await self._translate_fields(apod_data)
                        logger.info(
                            f"APOD 定时预缓存完成：{apod_data.get('date')}"
                        )
                    except Exception as exc:
                        logger.error(f"APOD 定时预缓存翻译失败：{exc}")
        except asyncio.CancelledError:
            pass

    async def _get_nasa(self) -> Optional[dict]:
        """NASA IOTD：记录也缓存，但**每次请求都核对列表页指纹**。

        核对只花 1 次 GET（列表页）；指纹未变直接用缓存记录，
        变了才抓文章页重新解析并覆盖写入（旧记录自然过期）。
        核对失败时退回缓存，不冒险重抓也不报错。"""
        cache_key = "nasa_cache"
        # 指纹核对放在锁外并 single-flight：并发时只探一次，不串行放大。
        latest_link = await self._coalesced_nasa_probe()
        async with self._content_lock(cache_key):
            cached = await self.get_cache(cache_key)
            if cached is not None and not self._is_valid_apod_data(cached):
                cached = None
            cached_link = str(cached.get("link") or "") if cached else ""

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
        parser = _HTMLAttrParser()
        parser.feed(content_html or "")
        # embed 底部链接是前后两期；用本期文章 ID 生成 WordPress 稳定链接，
        # 官网会重定向到正确的文章地址，避免把相邻条目当成本期。
        if not re.fullmatch(r"[1-9][0-9]*", parser.embed_post_id):
            return None
        link = f"https://science.nasa.gov/?p={parser.embed_post_id}"
        title_match = re.search(r"<h([12])\b[^>]*>(.*?)</h\1>", content_html, re.S | re.I)
        title = self._strip_html(title_match.group(2)) if title_match else ""
        if not title:
            return None
        data = self._build_apod_data(title, link, content_html)
        if data is not None:
            # 随机模式不展示「明日一图」。
            data["tomorrow_title"] = ""
        return data

    @staticmethod
    def _extract_nasa_latest_link(page_html: str) -> Optional[str]:
        """从 IOTD 列表页取第一个 image-article 链接（正文 h1 之后）。"""
        h1_index = (page_html or "").find("<h1")
        match = NASA_POST_LINK_PATTERN.search(
            page_html or "", h1_index + 1 if h1_index >= 0 else 0
        )
        return html_lib.unescape(match.group(0)) if match else None

    async def _probe_nasa_latest_link(self) -> Optional[str]:
        """每次请求的指纹核对：单次 GET 列表页，短超时、不重试，失败返回 None。"""
        timeout = aiohttp.ClientTimeout(
            total=min(self.timeout, NASA_PROBE_TIMEOUT_SECONDS)
        )
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(NASA_PAGE_URL) as response:
                    if response.status != 200:
                        logger.warning(
                            f"NASA 指纹核对失败：状态码={response.status}"
                        )
                        return None
                    page_html = await response.text()
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            logger.warning(f"NASA 指纹核对失败：{exc}")
            return None
        return self._extract_nasa_latest_link(page_html)

    async def _coalesced_nasa_probe(self) -> Optional[str]:
        """并发请求共享同一次在途指纹核对，避免每个等待者各探一次。"""
        flight = self._nasa_probe_flight
        if flight is None or flight.done():
            flight = asyncio.create_task(self._probe_nasa_latest_link())
            self._nasa_probe_flight = flight
        return await asyncio.shield(flight)

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
        video_url, _ = self._classify_video(
            html_lib.unescape(page_parser.og_video).strip()
        )
        if not image_url:
            region_parser = _HTMLAttrParser()
            region_parser.feed(region)
            for src in region_parser.img_srcs:
                candidate = html_lib.unescape(src).strip()
                if self._is_usable_image(candidate):
                    image_url = candidate
                    break
        # 说明与版权只在文章主内容容器里找，按原文顺序保留全部段落；
        # 页眉导航与页脚在容器之外，不参与拼接。
        # 正则必须限定标签名后是空白或 '>'，否则 <p[^>]*> 会误匹配
        # <path ...>（SVG 图标），把后续正文整块吞进同一个匹配。
        explanation_parts = []
        credit = ""
        for match in re.finditer(r"<p(\s[^>]*)?>(.*?)</p>", region, re.S):
            attrs = (match.group(1) or "").lower()
            text = self._strip_html(match.group(2))
            lowered = text.lower()
            if any(name in attrs for name in NASA_AUTHOR_CLASSES):
                continue
            if NASA_MIN_READ_PATTERN.fullmatch(lowered):
                continue
            if lowered.startswith(NASA_CREDIT_PREFIXES):
                if not credit:
                    credit = text.split(":", 1)[1].strip()
                continue
            if lowered.startswith("learn more") or lowered == "image article":
                continue
            explanation_parts.append(text)
        explanation = " ".join(part for part in explanation_parts if part)
        if not explanation:
            fallback = self._strip_html(
                html_lib.unescape(page_parser.og_description)
            )
            if fallback:
                logger.warning(
                    f"NASA 每日一图正文解析为空，回退 og:description：{link}"
                )
                explanation = fallback
            else:
                logger.warning(f"NASA 每日一图正文解析为空且无摘要：{link}")
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
                        if _is_fatal_client_status(response.status):
                            # 客户端错误重试无意义，直接结束本次获取。
                            break
                    else:
                        return await response.text()
            except asyncio.TimeoutError:
                logger.error(f"获取{label}超时：请求在 {self.timeout} 秒后超时。")
                self.last_apod_error = f"请求{label}超时，请稍后重试。"
            except aiohttp.ClientError as exc:
                logger.error(f"获取{label}时发生网络错误：{_safe_exc_text(exc)}")
                self.last_apod_error = f"连接{label}失败，请稍后重试。"

            if attempt < self.retry_count:
                await asyncio.sleep(_retry_delay(attempt))
        return None

    async def _fetch_from_feed(
        self, session: aiohttp.ClientSession, feed_urls=FEED_URLS
    ) -> Optional[dict]:
        """按顺序尝试各 feed 源：源站改版后根 feed 可能不再包含 APOD 条目。"""
        if isinstance(feed_urls, str):
            feed_urls = (feed_urls,)
        last_error = None
        for feed_url in feed_urls:
            raw_text = await self._fetch_text(session, feed_url, "NASA feed")
            if raw_text is None:
                last_error = self.last_apod_error
                continue
            try:
                apod_data = self._parse_feed(raw_text)
            except ElementTree.ParseError as exc:
                logger.error(f"解析 NASA feed XML 失败（{feed_url}）：{exc}")
                last_error = "NASA feed 数据格式异常，请稍后重试。"
                continue
            if apod_data is not None:
                return apod_data
            last_error = "NASA feed 中未找到有效的 APOD 条目，请稍后重试。"
        self.last_apod_error = last_error
        return None

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
            poster_url = ""
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
            thumbnail = item.find(".//{*}thumbnail")
            if thumbnail is not None:
                # media:thumbnail 嵌在 media:content 里，是视频条目的封面帧。
                poster_url = thumbnail.attrib.get("url", "")
            return self._build_apod_data(
                title_raw,
                link,
                content_html,
                pub_date_raw,
                video_url=enclosure_url,
                poster_url=poster_url,
            )
        logger.warning("NASA feed 中未找到 APOD 条目。")
        return None

    # ---------- 内容解析（feed 条目与随机内容共用） ----------

    @staticmethod
    def _strip_html(fragment: str) -> str:
        return re.sub(
            r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))
        ).strip()

    @classmethod
    def _extract_tomorrow_title(cls, content_html: str) -> str:
        """提取说明末尾的「明日一图」预告短语；没有该行时返回空串。"""
        matched = TOMORROW_TITLE_PATTERN.search(content_html or "")
        return cls._strip_html(matched.group(1)) if matched else ""

    @classmethod
    def _parse_meta_rows(cls, content_html: str) -> list:
        rows = []
        for th_html, td_html in META_ROW_PATTERN.findall(content_html or ""):
            rows.append((cls._strip_html(th_html), cls._strip_html(td_html)))
        return rows

    @staticmethod
    def _month_number(month_name: str) -> Optional[int]:
        return MONTH_NAMES.get(month_name.capitalize())

    @staticmethod
    def _date_from_title(title_raw: str) -> str:
        """解析年/月/日、日/月/年、月/日/年三种顺序，支持月份缩写。"""
        matched = FEED_TITLE_DATE_PATTERN.search(title_raw or "")
        if not matched:
            return ""
        if matched.group("ymd_year"):
            year, month_name, day = (
                matched.group("ymd_year"),
                matched.group("ymd_month"),
                matched.group("ymd_day"),
            )
        elif matched.group("dmy_year"):
            year, month_name, day = (
                matched.group("dmy_year"),
                matched.group("dmy_month"),
                matched.group("dmy_day"),
            )
        else:
            year, month_name, day = (
                matched.group("mdy_year"),
                matched.group("mdy_month"),
                matched.group("mdy_day"),
            )
        month_number = MONTH_NAMES.get(month_name or "")
        if not month_number:
            return ""
        return f"{int(year):04d}-{month_number:02d}-{int(day):02d}"

    @staticmethod
    def _strip_title_prefix(title_raw: str) -> str:
        """剥掉 “APOD: <完整日期> – ”，保留数字或月份词开头的真实标题。"""
        text = (title_raw or "").strip()
        prefix = FEED_TITLE_APOD_PREFIX_PATTERN.match(text)
        if not prefix:
            return text
        body = text[prefix.end() :].strip()
        # 开头的日期用同一个日期规则匹配（match 锚定在开头），再按结束位置裁掉。
        lead_date = FEED_TITLE_DATE_PATTERN.match(body)
        if lead_date:
            body = body[lead_date.end() :].strip()
        separator = FEED_TITLE_SEPARATOR_PATTERN.match(body)
        if separator:
            body = body[separator.end() :].strip()
        # 整串只有前缀（如 “APOD: 2026 October 29”）时保留原样，避免空标题。
        return body or text

    def _resolve_apod_date(
        self, title_raw: str, link: str, content_html: str, pub_date_raw: str
    ) -> str:
        """日期解析优先级：标题 → meta 表格 → 链接 slug → pubDate。"""
        title_date = self._date_from_title(title_raw)
        if title_date:
            return title_date

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
        poster_url: Optional[str] = None,
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

        # 「明日一图」与说明在同一个 <p> 里，但被停止标记截断，需单独提取。
        tomorrow_title = self._extract_tomorrow_title(content_html)

        content_parser = _HTMLAttrParser()
        content_parser.feed(content_html or "")
        if not image_url:
            for src in content_parser.img_srcs:
                candidate = html_lib.unescape(src).strip()
                if self._is_usable_image(candidate):
                    image_url = candidate
                    break

        # 视频候选：显式传入（feed media:content / NASA og:video）→ og:video →
        # 正文 <video>/<source> → <iframe>（YouTube 等嵌入播放器）。
        # YouTube 没有文件直链，只保留归一化后的 watch 链接。
        video_candidates = (
            video_url,
            content_parser.og_video,
            *content_parser.video_srcs,
            *content_parser.iframe_srcs,
        )
        video_url, video_platform = "", ""
        for candidate in video_candidates:
            candidate = html_lib.unescape(candidate or "").strip()
            video_url, video_platform = self._classify_video(candidate)
            if video_url:
                break

        title = self._strip_title_prefix(title_raw)
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

        # 视频条目的封面帧：feed 的 media:thumbnail 优先，其次文章页 og:image；
        # 图片条目的 url 本身就是图片，不需要额外的 poster。
        if media_type == "video":
            poster_url = (
                html_lib.unescape(poster_url or "").strip() or image_url or ""
            )
            if poster_url and not self._is_usable_image(poster_url):
                poster_url = ""
        else:
            poster_url = ""

        return {
            "date": date_str,
            "title": title,
            "explanation": explanation,
            "tomorrow_title": tomorrow_title,
            "credit": credit,
            "url": media_url,
            "hdurl": media_url,
            "media_type": media_type,
            "video_platform": video_platform,
            "poster_url": poster_url,
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

    @staticmethod
    def _youtube_watch_url(url: str) -> str:
        """把 YouTube 的 embed/shorts/youtu.be 链接还原成 watch 链接；其它返回空串。"""
        if url.startswith("//"):
            url = f"https:{url}"
        if not url.startswith(("http://", "https://")):
            return ""
        matched = YOUTUBE_ID_PATTERN.search(url)
        return (
            f"https://www.youtube.com/watch?v={matched.group(1)}" if matched else ""
        )

    @classmethod
    def _classify_video(cls, url: str):
        """候选地址 → (可用地址, 平台)：YouTube 归一化，其余按直链扩展名判定。"""
        watch_url = cls._youtube_watch_url(url or "")
        if watch_url:
            return watch_url, VIDEO_PLATFORM_YOUTUBE
        if cls._is_usable_video(url or ""):
            return url, VIDEO_PLATFORM_DIRECT
        return "", ""

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

    async def _run_push_check(self, label: str) -> bool:
        """跑一次推送检查；异常按失败处理，不让单次故障打死推送循环。"""
        try:
            return await self._push_check()
        except Exception as exc:
            logger.error(f"{label}出错：{exc}")
            return False

    async def _push_loop(self) -> None:
        try:
            if self.push_mode == "upstream":
                # 上游推送：按固定间隔巡检 feed，发现新一期立刻推送。
                while True:
                    await asyncio.sleep(self.push_interval_minutes * 60)
                    await self._run_push_check("APOD 推送巡检")
            else:
                # 定时推送：每天在 push_time 推送最新一期（已推送的不重复）。
                while True:
                    delay = self._seconds_until(self.push_time)
                    logger.info(
                        f"APOD 定时推送：距下次推送约 {delay / 3600:.1f} 小时。"
                    )
                    await asyncio.sleep(delay)
                    # 失败只补试一次：窗口短到不需要配置，也不会在凌晨反复试探。
                    if await self._run_push_check("APOD 定时推送"):
                        continue
                    logger.warning(
                        "APOD 定时推送未完成，"
                        f"{PUSH_RETRY_DELAY_SECONDS // 60} 分钟后重试一次。"
                    )
                    await asyncio.sleep(PUSH_RETRY_DELAY_SECONDS)
                    if not await self._run_push_check("APOD 定时推送"):
                        logger.error(
                            "APOD 今天的推送已放弃，可执行 /apod 今日 手动补发。"
                        )
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
        # 检查点状态由 _decode_checkpoint 统一解码（见 CHECKPOINT_* 常量）。
        def state_of(umo: str) -> str:
            """该会话在当前这一期的状态；别的期或坏记录都算未知。"""
            state, recorded = self._decode_checkpoint(pushed.get(umo))
            return state if recorded == link else CHECKPOINT_UNKNOWN

        def text_ok(umo: str) -> bool:
            return state_of(umo) in CHECKPOINT_TEXT_SENT

        def media_ok(umo: str) -> bool:
            return state_of(umo) in CHECKPOINT_MEDIA_SENT

        pending = [umo for umo in sessions if state_of(umo) != CHECKPOINT_DONE]

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

        title_zh, explanation_zh, tomorrow_zh = await self._translate_fields(apod_data)
        plan = self._media_plan(apod_data)
        media_expected = plan.send
        # 换刊：检查点只存当前一期，上一期没送完的记录跟不到新链接，只能明示放弃。
        stale = []
        for umo in pending:
            state, recorded = self._decode_checkpoint(pushed.get(umo))
            # 上一期已送达（CHECKPOINT_DONE）不算漏发，只是该发新一期了。
            if state in (CHECKPOINT_TEXT_PENDING, CHECKPOINT_MEDIA_PENDING) and (
                recorded != link
            ):
                stale.append(umo)
        if stale:
            logger.warning(
                f"APOD 有 {len(stale)} 个会话的上一期投递未完成（缺正文或缺媒体），"
                "已随新一期覆盖，不再补发。"
            )
        # 媒体预热放在会话循环外：整期只下载一次，所有会话共享结果。
        # 下载失败时 ok=False，本轮只发正文，检查点记 media_sent=False，
        # 下一轮再补媒体链（不再让平台二次下载后把整期记成已送达）。
        # 关闭图片缓存时的临时媒体统一放这个目录：一个对象持有、一处清理，
        # 走过多少次重新预热都不会漏（TemporaryDirectory 自带析构兜底，任务
        # 取消或异常退出时同样不会把文件留在盘上）。
        media = MediaReady(ok=False)
        # 本轮创建的临时媒体文件（含目录创建失败后落到系统临时目录的），
        # 统一在 finally 里清理。
        temp_files: list[str] = []
        needs_media = media_expected and any(not media_ok(umo) for umo in pending)
        media_dir: Optional[tempfile.TemporaryDirectory] = None
        if needs_media and not self.cache_images:
            try:
                media_dir = tempfile.TemporaryDirectory(prefix="apod-media-")
            except OSError as exc:
                # 目录都建不出来（如临时盘满）：同样只是“媒体准备失败”，
                # 不能让异常冲掉整轮推送。
                logger.warning(
                    f"临时媒体目录创建失败（阶段=下载）：{_safe_exc_text(exc)}"
                )

        async def build_chains(
            need_text: bool, need_media: bool
        ) -> list[MessageChain]:
            """按本轮缺的内容组链：只缺媒体时不重发正文，只缺正文时不再碰媒体。"""
            nonlocal media, media_stale, stale_recoveries
            if need_media and (
                media_stale or (media.path and not self._is_readable_file(media.path))
            ):
                if stale_recoveries >= STALE_MEDIA_RECOVERY_LIMIT:
                    # 重下预算已用完：不再重下，剩余会话本轮只发正文，留给下次检查。
                    media = MediaReady(ok=False)
                    media_stale = False
                else:
                    # 文件被淘汰/读不出来：作废后重新预热一次再发（下载层自带重试），
                    # 否则后面所有会话都只能收到正文，或者反复发送同一个坏文件。
                    # 两条发现坏文件的路径（适配器报错 / 组链前发现被淘汰）
                    # 共用同一份预算，避免多会话轮流把同一份坏文件重下多次。
                    stale_recoveries += 1
                    self._remove_file(media.path)
                    media = await self._prewarm_media(
                        apod_data, media_dir.name if media_dir else None, temp_files
                    )
                    media_stale = False
            if need_media and not need_text:
                media_chain = await self._build_media_chain(apod_data, media=media)
                return [media_chain] if media_chain.chain else []
            if not need_media:
                text = self._build_text_chain(
                    apod_data, title_zh, explanation_zh, tomorrow_zh
                )
                return [text] if text.chain else []
            return await self._build_apod_chains(
                apod_data,
                title_zh,
                explanation_zh,
                tomorrow_zh,
                media=media,
            )

        text_needed = [umo for umo in pending if not text_ok(umo)]
        media_needed = [umo for umo in pending if not media_ok(umo)] \
            if media_expected else []
        # 一轮 = 把所有待发会话试一遍；网络抖动是秒级现象，只缺图时本轮内
        # 立即转向下一轮（轮数与退避沿用 retry_count / _retry_delay），
        # 不必等外层那次 6 分钟的补试。
        unknown_text: set[str] = set()
        unknown_media: set[str] = set()
        media_stale = False
        stale_recoveries = 0
        async def run_rounds() -> bool:
            """把待发会话试一遍；返回 False 表示本轮提前中止（落盘失败）。

            媒体预热也放进来，好让它和发送共用同一个 try/finally：
            任务取消时临时目录必须当场清掉，不能等异常对象被回收。
            """
            nonlocal media, media_stale, stale_recoveries
            if needs_media:
                media = await self._prewarm_media(
                    apod_data, media_dir.name if media_dir else None, temp_files
                )
            work = list(pending)
            for attempt in range(self.retry_count + 1):
                for umo in work:
                    # 媒体开关随时可能被改：按“还缺什么”决定，而不是只看检查点。
                    need_text = not text_ok(umo)
                    need_media = media_expected and not media_ok(umo)
                    done_text = text_ok(umo)
                    done_media = media_ok(umo)
                    if need_text or need_media:
                        chains = await build_chains(need_text, need_media)
                        if not chains:
                            # 无内容可发（如媒体仍未下载成功）：留待下一轮。
                            continue
                        report = await self._send_chains(
                            chains,
                            partial(self.context.send_message, umo),
                            f"APOD 推送到 {umo}",
                        )
                        if report.text_unknown or report.media_unknown:
                            # 超时/提交后断连：平台可能已收到，重发就是重复图片/文案；
                            # 但只把未知的那一项记为已送达，另一项照常走补试。
                            parts = []
                            if report.text_unknown:
                                unknown_text.add(umo)
                                parts.append("正文")
                            if report.media_unknown:
                                unknown_media.add(umo)
                                parts.append("媒体")
                            logger.warning(
                                f"APOD 推送到 {umo} 的{"/".join(parts)}发送结果未知："
                                "不再重发该部分，请以群内消息为准。"
                            )
                        if report.media_stale:
                            # 已经确认这份文件读不了：当场作废，别让下一次检查
                            # 再撞上它；是否重新下载由重下预算决定。
                            media_stale = True
                            self._remove_file(media.path)
                        done_text = done_text or report.text_sent
                        done_media = done_media or report.media_sent
                    else:
                        # 只剩“媒体待补”但当前配置不发送媒体：收敛为整期送达，避免永久待补。
                        logger.warning(
                            f"APOD 推送到 {umo} 的待补媒体已跳过：当前配置不发送媒体。"
                        )
                    # 记账：正文与媒体分开；当前不发送媒体时不写 media_sent，
                    # 否则开启媒体后会被误认为“媒体已送达”而跳过补图。
                    if done_text and (done_media or not media_expected):
                        pushed[umo] = link
                    elif done_text:
                        logger.warning(
                            f"APOD 推送到 {umo} 正文已送达但媒体未送达，下轮只补媒体。"
                        )
                        pushed[umo] = self._encode_checkpoint(link, media_sent=False)
                    elif done_media:
                        logger.warning(
                            f"APOD 推送到 {umo} 媒体已送达但正文失败，下轮只补正文。"
                        )
                        pushed[umo] = self._encode_checkpoint(link, media_sent=True)
                    else:
                        continue
                    try:
                        await self._save_push_checkpoint(pushed)
                    except RuntimeError as exc:
                        # 进度落盘失败立即中止本轮：未确认会话留待下轮补发，避免盲目重发。
                        logger.error(f"{exc}：本轮推送中止。")
                        return False

                remaining = [umo for umo in pending if state_of(umo) != CHECKPOINT_DONE]
                if not remaining or not media_expected:
                    break
                # 只有“只剩缺图”才值得秒级重试：正文缺失一律交给 6 分钟后的补试，
                # 避免在几秒内重复发一整期正文。
                if not all(text_ok(umo) for umo in remaining):
                    break
                if attempt >= self.retry_count:
                    break
                if media_stale and stale_recoveries >= STALE_MEDIA_RECOVERY_LIMIT:
                    # 重新准备过一次仍是坏的：别再复用同一份文件，留给下一次检查。
                    self._remove_file(media.path)
                    break
                if not media.ok:
                    # 媒体没准备好：下载层已经用尽 retry_count，轮内再请求只是
                    # 把同一个失败地址（尤其 4xx）反复刷进日志。
                    break
                delay = _retry_delay(attempt, jitter=True)
                logger.warning(
                    f"APOD 有 {len(remaining)} 个会话缺图，{delay:.1f} 秒后补发"
                    f"（{attempt + 1}/{self.retry_count}）。"
                )
                await asyncio.sleep(delay)
                work = remaining

            return True

        try:
            if not await run_rounds():
                return False
        finally:
            if media_dir is not None:
                media_dir.cleanup()
            for path in temp_files:
                self._remove_file(path)

        text_delivered = sum(
            1 for umo in text_needed if text_ok(umo) and umo not in unknown_text
        )
        media_delivered = sum(
            1 for umo in media_needed if media_ok(umo) and umo not in unknown_media
        )
        await self.put_cache(
            "last_pushed_apod", {"date": date, "link": link, "delivered": True}
        )
        # 只要还有会话没到“整期送达”就需要下一轮补试；结果未知的部分已按送达
        # 记账，不会因为“另一项未知”把明确失败的那一项的补试吞掉。
        missed = sum(1 for umo in pending if state_of(umo) != CHECKPOINT_DONE)
        notes = []
        if missed:
            notes.append(f"{missed} 个会话待补发")
        unknown = unknown_text | unknown_media
        if unknown:
            # 结果未知按“可能已送达”记账、不重发，但汇总不能算成已送达。
            notes.append(f"{len(unknown)} 个会话有内容发送结果未知（未重发）")
        logger.info(
            f"APOD 本次推送 {date}：正文 {text_delivered}/{len(text_needed)}、"
            f"媒体 {media_delivered}/{len(media_needed)}"
            + ("，" + "、".join(notes) + "。" if notes else "。")
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
                    still_over = self._trim_image_index(names)
                    await self.put_cache("image_cache_index", names)
                if still_over:
                    # 又有新文件进入保护窗口：清掉自身引用后再排一轮。
                    self._image_reconcile_task = None
                    self._schedule_image_reconcile()
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

    async def _download_image(self, url: str, dest: str) -> None:
        """把单个文件下载到 dest，走 AstrBot 的 download_file。

        download_file 内部是 trust_env=True 的会话，会跟随 AstrBot 配置的
        http_proxy；插件不再自建裸 ClientSession（那样不走代理）。独立成方法
        也方便测试替身直接注入。
        """
        from astrbot.core.utils.io import download_file

        # allow_insecure_ssl_fallback=False：AstrBot 默认会在证书失败时改用
        # CERT_NONE 且不跟随 http_proxy 的裸会话重试，那比原实现放宽了证书校验。
        await asyncio.wait_for(
            download_file(url, dest, allow_insecure_ssl_fallback=False),
            timeout=self.timeout,
        )

    @staticmethod
    def _is_readable_file(path: str) -> bool:
        """能打开才算可读：FIFO 淘汰与权限损坏都能在发出去之前发现。"""
        try:
            with open(path, "rb"):
                return True
        except OSError:
            return False

    @staticmethod
    def _remove_file(path: Optional[str]) -> None:
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass

    async def _download_to_temp(
        self,
        url: str,
        directory: Optional[str] = None,
        created: Optional[list] = None,
    ) -> Optional[str]:
        """把远图先下到临时文件（不写缓存），让“读图失败”发生在提交之前。

        关闭图片缓存时平台会自己去下载远图：下载失败会把同一条消息里的正文一起
        丢掉，而且事后无法与“已提交但回执丢失”区分。先下好再交给平台，失败只
        降级为纯文字并保留媒体待补。

        建临时文件也算“媒体准备”——失败（如临时盘满）同样只降级为纯文字，
        不能让异常冲掉整轮推送。
        """
        suffix = os.path.splitext(url.split("?", 1)[0])[1][:8] or ".img"
        try:
            handle, tmp_path = tempfile.mkstemp(
                prefix="apod-media-", suffix=suffix, dir=directory
            )
            os.close(handle)
        except OSError as exc:
            logger.warning(f"临时媒体文件创建失败（阶段=下载）：{_safe_exc_text(exc)}")
            return None
        try:
            await self._download_image_with_retry(url, tmp_path)
        except asyncio.CancelledError:
            self._remove_file(tmp_path)
            raise
        except Exception as exc:
            logger.warning(f"图片预下载失败（阶段=下载）：{_safe_exc_text(exc)}")
            self._remove_file(tmp_path)
            return None
        if created is not None:
            # 目录创建失败时会退回系统临时目录，这些文件同样要登记由调用方清理。
            created.append(tmp_path)
        return tmp_path

    @classmethod
    def _is_fatal_download_error(cls, exc: BaseException) -> bool:
        """4xx（限流/超时除外）属于重试无意义的失败，本轮不必再请求同一地址。"""
        status = cls._download_http_status(exc)
        return status is not None and _is_fatal_client_status(status)

    @staticmethod
    def _download_http_status(exc: BaseException) -> Optional[int]:
        """从下载异常里取出 HTTP 状态码（AstrBot 的 DownloadFileHTTPError 只带文本）。"""
        status = getattr(exc, "status", None)
        if isinstance(status, int):
            return status
        match = re.search(r"HTTP status code:\s*(\d{3})", str(exc))
        return int(match.group(1)) if match else None

    async def _download_image_with_retry(self, url: str, dest: str) -> None:
        """下载单张图片：按 retry_count 指数退避重试（1/2/4 秒起步 + 抖动）。

        4xx（限流 429 与超时 408 除外）重试无意义，直接放弃。
        """
        for attempt in range(self.retry_count + 1):
            try:
                await self._download_image(url, dest)
                if not os.path.getsize(dest):
                    raise RuntimeError("空响应")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._is_fatal_download_error(exc) or attempt >= self.retry_count:
                    raise
                delay = _retry_delay(attempt, jitter=True)
                logger.warning(
                    f"图片下载失败（阶段=下载，第 {attempt + 1}/{self.retry_count} 次），"
                    f"{delay:.1f} 秒后重试：{_safe_exc_text(exc)}"
                )
                await asyncio.sleep(delay)

    async def _localize_image(self, url: str) -> Optional[str]:
        """按需把图片下载到本地缓存并返回本地路径；关闭或最终失败时返回 None。

        下载走 AstrBot 的 download_file（自带 trust_env=True），失败按
        retry_count 重试并退避（4xx 由下载层直接放弃）；最终失败打 WARN，
        不静默降级。
        """
        if not self.cache_images or not url:
            return None
        path = self._image_cache_path(url)
        epoch = self._cache_epoch
        if os.path.exists(path) and not self._is_readable_file(path):
            # 缓存文件坏了（权限/内容损坏）：留着只会反复失败，删掉重下。
            logger.warning("图片缓存文件不可读，重新下载。")
            self._remove_file(path)
        if os.path.exists(path):
            try:
                # 命中也要刷新 mtime：保护窗口从最近一次使用算起。
                os.utime(path)
            except OSError:
                pass
            if epoch != self._cache_epoch:
                # 期间发生过清理：不登记索引，直接用本地文件。
                return path
            await self._touch_image_cache(path)
            return path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # 唯一临时名：同一 URL 并发下载不会互写同一个 .part。
            tmp_path = f"{path}.{os.urandom(6).hex()}.part"
            try:
                await self._download_image_with_retry(url, tmp_path)
                if epoch != self._cache_epoch:
                    # 下载期间发生了清理：丢弃临时文件，绝不覆盖目标路径。
                    return None
                os.replace(tmp_path, path)
            finally:
                # CancelledError 不走 except Exception；统一在 finally 清理临时文件。
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            if epoch != self._cache_epoch:
                # 期间发生过清理：不登记索引，直接用本地文件。
                return path
            await self._touch_image_cache(path)
            return path
        except Exception as exc:
            logger.warning(f"图片缓存失败（阶段=下载）：{_safe_exc_text(exc)}")
            return None

    def _split_media(self, media_chain: MessageChain) -> bool:
        """媒体与正文是否分两条消息：图片跟随 is_divided，视频恒拆。

        视频必须恒拆：平台会把 [Video, Plain] 合成一条富媒体消息
        （msg_type=7 且 content=正文），视频气泡不渲染 content，
        正文会被平台静默丢弃，而发送层拿到的仍是成功。

        判据看媒体链里实际有没有视频组件，而不是 media_type：YouTube 条目
        的 media_type 也是 video，但媒体链里放的是封面帧（图片），不存在
        富媒体吞正文的问题，不该被恒拆。
        """
        return self.is_divided or any(
            isinstance(item, Comp.Video) for item in media_chain.chain
        )

    def _media_plan(self, apod_data: Dict[str, Any]) -> MediaPlan:
        """按本期数据决定媒体怎么发：视频直链无需本地文件，YouTube 用封面帧。"""
        url = str(apod_data.get("hdurl") or apod_data.get("url") or "").strip()
        if not self.send_media or not url:
            return MediaPlan(False, "", url)
        media_type = str(apod_data.get("media_type") or "").strip().lower()
        if media_type != "video":
            return MediaPlan(True, url, url)
        video_platform = str(apod_data.get("video_platform") or "").strip().lower()
        if video_platform != VIDEO_PLATFORM_YOUTUBE:
            return MediaPlan(True, "", url)
        poster_url = str(apod_data.get("poster_url") or "").strip()
        return (
            MediaPlan(True, poster_url, url)
            if poster_url
            else MediaPlan(False, "", url)
        )

    async def _prewarm_media(
        self,
        apod_data: Dict[str, Any],
        media_dir: Optional[str] = None,
        created: Optional[list] = None,
    ) -> MediaReady:
        """整期只下载一次媒体，返回预热结果。

        视频直链无需本地文件；下载失败返回 ok=False（本轮不发媒体）。
        关闭图片缓存时也先把远图下到临时文件（不写缓存）：平台在提交前读图，
        让它自己去下载，失败会把同一条消息里的正文一起丢掉。
        """
        plan = self._media_plan(apod_data)
        if not plan.send:
            return MediaReady(ok=False)
        if not plan.localize:
            return MediaReady(ok=True)
        if self.cache_images:
            local_path = await self._localize_image(plan.localize)
        else:
            local_path = await self._download_to_temp(
                plan.localize, media_dir, created
            )
        return MediaReady(ok=bool(local_path), path=local_path or "")

    async def _build_media_chain(
        self, apod_data: Dict[str, Any], media: Optional[MediaReady] = None
    ) -> MessageChain:
        """只含媒体组件的消息链；未开启发送媒体或无可用媒体时为空链。

        media=None 表示调用方没有预热（指令/工具路径）：自行本地化，失败
        回退远端 URL。传入 MediaReady 时按预热结果组链，ok=False 就不构造
        Image，避免平台二次下载后再报错。
        """
        plan = self._media_plan(apod_data)
        if not plan.send:
            return MessageChain(chain=[])
        if not plan.localize:
            return MessageChain(chain=[Comp.Video.fromURL(plan.url)])
        if media is None:
            local_path = await self._localize_image(plan.localize)
            if local_path:
                return MessageChain(chain=[Comp.Image.fromFileSystem(local_path)])
            return MessageChain(chain=[Comp.Image.fromURL(plan.localize)])
        if not media.ok or not media.path or not os.path.exists(media.path):
            # 预热失败或文件已被淘汰：本轮只发正文，下轮再补媒体。
            return MessageChain(chain=[])
        return MessageChain(chain=[Comp.Image.fromFileSystem(media.path)])

    def _build_text_chain(
        self,
        apod_data: Dict[str, Any],
        title_zh: Optional[str],
        explanation_zh: Optional[str],
        tomorrow_zh: Optional[str] = None,
        display_name: str = PLUGIN_DISPLAY_NAME,
    ) -> MessageChain:
        """只含正文的消息链；正文全关时为空链。"""
        text = self._build_display_text(
            apod_data,
            title_zh,
            explanation_zh,
            tomorrow_zh=tomorrow_zh,
            display_name=display_name,
        )
        return MessageChain(chain=[Comp.Plain(text)] if text else [])

    async def _build_apod_chains(
        self,
        apod_data: Dict[str, Any],
        title_zh: Optional[str],
        explanation_zh: Optional[str],
        tomorrow_zh: Optional[str] = None,
        display_name: str = PLUGIN_DISPLAY_NAME,
        media: Optional[MediaReady] = None,
    ) -> list[MessageChain]:
        """按 _split_media 返回要发送的消息链：1 条=合并，2 条=媒体+正文。

        media 默认 None（自行本地化）；推送路径传入预热结果，一次下载供所有
        会话共享。
        """
        media_chain = await self._build_media_chain(apod_data, media=media)
        text_chain = self._build_text_chain(
            apod_data,
            title_zh,
            explanation_zh,
            tomorrow_zh=tomorrow_zh,
            display_name=display_name,
        )
        if not self._split_media(media_chain):
            merged = MessageChain(chain=[*media_chain.chain, *text_chain.chain])
            return [merged] if merged.chain else []
        chains = []
        if media_chain.chain:
            chains.append(media_chain)
        if text_chain.chain:
            chains.append(text_chain)
        return chains

    @staticmethod
    async def _send_chain(
        send: Callable[[MessageChain], Awaitable[Any]],
        chain: MessageChain,
        label: str,
        stage: str = "发送",
    ) -> str:
        """发一条链，返回 SEND_OK / SEND_FAILED / SEND_UNKNOWN。

        只有本地准备阶段的失败（见 _PRE_SUBMIT_ERRORS）与接口返回 False 能证明
        消息没提交过，才允许自动补发；网络类失败、超时、提交后断连都可能已经
        送达，一律按“结果未知”处理，不再重发。
        """
        try:
            sent = await asyncio.wait_for(send(chain), timeout=SEND_TIMEOUT_SECONDS)
        except _STALE_MEDIA_ERRORS as exc:
            logger.error(
                f"{label}失败（阶段={stage}，本地媒体不可用）：{_safe_exc_text(exc)}"
            )
            return SEND_STALE
        except _PRE_SUBMIT_ERRORS as exc:
            logger.error(
                f"{label}失败（阶段={stage}，未提交）：{_safe_exc_text(exc)}"
            )
            return SEND_FAILED
        except asyncio.TimeoutError:
            logger.error(f"{label}超时（阶段={stage}）：结果未知，不自动重发。")
            return SEND_UNKNOWN
        except Exception as exc:
            logger.error(
                f"{label}异常（阶段={stage}）：结果未知，不自动重发。"
                f"{_safe_exc_text(exc)}"
            )
            return SEND_UNKNOWN
        if sent is False:
            logger.error(f"{label}失败（阶段={stage}）：发送接口返回 False。")
            return SEND_FAILED
        return SEND_OK

    async def _send_chains(
        self,
        chains: list[MessageChain],
        send: Callable[[MessageChain], Awaitable[Any]],
        label: str = "APOD",
    ) -> ChainSendReport:
        """逐条发送消息链；媒体那条失败时舍媒体保正文（合并链补发纯文字）。

        正文失败一律视为未送达：宁可下轮补发（媒体已送达时只补正文），
        也不能让说明文本静默丢掉；但超时属于“结果未知”，按已送达处理，
        不重发（重发就是重复刷图/刷文案）。带媒体的链失败更可能是上传失败。
        """
        if not chains:
            return ChainSendReport(False, False)
        degraded = False
        text_sent = False
        media_sent = False
        text_unknown = False
        media_unknown = False
        media_stale = False

        def current_report(text_done: bool) -> ChainSendReport:
            """按当前累计状态出报告：所有出口共用，避免漏带某个标记。"""
            return ChainSendReport(
                text_done, media_sent, text_unknown, media_unknown, media_stale
            )

        for index, chain in enumerate(chains):
            has_media = any(
                isinstance(item, (Comp.Image, Comp.Video)) for item in chain.chain
            )
            has_text = any(isinstance(item, Comp.Plain) for item in chain.chain)
            outcome = await self._send_chain(
                send, chain, label, "上传" if has_media else "发送"
            )
            if outcome == SEND_UNKNOWN:
                # 不确定是否已送达：按送达记账（不再重发），但逐项标记未知。
                if has_media:
                    media_unknown = media_sent = True
                if has_text:
                    text_unknown = text_sent = True
                continue
            if outcome == SEND_STALE and has_media:
                # 本地媒体不可用（一定没提交）：记下这笔，让调用方作废文件重来。
                media_stale = True
            if outcome == SEND_OK:
                media_sent = media_sent or has_media
                text_sent = text_sent or has_text
                continue
            if not has_media:
                return current_report(text_sent)
            fallback = MessageChain(
                chain=[item for item in chain.chain if isinstance(item, Comp.Plain)]
            )
            if fallback.chain:
                outcome = await self._send_chain(send, fallback, label)
                if outcome == SEND_UNKNOWN:
                    # 降级正文也可能已经被平台接收：按已送达记账，不再重发一次。
                    text_unknown = text_sent = True
                elif outcome != SEND_OK:
                    return current_report(False)
                else:
                    text_sent = True
            elif index == len(chains) - 1:
                return current_report(text_sent)
            degraded = True
        if degraded:
            logger.warning(f"{label}已降级为纯文字。")
        return current_report(text_sent)

    async def terminate(self):
        logger.info("正在终止 NASA APOD 插件...")
        for attr in (
            "_push_task",
            "_prefetch_task",
            "_image_reconcile_task",
            "_nasa_probe_flight",
        ):
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
