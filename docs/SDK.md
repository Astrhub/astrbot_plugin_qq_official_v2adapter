# QQ V2 SDK 快速参考

本文件保留为旧链接的兼容入口，完整内容已迁移到 [开发者 SDK 指南](development/sdk.md)。

最小用法：

```python
client = event.bot
member = await client.qq.get_group_member_info(event.route.target, "成员 OpenID")
await event.send("你好")
```

`client.qq` 是官方 V2 原生具名 API，属于插件的主要扩展入口。`client.api` 与 `client.call_action` 仍提供进程内兼容调用，方便已有代码迁移；新代码直接使用具名 SDK 方法。独立 OneBot 网络入口处于计划废弃阶段，迁移对应关系见 [SDK 迁移指南](development/sdk-migration.md)，网络边界见 [旧 OneBot 说明](ONEBOT.md)。
