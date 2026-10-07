# QQ V2 SDK 快速参考

本文件保留为旧链接的兼容入口，完整内容已迁移到 [开发者 SDK 指南](development/sdk.md)。

最小用法：

```python
client = event.bot
member = await client.qq.get_group_member_info(event.route.target, "成员 OpenID")
await event.send("你好")
```

`client.qq` 是官方 V2 原生具名 API；`client.api` 是 OneBot v11 风格 OpenID 投影。同名方法的返回结构和能力边界不同，详见 [SDK 指南](development/sdk.md)、[覆盖台账](SDK_COVERAGE.md) 和 [OneBot 说明](ONEBOT.md)。
