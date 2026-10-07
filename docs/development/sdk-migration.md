# 从旧 OneBot 网络入口迁移到进程内 SDK

独立 OneBot HTTP/正向 WebSocket 入口处于计划废弃阶段。当前版本继续保留它来承接已有外部客户端；新插件和新集成直接调用进程内 SDK。这样可以复用当前事件的机器人身份、场景、权限检查和发送账本，也不需要为插件单独配置监听端口和 token。

## 接入 SDK

事件处理函数从 `event.bot.qq` 获取与当前事件绑定的原生视图：

```python
async def on_message(event):
    await event.send("收到")

    member = await event.bot.qq.get_group_member_info(
        event.route.target,
        "成员 OpenID",
    )
```

`event.send(...)` 会沿用当前事件的回复来源。后台任务或需要明确目标时，调用 `event.bot.qq.send(scene, target, message)`：

```python
result = await event.bot.qq.send(
    "group",
    "群 OpenID",
    "主动消息",
    operation_id="job-42",
)
state = event.bot.qq.send_status(result["operation_id"])
```

`event.send(...)` 负责宿主回复，当前没有返回发送结果；需要 `operation_id`、消息 ID 或状态时，使用 `event.bot.qq.send(...)`，或者从 `event.get_extra("qq_send_result")` 读取宿主保存的结果。

`scene` 使用 `group`、`c2c`、`channel` 或 `dm`。目标使用 QQ 官方 OpenID、频道 ID 或子频道 ID，统一按字符串处理。消息可以是字符串、AstrBot `MessageChain` 或组件列表；OneBot 的 `auto_escape` 只属于兼容调用参数。

## 常用调用对应关系

| 旧网络 action | 进程内 SDK |
| --- | --- |
| `send_group_msg` | 事件内使用 `await event.send(message)`；明确目标时使用 `await event.bot.qq.send("group", group_openid, message)` |
| `send_private_msg` | `await event.bot.qq.send("c2c", user_openid, message)` |
| `send_msg` | 根据 `group_id` 或 `user_id` 选择 `group` 或 `c2c`，再调用 `event.bot.qq.send(...)` |
| `get_login_info` | `await event.bot.qq.login_info()` |
| `get_group_info` | `await event.bot.qq.get_group_info(group_openid)` |
| `get_group_member_info` | `await event.bot.qq.get_group_member_info(group_openid, member_openid)` |
| `get_group_member_list` | 获取一页使用 `get_group_member_list(group_openid, cursor)`；完整遍历使用 `async for row in event.bot.qq.iter_group_members(group_openid)` |
| `get_stranger_info` | 群成员资料使用 `await event.bot.qq.profiles.get_member(group_openid, member_openid, mode="prefer_cache")`；需要新鲜数据时使用 `mode="refresh"` |
| `set_group_ban` | 使用 `await event.bot.qq.group_ban(group, member, duration, operation_id=...)`；原生管理方法见 [SDK 指南](sdk.md) |
| `set_group_kick` | 使用 `await event.bot.qq.group_kick(group, [member], blacklist=..., operation_id=...)` |
| `set_group_add_request` | 使用 `await event.bot.qq.approve(flag, approve=..., reason=..., blacklist=...)`；需要原生参数时使用 `approve_group_join_request(...)` |
| `delete_msg` | 使用 `await event.bot.qq.delete_message(message_id, operation_id=...)`，或按场景调用对应 `recall_*` 方法 |
| `_qq_get_send_status` | `event.bot.qq.send_status(operation_id)` |
| `_qq_get_extension_status` | `event.bot.qq.extension_status(operation_id)` |
| `_qq_get_capabilities` | `event.bot.capabilities()` |

频道和 DM 使用 SDK 中对应的 `post_message`、`post_dms` 等原生方法。它们保留 QQ 官方字段和响应结构，能力受当前账号权限与场景限制。

## 事件订阅

原生事件通过 `event.bot.qq.events.subscribe(...)` 订阅。回调使用异步函数，并为订阅绑定插件 owner：

```python
subscription = event.bot.qq.events.subscribe(
    ["MESSAGE_CREATE"],
    callback=handle_native_event,
    owner=self,
)
```

订阅最多保留 64 条排队事件和 16 MiB 数据。队列或字节预算超限时会收到 `subscription_gap`，回调异常或超时会被记录，订阅会继续运行。插件卸载或平台重载后，旧 owner 和旧 SDK 视图会失效，重新获取当前事件的视图即可。

## 兼容调用的边界

`client.api`、`client.call_action` 和 `event.bot.api` 仍保留在进程内，用于已有插件逐步迁移。新代码使用具名 SDK 方法；兼容 action 的参数和返回值仍按 OneBot OpenID 子集处理。两类调用共用发送账本、权限门控和来源检查，调用入口不会扩大 QQ 账号的权限。

独立网络入口的 `writes`、`management_writes`、token、HTTP 路由和 WebSocket peer 限制只影响旧外部客户端。迁移完成后可以关闭全局 `onebot_network_enabled`，并移除实例中的 `onebot` 监听配置。

## 生命周期

平台重载会生成新的代次。不要跨重载缓存 `event.bot`、`event.bot.qq`、`NativeView`、事件订阅或上传句柄。需要把操作 ID 和 owner 固定到一次调用时，使用：

```python
view = event.bot.qq.with_options(
    operation_id="moderation-42",
    owner=self,
)
result = await view.set_group_member_blacklist(
    "群 OpenID",
    "add",
    ["成员 OpenID"],
)
```

发送、管理和媒体操作遇到 `unknown` 或 `partial` 时，先用原 `operation_id` 查询状态，再由业务决定后续动作。更换 ID 会创建新的操作，可能造成重复执行。
