# 从 OneBot 兼容调用迁到原生 SDK

同一进程内 `V2Client` 同时提供 OneBot v11 风格投影和官方 V2 原生字段。前者方便已有 `call_action` / `event.bot.api` 代码；后者提供原生字段和更细的生命周期控制。两者复用当前事件身份、场景、权限检查和发送账本，调用方不需要另写 QQ V2 适配器，也不启动独立网络服务。


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

适配器内部没有事件对象时，从实例调用 `instance.get_client()` 获取 `V2Client`，再使用 `client.qq` 访问根视图。根视图适合明确指定场景和目标的主动调用；宿主聊天回复继续使用事件绑定的 `event.bot`。

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

| OneBot 兼容 action | 进程内 SDK |
| --- | --- |
| `send_group_msg` | 事件内使用 `await event.send(message)`；明确目标时使用 `await event.bot.qq.send("group", group_openid, message)` |
| `send_private_msg` | `await event.bot.qq.send("c2c", user_openid, message)` |
| `send_msg` | 根据 `group_id` 或 `user_id` 选择 `group` 或 `c2c`，再调用 `event.bot.qq.send(...)` |
| `get_login_info` | `await event.bot.qq.me()` |
| `get_msg` / `get_forward_msg` | 查询本实例 `history.sqlite3`；无记录返回 `message_not_found` / `message_expired` / `message_deleted` / `forward_nodes_unavailable`，不请求频道 `get_message`。`get_forward_msg` 只返回真实本地节点 |
| `get_group_info` | `await event.bot.qq.get_group_info(group_openid)` |
| `get_group_member_info` | `await event.bot.qq.get_group_member_info(group_openid, member_openid)` |
| `get_group_member_list` | 获取一页使用 `await event.bot.qq.get_group_member_list(group_openid, cursor)`；完整遍历使用 `async for row in event.bot.qq.iter_group_members(group_openid)` |
| `get_stranger_info` | C2C 使用 `await event.bot.qq.get_user_info(user_openid)`；群成员使用 `await event.bot.qq.profiles.get_member(group_openid, member_openid, mode="prefer_cache")`，需要新鲜数据时使用 `mode="refresh"` 或 `get_group_member_info` |
| `set_group_ban` | 使用 `await event.bot.qq.group_ban(group, member, duration, operation_id=...)`；原生管理方法见 [SDK 指南](sdk.md) |
| `set_group_kick` | 使用 `await event.bot.qq.group_kick(group, [member], operation_id=...)`；官方已删除移除时拉黑，`blacklist=True` 或 `reject_add_request=true` 返回 `unsupported`，拉黑改用 `change_blacklist` |
| `set_group_add_request` | 使用 `await event.bot.qq.approve(flag, approve=..., reason=..., blacklist=...)`；需要原生参数时使用 `approve_group_join_request(...)` |
| `delete_msg` | 进程内 `await event.bot.api.call_action("delete_msg", message_id=event.message_obj.message_id)` 进入 `management.delete_message`；原生用 `event.bot.qq.delete_message(...)` 或按场景 `recall_*`。只撤回已发送且未过期的消息 |
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

订阅回调收到的 `notice.client` 是绑定 owner 的根视图，`notice.reply_context` 只在实时聊天事件中携带回复来源。它不会自动回复收到的消息；需要宿主回复时使用 `V2MessageEvent` 的 `event.bot` 和 `event.send(...)`。

## 兼容调用的边界

`event.bot` 是 `V2Client`，`event.bot.api` 是同一实例。`client.api`、`client.call_action` 和 `event.bot.api` 是 AstrBot 进程内 Bot API 兼容面，不是 NapCat/Lagrange HTTP 入口。新代码使用具名 SDK 方法；兼容 action 的参数和返回值仍按 OneBot OpenID 子集处理。两类调用共用发送账本、权限门控和来源检查。独立 OneBot HTTP/WebSocket 入口已经移除。

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
