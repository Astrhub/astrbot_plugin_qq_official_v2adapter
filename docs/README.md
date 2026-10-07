# 开发者文档

这组文档描述当前源码实现的边界、生命周期和扩展契约。它们面向：

- 为 AstrBot 编写 QQ 插件的开发者；
- 维护本适配器、修复传输/投递问题的贡献者；
- 需要使用原生 V2 SDK、事件订阅或迁移旧网络客户端的集成开发者。

## 推荐阅读顺序

1. [架构与数据流](development/architecture.md)：理解传输、持久收件、宿主投递和发送账本的关系。
2. [连接、代次与配置](development/lifecycle-and-configuration.md)：理解实例身份、重载、WebSocket/Webhook 和 Pages 设置。
3. [SDK 与事件订阅](development/sdk.md) 与 [事件模型](development/events.md)：使用 `event.bot.qq`、`event.send` 和原生事件观察 API。
4. [发送、媒体与幂等](development/delivery-and-media.md)：处理回复来源、流式、上传、unknown 和操作状态。
5. [扩展、按钮与群工具](development/extensions.md)：接入按钮回调、托管面板和 13 个受控群工具。
6. [持久化与恢复](development/persistence-and-recovery.md)：理解账本、迁移和 unknown 的恢复边界。
7. [从旧 OneBot 网络入口迁移到 SDK](development/sdk-migration.md)：把进程外网络调用改为进程内 SDK。
8. [旧 OneBot 说明](ONEBOT.md)：维护已有外部客户端时查看网络入口和兼容边界。
9. [测试与 CI](development/testing.md)：在与 CI 相同的隔离环境中运行回归测试。

## 文档与源码的对应关系

| 主题 | 主要源码 |
| --- | --- |
| 插件注册与实例 | `main.py`, `v2/adapter.py`, `v2/models.py` |
| 认证与连接 | `v2/connection_config.py`, `v2/connections.py`, `v2/transport/` |
| 入站收件与宿主投递 | `v2/transport/inbox.py`, `v2/messaging/delivery.py`, `v2/messaging/convert.py` |
| 发送与来源 | `v2/messaging/outbound.py`, `v2/messaging/reply.py`, `v2/messaging/store.py` |
| 原生 SDK 与事件 | `v2/sdk/`, `v2/client.py`, `v2/event.py` |
| 媒体与扩展 | `v2/media/`, `v2/extensions/`, `v2/panels.py` |
| 旧 OneBot 网络入口 | `v2/network.py`, `v2/network_config.py` |
| 测试入口 | `tests/`, `scripts/test-isolated.sh`, `.github/workflows/tests.yml` |

## 稳定性边界

`event.bot.qq`、`event.send`、`event.bot.qq.events` 和 `event.qq` 是插件的主要公共入口。`client.api` 与 `client.call_action` 保留给已有兼容代码；独立 OneBot 网络入口处于计划废弃阶段。`v2/` 内部类、SQLite 表、HTTP 路由实现细节不是跨版本稳定 API；修改它们时必须同步测试、迁移说明和错误契约。
