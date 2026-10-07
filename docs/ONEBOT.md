# OneBot 网络入口

这是 **OneBot v11 风格的 OpenID 子集**，由 `v2/network.py` 提供。它复用本适配器的发送核心、资料缓存和操作账本，不是独立的 QQ 账号实现。

## 启用

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

网络写 action 必须满足全局 OneBot 开关、实例 `enable`、实例 `writes=true` 和 QQ 当次权限；具名管理、撤回、黑名单写另外需要插件 `management_writes=true`；托管面板同步另受 `remote_menu_sync` 控制。不会自动开启反向 WS、HTTP POST 事件、quick operations 或跨实例路由。

## 事件与回复

群/C2C 消息以 `post_type: "qq_event"` 扩展提供；ID 保留为真实字符串 OpenID，不补造标准 v11 的数字字段。频道事件只提供 QQ 扩展视图。`_qq_reply_context` 必须来自本实例已观察的真实群/C2C 来源，群约 5 分钟、C2C 保守 60 分钟，重载/容量淘汰后失效。

使用 `_qq_get_send_status` 和 `_qq_get_extension_status` 查询操作。未知、超时、部分成功不会自动重试；网络事件不会提供历史回放或入群申请 flag。

资料查询默认使用缓存和当前性标记；`get_group_member_list` 是一页或有限聚合，不等于永远完整的群名单。`get_stranger_info` 必须提供真实的 `id_kind` 与 `scope`，历史资料会标记为 `profile_cache`，不会伪造聊天时间和消息 ID。

完整的进程内 OneBot action 仍可通过 `event.bot.api`/`client.call_action` 使用，但原生 `.qq` 的全部具名写接口不会自动暴露为网络 action。
