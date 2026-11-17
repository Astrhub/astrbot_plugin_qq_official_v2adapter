# OneBot v11 兼容入口

这是由 `v2/network.py` 提供的 OneBot v11 风格 OpenID 翻译层。它把 QQ 官方事件和接口转换为标准 OneBot 字段，并在 `_qq` 中保留无法映射的官方字段；现有 OneBot 客户端无需另写 QQ V2 适配器。原生 SDK 与 OneBot 入口都是正式公共入口，新插件可按需要选择。

## 启用入口

全局插件设置 `onebot_network_enabled=true` 后，在对应平台实例中配置：

```json
{
  "enable": true,
  "host": "127.0.0.1",
  "port": 5700,
  "token": "a-dedicated-token-with-at-least-16-chars",
  "writes": false
}
```

`host` 必须是明确的 IPv4/IPv6 地址，端口范围为 1–65535，token 是 16–512 个可打印 ASCII 字符，不能与 QQ AppSecret 相同。token 通过 `Authorization: Bearer ...` 或 `access_token` 查询参数提供；两者同时出现会拒绝。

## 路由与限制

| 路由 | 用途 |
| --- | --- |
| `GET/POST /:action` | HTTP action |
| WebSocket `/api` | 执行动作 |
| WebSocket `/event` | 只观察事件，入站数据不会执行 quick operation |
| WebSocket `/` | 兼容 mixed 模式 |

每实例最多 16 个 WS peer、32 个活动 HTTP/WS 请求；单条 WS 同时只执行一个 action。单帧 256 KiB，JSON 深度 20、节点 4096，action 超时 120 秒；peer 队列 32 帧/1 MiB，实例总排队 4 MiB。超过边界会明确拒绝或关闭连接。

网络写 action 必须满足全局 OneBot 开关、实例 `enable`、实例 `writes=true` 和 QQ 当次权限；具名管理、撤回、黑名单写另外需要插件 `management_writes=true`；托管面板同步另受 `remote_menu_sync` 控制。不会自动开启反向 WS、HTTP POST 事件、quick operations 或跨实例路由。网络 action 不自动复制全部原生接口：等价能力按需映射，非等价能力明确返回 unsupported；进程内 `event.bot.api`、`client.api` 和 `client.call_action` 可直接复用已实现的新原生能力。

## QQ 头像扩展

`_qq_get_avatar` 是 `qq_official_v2` 提供的 `_qq_` 扩展 action，不是跨适配器通用的 OneBot action。`size` 只能是整数 `0`、`100`、`140` 或 `640`，省略时按 `100` 处理；返回的 `verified` 固定为 `false`，表示插件不会把头像 URL 视为已验证的身份凭据。

当 action 绑定到真实事件路由且未显式提供范围时，插件按事件场景推导 `id_kind` 与 `scope`：C2C 使用 `user_openid` 与 `c2c:<target>`，群聊使用 `member_openid` 与 `group:<target>`，频道使用 `channel_user_id` 与 `channel:<target>`，频道私信使用 `channel_user_id` 与 `dm:<target>`。没有绑定事件路由时，调用方必须显式提供真实字符串 `id_kind` 和 `scope`；OpenID 不转换为 QQ 号码，也不能跨 Bot/AppID 使用。

已从当前事件观察到的用户头像可返回 `source=chat_event` 且 `size=null`；指定尺寸或没有可复用的事件头像时返回 QQ URL，`source=qqapp`。群头像必须使用群目标，返回 `kind=group`。头像 URL 只表示来源，不改变身份、权限或目标归属。

## 事件与回复

群/C2C 消息以 `post_type: "qq_event"` 扩展提供；ID 保留为真实字符串 OpenID，不补造标准 v11 的数字字段。频道事件只提供 QQ 扩展视图。`_qq_reply_context` 必须来自本实例已观察的真实群/C2C 来源，群约 5 分钟、C2C 保守 60 分钟，重载/容量淘汰后失效。

使用 `_qq_get_send_status` 和 `_qq_get_extension_status` 查询操作。未知、超时、部分成功不会自动重试；网络事件不会提供历史回放或入群申请 flag。

资料查询默认使用缓存和当前性标记；`get_group_member_list` 是一页或有限聚合，不等于永远完整的群名单。`get_stranger_info` 必须提供真实的 `id_kind` 与 `scope`，历史资料会标记为 `profile_cache`，不会伪造聊天时间和消息 ID。`no_cache=true` 时 C2C 走官方用户信息接口、群成员走官方成员接口，频道/DM 没有对应官方用户查询。`get_group_info` 与成员查询输出标准字段，已证明的 avatar、`union_openid`、`union_group_openid` 放在 `_qq`。网络消息 `sender` 自动填 nickname，头像和 union 标识同样只出现在 `_qq`。群成员增减和机器人进退群投影为 `qq_type=notice`，只输出能证明的字段，不与 name-only `retained` 重复，也不伪造数字 QQ 号、operator 或成功语义。

迁移中的旧代码可以继续通过 `event.bot.api`、`client.api` 或 `client.call_action` 使用进程内兼容 action。原生 `.qq` 的具名接口不会自动暴露为网络 action；对应关系见 [SDK 迁移指南](development/sdk-migration.md)。
