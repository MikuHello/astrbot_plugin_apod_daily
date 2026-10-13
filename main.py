# ruff: noqa: UP006, UP035, UP045
import asyncio
import hashlib
import html as html_lib
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional
from xml.etree import ElementTree

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import PermissionType
from astrbot.api.provider import ProviderType
from astrbot.api.star import Context, Star, register

# NASA APOD 主站已从 apod.nasa.gov 迁移到 science.nasa.gov（WordPress 站点），
# 迁移期内 api.nasa.gov 频繁出现 500、超时和占位数据（标题/图片变成
# "NASA Science" logo）。因此本插件的数据层改为：
#   唯一数据源：science.nasa.gov 站点 RSS feed —— 无需 API Key、无速率限制，
#     每天约 UTC 04:05 发布标题形如 "APOD: 2026 October 11 – ..." 的条目，
#     content:encoded 内含当日图片与解说。
#   容错策略：feed 拉取失败时回退缓存（KV + 内存），不做第二数据源。
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
PLACEHOLDER_MARKS = ("nasa-logo", "/themes/")
IMAGE_URL_SOURCES = (
    "assets.science.nasa.gov",
    "apod.nasa.gov/apod/image",
    "wp-content/uploads",
)
EXPLANATION_STOP_MARKERS = (
    "APOD's main NASA site is moving",
    "Tomorrow's picture",
    "Yesterday's Image",
    "Tomorrow's Image",
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
PLUGIN_DISPLAY_NAME = "APOD 每日天文图"
CREDIT_LABEL = "图片来源与版权"


@register("apod", "Cysheper", "APOD 每日天文图插件：RSS 数据源 + 订阅推送 + LLM 翻译。", "1.0.4")
class APOD(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        self.config = config
        self.context = context
        self.last_apod_error: Optional[str] = None
        self._stale_apod: Optional[dict] = None

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
    def _build_translation_cache_key(text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"translate_cache:{digest}"

    async def initialize(self):
        logger.info("正在初始化 NASA APOD 插件...")

        # 读取配置；嵌套配置统一规范成字典。RSS 数据源无需任何密钥。
        self.image = self.config.get("image", True)
        self.explanation = self._ensure_dict(self.config.get("explanation", {}))
        self.title = self._ensure_dict(self.config.get("title", {}))
        self.provider = self.config.get("provider", "")
        self.date = self._ensure_dict(self.config.get("date", {}))
        self.is_divided = self.config.get("is_divided", True)
        self.timeout = max(1, int(self.config.get("timeout", 120)))
        self.retry_count = max(0, int(self.config.get("retry_count", 2)))

        # 翻译模型：优先用插件配置的 provider，未配置时回落到 AstrBot 默认对话模型。
        push_cfg = self._ensure_dict(self.config.get("push", {}))
        self.push_enabled = bool(push_cfg.get("enabled", False))
        self.push_mode = str(push_cfg.get("mode", "scheduled")).strip().lower()
        if self.push_mode not in ("scheduled", "upstream"):
            self.push_mode = "scheduled"
        self.push_time = str(push_cfg.get("push_time", "01:00")).strip() or "01:00"
        self.push_interval_minutes = max(5, int(push_cfg.get("interval_minutes", 30)))
        self.push_translate = bool(push_cfg.get("translate", True))
        self.push_send_image = bool(push_cfg.get("send_image", True))
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

    @filter.command("apod")
    async def apod(self, event: AstrMessageEvent):
        """获取 NASA 每日天文图（订阅管理请使用 /apod管理）"""
        logger.info("正在获取 NASA 每日天文图片...")

        # 如果今天的 APOD 缓存仍然有效，就优先复用缓存数据。
        apod_data = await self.get_cache_apod()
        if not apod_data:
            yield event.plain_result(
                self.last_apod_error or "获取 APOD 数据失败，请稍后重试。"
            )
            return

        # 当要求返回图片时，如果当天 APOD 不是图片类型，就直接视为失败。
        if apod_data.get("media_type") != "image" and self.image:
            yield event.plain_result("今天的 APOD 不是图片类型，请稍后再试。")
            return

        url = apod_data.get("hdurl") or apod_data.get("url")

        # 图片模式还要求 NASA 返回可用的图片链接。
        if not url and self.image:
            yield event.plain_result("获取 APOD 图片链接失败，请稍后重试。")
            return

        title_zh, explanation_zh = await self._translate_fields(apod_data)
        display_text = self._build_display_text(apod_data, title_zh, explanation_zh)

        if self.is_divided:
            # 图片一条、文本一条。
            has_output = False
            if self.image and url:
                has_output = True
                yield event.image_result(url)
            if display_text:
                has_output = True
                yield event.plain_result(display_text)
            if not has_output:
                yield event.plain_result("当前配置未启用任何可返回的内容。")
            return

        # 图 + 文聚合为一条消息链。
        chain = []
        if self.image and url:
            chain.append(Comp.Image.fromURL(url))
        if display_text:
            chain.append(Comp.Plain(display_text))
        if not chain:
            yield event.plain_result("当前配置未启用任何可返回的内容。")
            return

        yield event.chain_result(chain)

    @filter.command("apod管理")
    @filter.permission_type(PermissionType.GROUP_ADMIN)
    async def apod_manage(self, event: AstrMessageEvent):
        """APOD 推送订阅管理：订阅 / 退订 / 列表"""
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

        if sub in ("list", "列表", "名单"):
            sessions = await self._get_push_sessions()
            if sessions:
                yield event.plain_result(
                    "当前订阅 APOD 推送的会话："
                    + chr(10)
                    + chr(10).join(sessions)
                )
            else:
                yield event.plain_result("还没有会话订阅推送喵～")
            return

        # 裸指令：输出支持的子命令与填写方式。
        usage = (
            "APOD 订阅管理指令："
            + chr(10)
            + "/apod管理 订阅 —— 订阅本会话的每日推送"
            + chr(10)
            + "/apod管理 退订 —— 退订本会话的每日推送"
            + chr(10)
            + "/apod管理 列表 —— 查看订阅名单"
        )
        yield event.plain_result(usage)

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

    async def _translate_fields(
        self, apod_data: Dict[str, Any], include_hidden: bool = False
    ) -> tuple[Optional[str], Optional[str]]:
        """按配置翻译标题与解说，带哈希键缓存；命令与推送共用。

        include_hidden=True（推送场景）时忽略 is_show，总是翻译全部内容。
        """
        title_zh, explanation_zh = None, None
        try:
            explanation = apod_data.get("explanation")
            title = apod_data.get("title")
            if (
                explanation
                and self.explanation.get("is_translate")
                and (include_hidden or self.explanation.get("is_show"))
            ):
                explanation_cache_key = self._build_translation_cache_key(explanation)
                cached_translation = await self.get_cache(explanation_cache_key)
                if cached_translation is not None:
                    explanation_zh = cached_translation
                else:
                    explanation_zh = await self.translate_explanation(
                        explanation.strip(), self.provider
                    )
                    await self.put_cache(explanation_cache_key, explanation_zh)

            if (
                title
                and self.title.get("is_translate")
                and (include_hidden or self.title.get("is_show"))
            ):
                title_cache_key = self._build_translation_cache_key(title)
                cached_translation = await self.get_cache(title_cache_key)
                if cached_translation is not None:
                    title_zh = cached_translation
                else:
                    title_zh = await self.translate_explanation(
                        title.strip(), self.provider
                    )
                    await self.put_cache(title_cache_key, title_zh)
        except Exception as exc:
            logger.error(f"翻译 APOD 内容失败：{exc}")
        return title_zh, explanation_zh

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
        include_hidden: bool = False,
    ) -> str:
        """生成“品牌头 + 标题/日期/版权 + 空行 + 说明”的正文。"""
        show_title = include_hidden or self.title.get("is_show")
        show_date = include_hidden or self.date.get("is_show")
        show_explanation = include_hidden or self.explanation.get("is_show")

        title = (title_zh or apod_data.get("title") or "").strip()
        title_raw = str(apod_data.get("title") or "").strip()
        date_cn = self._format_date_cn(str(apod_data.get("date") or "").strip())
        credit = str(apod_data.get("credit") or "").strip()
        explanation = (explanation_zh or apod_data.get("explanation") or "").strip()

        newline = chr(10)
        lines = [PLUGIN_DISPLAY_NAME]
        if show_title and title:
            # 有译文且与原文不同时，展示“译文（原文）”；否则只展示其一
            if title_zh and title_raw and title_zh.strip() != title_raw:
                lines.append(f"标题：{title_zh.strip()}（{title_raw}）")
            else:
                lines.append(f"标题：{title}")
        if show_date and date_cn:
            lines.append(f"日期：{date_cn}")
        if credit:
            lines.append(f"{CREDIT_LABEL}：{credit}")
        text = newline.join(lines)
        if show_explanation and explanation:
            text = (
                f"{text}{newline}{newline}说明：{explanation}"
                if text
                else f"说明：{explanation}"
            )
        return text

    def _resolve_chat_provider_id(self) -> Optional[str]:
        """优先插件配置的 provider，否则回落到 AstrBot 当前对话模型。"""
        if self.provider:
            return self.provider
        try:
            prov = self.context.provider_manager.get_using_provider(
                ProviderType.CHAT_COMPLETION
            )
            pid = None
            if prov is not None:
                # Provider 实例没有 .id 属性，id 存在 provider_config 里
                pid = getattr(prov, "id", None)
                if not pid:
                    pid = (getattr(prov, "provider_config", None) or {}).get("id")
            if pid:
                return pid
            logger.warning("未能从提供商实例解析到 id。")
        except Exception as exc:
            logger.warning(f"获取 AstrBot 当前对话模型失败：{exc}")
        return None

    async def translate_explanation(
        self, explanation: str, provider_id: Optional[str] = None
    ) -> str:
        provider_id = provider_id or self._resolve_chat_provider_id()
        if not provider_id:
            raise RuntimeError(
                "翻译不可用：插件未配置 provider，且 AstrBot 没有可用的默认对话模型。"
            )
        logger.info(f"正在翻译 APOD 内容（provider={provider_id}）...")
        llm_resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=(
                "You are a professional astronomy translator. Translate the input text "
                "into Simplified Chinese accurately, and do not add any extra explanation."
            ),
            prompt=explanation,
        )
        return llm_resp.completion_text

    # 把“拉取并写入缓存”的逻辑集中到这里，保证所有刷新路径行为一致。
    async def _fetch_and_cache_apod(self) -> Optional[dict]:
        logger.info("正在获取最新 APOD 数据...")
        apod_data = await self.get_apod()
        if apod_data is None:
            return None

        if not self._is_valid_apod_data(apod_data):
            logger.error(f"获取到的 APOD 数据无效：{apod_data}")
            return None

        apod_data["retrieved_at"] = datetime.now().isoformat()
        self._stale_apod = apod_data
        await self.put_cache("apod_cache", apod_data)
        return apod_data

    # 只有在缓存结构有效且时间未过期时，才真正使用缓存。
    async def get_cache_apod(self) -> Optional[dict]:
        apod_data = await self.get_cache("apod_cache")
        if apod_data is None:
            fresh = await self._fetch_and_cache_apod()
            if fresh is not None:
                return fresh
            # 没有任何缓存且全部数据源失败时，尝试用内存中的旧数据兜底。
            return self._stale_apod

        if not self._is_valid_apod_data(apod_data):
            logger.warning("缓存中的 APOD 数据无效，正在刷新缓存。")
            return await self._fetch_and_cache_apod()

        retrieved_at_raw = apod_data.get("retrieved_at")
        if not retrieved_at_raw:
            logger.info("缓存中的 APOD 数据缺少 retrieved_at，正在刷新缓存。")
            return await self._fetch_and_cache_apod()

        try:
            retrieved_at = datetime.fromisoformat(retrieved_at_raw)
        except (TypeError, ValueError):
            logger.warning("缓存中的 APOD 时间格式无效，正在刷新缓存。")
            return await self._fetch_and_cache_apod()

        now = datetime.now()
        age = (now - retrieved_at).total_seconds()
        # 新鲜度判断以“美东今天”为基准（APOD 约在美东 00:05 发布当日条目），
        # 与服务器本地时区无关：
        #   - 12 小时硬 TTL；
        #   - 缓存超过 1 小时且日期落后于最新一期时提前刷新，
        #     避免新一期发布后最长要等满 12 小时才能刷出来。
        expired = age > 12 * 3600
        if not expired and age > 3600:
            cached_date = str(apod_data.get("date") or "")
            if cached_date and cached_date != self._expected_latest_date():
                logger.info("缓存日期落后于最新一期，正在刷新缓存。")
                expired = True
        if expired:
            logger.info("缓存中的 APOD 数据已过期，正在刷新缓存。")
            fresh = await self._fetch_and_cache_apod()
            if fresh is not None:
                return fresh
            # 刷新失败时回退到过期缓存，避免直接对用户报错。
            logger.warning("刷新失败，回退使用过期的 APOD 缓存。")
        return apod_data

    # ---------- 数据获取主入口 ----------
    # 唯一数据源：science.nasa.gov 站点 RSS（无需 token、无速率限制）。
    # 拉取失败时由缓存层兜底（KV / 内存 stale），不做第二数据源。

    async def get_apod(self) -> Optional[dict]:
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

    # ---------- RSS feed 拉取与解析 ----------

    async def _fetch_from_feed(self, session: aiohttp.ClientSession) -> Optional[dict]:
        for attempt in range(self.retry_count + 1):
            try:
                async with session.get(FEED_URL) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(
                            f"获取 NASA feed 失败：状态码={response.status}，"
                            f"响应内容={error_text[:300]}"
                        )
                        self.last_apod_error = "NASA feed 暂时不可用，请稍后重试。"
                    else:
                        apod_data = self._parse_feed(await response.text())
                        if apod_data is not None:
                            return apod_data
                        self.last_apod_error = (
                            "NASA feed 中未找到有效的 APOD 条目，请稍后重试。"
                        )
            except asyncio.TimeoutError:
                logger.error(f"获取 NASA feed 超时：请求在 {self.timeout} 秒后超时。")
                self.last_apod_error = "请求 NASA feed 超时，请稍后重试。"
            except aiohttp.ClientError as exc:
                logger.error(f"获取 NASA feed 时发生网络错误：{exc}")
                self.last_apod_error = "连接 NASA feed 时发生网络错误，请稍后重试。"
            except ElementTree.ParseError as exc:
                logger.error(f"解析 NASA feed XML 失败：{exc}")
                self.last_apod_error = "NASA feed 数据格式异常，请稍后重试。"

            if attempt < self.retry_count:
                await asyncio.sleep(min(2**attempt, 4))
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
            for child in item:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "encoded":
                    content_html = child.text or ""
                elif tag == "pubDate":
                    pub_date_raw = (child.text or "").strip()
            return self._build_apod_from_feed_item(
                title_raw, link, pub_date_raw, content_html
            )
        logger.warning("NASA feed 中未找到 APOD 条目。")
        return None

    def _build_apod_from_feed_item(
        self,
        title_raw: str,
        link: str,
        pub_date_raw: str,
        content_html: str,
    ) -> Optional[dict]:
        date_matched = FEED_TITLE_DATE_PATTERN.search(title_raw)
        if date_matched:
            year, month_name, day = date_matched.groups()
            date_str = f"{int(year):04d}-{MONTH_NAMES[month_name]:02d}-{int(day):02d}"
        elif pub_date_raw:
            try:
                date_str = (
                    parsedate_to_datetime(pub_date_raw)
                    .astimezone(self._et_timezone())
                    .strftime("%Y-%m-%d")
                )
            except (TypeError, ValueError):
                date_str = ""
        else:
            date_str = ""

        title_matched = FEED_TITLE_PREFIX_PATTERN.match(title_raw)
        title = title_matched.group(1).strip() if title_matched else title_raw

        image_url = ""
        for src in re.findall(r'<img[^>]+src="([^"]+)"', content_html):
            candidate = html_lib.unescape(src).strip()
            if self._is_usable_image(candidate):
                image_url = candidate
                break

        plain_text = re.sub(
            r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", content_html))
        ).strip()
        # 原文的撇号是 U+2019（’），标记匹配前统一映射为 ASCII 撇号；
        # 替换是等长的，可直接用同一索引回切原文，不影响展示内容。
        normalized_text = plain_text.replace("\u2019", "'")
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
            explanation = plain_text

        credit = ""
        for credit_marker in ("Credit & Copyright:", "Image Credit:", "Credit:"):
            marker_index = normalized_text.find(credit_marker)
            if marker_index < 0:
                continue
            start = marker_index + len(credit_marker)
            stop_index = len(normalized_text)
            for stop_marker in CREDIT_STOP_MARKERS:
                found = normalized_text.find(stop_marker, start)
                if found >= 0:
                    stop_index = min(stop_index, found)
            credit = plain_text[start:stop_index].strip()
            break

        if not date_str or not explanation:
            logger.error(
                f"NASA feed 条目信息不完整：date={date_str!r}，link={link!r}"
            )
            return None

        return {
            "date": date_str,
            "title": title,
            "explanation": explanation,
            "credit": credit,
            "url": image_url,
            "hdurl": image_url,
            "media_type": "image" if image_url else "other",
            "service_version": "rss-science.nasa.gov",
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

    # ---------- 时区工具（pubDate 兜底换算与“最新一期”探测用） ----------

    @staticmethod
    def _et_timezone():
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo("America/New_York")
        except Exception:
            # Windows 未安装 tzdata 包时的降级方案：按美国夏令时规则估算偏移。
            now_utc = datetime.now(timezone.utc)

            def nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
                first_day = datetime(year, month, 1, tzinfo=timezone.utc)
                shift = (weekday - first_day.weekday()) % 7
                return first_day + timedelta(days=shift + 7 * (n - 1))

            dst_start = nth_weekday(now_utc.year, 3, 6, 2)  # 3 月第二个周日
            dst_end = nth_weekday(now_utc.year, 11, 6, 1)  # 11 月第一个周日
            offset_hours = -4 if dst_start <= now_utc < dst_end else -5
            return timezone(timedelta(hours=offset_hours))

    def _expected_latest_date(self) -> str:
        # APOD 约在美东 00:05 发布当日条目，因此“美东今天”就是期望的最新日期。
        return datetime.now(self._et_timezone()).strftime("%Y-%m-%d")

    # ---------- 订阅与推送 ----------

    # 订阅名单以插件配置页为唯一数据源：指令订阅/退订即时回写配置，
    # 配置页手改保存并重载插件后同样生效。
    def _get_push_sessions(self) -> list:
        push_cfg = self._ensure_dict(self.config.get("push", {}))
        return [str(s) for s in (push_cfg.get("sessions") or []) if str(s).strip()]

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
        apod_data = await self.get_apod()
        if apod_data is None:
            return False
        link = str(apod_data.get("link") or "")
        date = str(apod_data.get("date") or "")
        if not link:
            logger.warning("feed 条目缺少 link，跳过本次推送检查。")
            return True
        last = await self.get_cache("last_pushed_apod")
        last_link = str(last.get("link") or "") if isinstance(last, dict) else ""
        if link == last_link:
            return True
        if not last_link:
            # 首次建立基准，不把启动时的旧一期当作“新刊”群发。
            await self.put_cache("last_pushed_apod", {"date": date, "link": link})
            logger.info(f"APOD 推送基准已建立：{date}")
            return True
        title_zh, explanation_zh = (None, None)
        if self.push_translate:
            title_zh, explanation_zh = await self._translate_fields(
                apod_data, include_hidden=True
            )
        chain = self._build_push_chain(apod_data, title_zh, explanation_zh)
        delivered = 0
        for umo in sessions:
            try:
                if await self.context.send_message(umo, chain):
                    delivered += 1
            except Exception as exc:
                logger.error(f"APOD 推送到 {umo} 失败：{exc}")
        logger.info(
            f"APOD 新一期已推送：{date}，送达 {delivered}/{len(sessions)} 个会话。"
        )
        await self.put_cache("last_pushed_apod", {"date": date, "link": link})
        return True

    def _build_push_chain(
        self,
        apod_data: Dict[str, Any],
        title_zh: Optional[str],
        explanation_zh: Optional[str],
    ) -> MessageChain:
        chain = []
        url = apod_data.get("hdurl") or apod_data.get("url")
        if self.push_send_image and url:
            chain.append(Comp.Image.fromURL(url))
        text = self._build_display_text(
            apod_data, title_zh, explanation_zh, include_hidden=True
        )
        if text:
            chain.append(Comp.Plain(text))
        return MessageChain(chain=chain)

    async def terminate(self):
        logger.info("正在终止 NASA APOD 插件...")
        if getattr(self, "_push_task", None):
            self._push_task.cancel()
            self._push_task = None
