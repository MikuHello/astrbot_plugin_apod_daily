天文每日一图（APOD）与 NASA 每日一图（IOTD）AstrBot 插件：RSS/官网页面可靠数据源，支持订阅推送、随机、LLM 翻译与函数工具。

> 本项目修改自 [Cysheper/astrbot_plugin_Astronomy_Picture_of_the_Day-APOD](https://github.com/Cysheper/astrbot_plugin_Astronomy_Picture_of_the_Day-APOD)，遵循 AGPL-3.0 许可证发布，感谢原作者 Cysheper。

## 与上游的主要差异

- 数据源切换为 science.nasa.gov 官方 RSS（上游 api.nasa.gov 在迁移期不稳定）
- 新增订阅推送：定时推送（默认每天 01:00）/ 上游更新即推 两种模式
- 新增随机天文图：复用官网 Random APOD 同款 embed 接口，一次请求取全量内容
- 翻译未配置 provider 时自动使用 AstrBot 默认对话模型；标题与说明一次请求结构化返回
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

## 许可证

[AGPL-3.0](./LICENSE)
