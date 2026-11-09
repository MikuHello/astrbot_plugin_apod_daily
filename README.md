天文每日一图（APOD）与 NASA 每日一图（IOTD）AstrBot 插件：RSS/官网页面可靠数据源，支持订阅推送、随机、LLM 翻译与函数工具。

> 本项目修改自 [Cysheper/astrbot_plugin_Astronomy_Picture_of_the_Day-APOD](https://github.com/Cysheper/astrbot_plugin_Astronomy_Picture_of_the_Day-APOD)，遵循 AGPL-3.0 许可证发布，感谢原作者 Cysheper。

## 与上游的主要差异

- 数据源切换为 science.nasa.gov 官方 RSS（上游 api.nasa.gov 在迁移期不稳定）
- 新增订阅推送：定时推送（默认每天 01:00）/ 上游更新即推 两种模式
- 新增随机天文图：复用官网 Random APOD 同款 embed 接口，一次请求取全量内容
- 翻译未配置 provider 时自动使用 AstrBot 默认对话模型；标题、说明与「明日一图」一次请求结构化返回
- 显示说明末尾的「明日一图」预告（随说明一起翻译，缺译文时保留原文）
- 支持视频型条目（解析 `<video>`/`og:video`/feed enclosure/YouTube iframe；YouTube 条目发观看链接，平台不支持时降级为文字+链接）
- 修复缓存时区问题；移除已失效的 NASA API 依赖

## 指令

| 指令 | 说明 |
| --- | --- |
| /apod | 输出帮助（子指令一览） |
| /apod 今日 | 获取最新一期天文每日一图 |
| /apod 随机 | 随机欣赏一期历史天文每日一图 |
| /nasa | 获取 NASA 每日一图（Image of the Day，抓取官网当日页面） |
| /apod管理 | 查看订阅管理用法 |
| /apod管理 订阅 | 订阅本会话的每日推送（名单即时同步到插件配置页） |
| /apod管理 退订 | 退订本会话的每日推送 |
| /apod管理 清理 | 清空翻译与内容缓存（不影响推送记录） |

> `/apod管理` 默认仅群聊限管理员可用，可在 AstrBot WebUI「指令管理」中调整权限；订阅名单统一在插件配置页「推送/订阅名单 (UMO)」维护。
> 已注册 `send_apod_today` / `send_apod_random` LLM 函数工具，开启函数调用后模型可在对话中主动发送今日/随机天文图。

## 配置

详见插件配置页（推送模式 / 定时时间 / 轮询周期 / 推送名单 / 翻译与缓存上限等）。

- 图片按 `is_divided` 决定是否与正文分开发送；视频组件始终单独发送，避免平台吞掉正文。YouTube 封面按图片处理。
- 媒体发送失败时尝试保留纯文字正文；订阅推送按正文/媒体分开记账：媒体成功而正文失败时下轮只补正文，正文成功而媒体失败时下轮只补媒体（补发只针对当前一期，换刊后上一期的待补媒体不再补发并会记日志；关闭「发送媒体」时待补媒体直接跳过）。
- 图片缓存走 AstrBot 的下载工具（跟随其 `http_proxy`，证书失败不降级为不校验连接），失败按 `retry_count` 重试（1/2/4 秒 + 抖动）后再降级为纯文字。
- `quote_reply` 默认关闭，开启后仅首条成功发送的指令回复带引用。内容指令直接发送消息，跳过 AstrBot 的回复装饰流程（包括全局 At、引用、文字转图、回复前缀、TTS、内容安全与装饰钩子）；帮助和订阅管理仍走框架回复流程。

## 许可证

[AGPL-3.0](./LICENSE)
