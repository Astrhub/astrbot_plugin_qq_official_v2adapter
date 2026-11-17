# OneBot v11 兼容调用

同一进程内 `V2Client` 同时提供官方 QQ V2 原生调用和 aiocqhttp/OneBot v11 风格直接调用。调用方不启动 HTTP/WebSocket 服务，也不接入独立 OneBot 适配器。两种调用共享实例、代次、权限、operation ledger 和 unknown/partial 保护。

独立 OneBot HTTP/正向 WebSocket 监听、反向连接和实例网络配置已经移除。

## 双调用

```python
native = await client.qq.get_group_info(group_openid)
compat = await client.api.call_action("get_group_info", group_id=group_openid)
same = await client.call_action("get_group_info", group_id=group_openid)
via_attr = await client.get_group_info(group_id=group_openid)
```

`event.bot` 就是当前事件绑定的 `V2Client`。`event.bot.api` 与 `event.bot` 是同一实例，因此 `event.bot.api.call_action`、`event.bot.call_action` 和 `event.bot.<action>` 共用同一 dispatcher、代次、权限和 operation ledger。这是 AstrBot 进程内 Bot API（aiocqhttp 风格），不是 NapCat/Lagrange HTTP 或反向 WS 服务。

`client.qq` 返回官方原生字段。`client.api`、`client.call_action` 和标准 action 方法走同一 dispatcher，返回 OneBot OpenID 投影。ID 保持真实字符串 OpenID，不转换成数字 QQ 号。

公开原生方法可通过 `client.qq` 直接使用；兼容 dispatcher 可按公开方法名转发，不依赖独立网络 action 白名单。标准 OneBot action 只映射等价原生能力；无等价能力明确返回 `unsupported`。

`delete_msg` 进入当前实例 `management.delete_message`。只撤回账本里有唯一真实发送路由的消息，并受代次、`management_writes`、群/C2C 两分钟时限和 unknown 不重放约束。`await event.bot.api.call_action("delete_msg", message_id=event.message_obj.message_id)` 与 AstrBot aiocqhttp 插件写法相同；若该 ID 不是本机器人已发送消息，返回 `message_route_unavailable`，过期返回 `delete_expired`，不伪造成功。

## 头像与资料

`_qq_get_avatar` 是 `qq_official_v2` 的 `_qq_` 扩展 action。`size` 只能是整数 `0`、`100`、`140` 或 `640`，省略时按 `100` 处理；返回的 `verified` 固定为 `false`。

当 action 绑定到真实事件路由且未显式提供范围时，按事件场景推导 `id_kind` 与 `scope`：C2C 使用 `user_openid` 与 `c2c:<target>`，群聊使用 `member_openid` 与 `group:<target>`，频道使用 `channel_user_id` 与 `channel:<target>`，频道私信使用 `channel_user_id` 与 `dm:<target>`。没有绑定事件路由时，调用方必须显式提供真实字符串 `id_kind` 和 `scope`。

资料查询默认使用缓存和当前性标记；`get_group_member_list` 是一页或有限聚合。`get_stranger_info` 必须提供真实的 `id_kind` 与 `scope`。`no_cache=true` 时 C2C 走官方用户信息接口、群成员走官方成员接口，频道/DM 没有对应官方用户查询。`get_group_info` 与成员查询输出标准字段，已证明的 avatar、`union_openid`、`union_group_openid` 放在 `_qq`。

使用 `_qq_get_send_status` 和 `_qq_get_extension_status` 查询操作。未知、超时、部分成功不会自动重试。旧磁盘 `onebot` 字段在连接保存时剥离，加载时忽略且不启动监听。

## 不支持的能力

不提供完整好友列表、合并转发发送、跨实例路由、数字 ID 语义和业务重放。`get_friend_list`、`get_group_list`、`send_group_forward_msg`、`send_private_forward_msg`、`set_group_whole_ban` 等非等价 action 返回 `unsupported`。

`get_msg` / `get_forward_msg` 在历史库已挂载时条件支持，按实例 `settings_key`（platform_id + AppID + environment）隔离。无记录时返回 `message_not_found`、`message_expired`、`message_deleted` 或 `forward_nodes_unavailable`，不伪造正文，也不请求频道 `get_message`。频道原生 `get_message(channel_id, message_id)` 不是 OneBot `get_msg`。

对应关系见 [SDK 迁移指南](development/sdk-migration.md)。
