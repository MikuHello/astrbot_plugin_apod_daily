NASA 每日天文图（APOD）AstrBot 插件：基于官方 RSS 的可靠数据源，支持订阅推送、定时分发与 LLM 翻译。

> 本项目修改自 [Cysheper/astrbot_plugin_Astronomy_Picture_of_the_Day-APOD](https://github.com/Cysheper/astrbot_plugin_Astronomy_Picture_of_the_Day-APOD)，遵循 AGPL-3.0 许可证发布，感谢原作者 Cysheper。

## 与上游的主要差异

- 数据源切换为 science.nasa.gov 官方 RSS（上游 api.nasa.gov 在迁移期不稳定）
- 新增订阅推送：定时推送（默认每天 01:00）/ 上游更新即推 两种模式
- 翻译未配置 provider 时自动使用 AstrBot 默认对话模型
- 修复缓存时区问题；移除已失效的 NASA API 依赖

## 指令

| 指令 | 说明 |
| --- | --- |
| /apod | 获取最新一期 |
| /apod管理 | 查看订阅管理用法 |
| /apod管理 订阅 | 订阅本会话的每日推送 |
| /apod管理 退订 | 退订本会话的每日推送 |
| /apod管理 列表 | 查看订阅名单 |

## 配置

详见插件配置页（推送模式 / 定时时间 / 轮询周期 / 推送名单 / 翻译等）。

## 许可证

[AGPL-3.0](./LICENSE)
