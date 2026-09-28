# QQ V2 群工具

插件注册 13 个 `qq_v2_` 前缀的宿主 LLM 工具，使用触发消息绑定的机器人、群和当前会话；模型不能指定其他平台或群。

| 工具后缀 | 输入及返回范围 |
| --- | --- |
| `get_group` | 当前群名称/人数及来源。 |
| `get_member` | 显式成员 OpenID、可选 `refresh`；通常缓存优先，刷新请求 QQ，保留 `_qq` 来源/当前性。 |
| `list_members` | 官方游标单页；返回 `next_cursor` 与 `non_atomic=true`。 |
| `find_known_members` | 名称或 OpenID 片段的历史候选，最多 20 项。 |
| `list_mutes` | 当前群禁言查询，最多呈现 100 条。 |
| `mute_members` | 1–20 个显式 OpenID、`duration_seconds`（0 为解禁）。 |
| `kick_members` | 1–20 个显式 OpenID、可选 `blacklist`。 |
| `list_blacklist` | 官方游标单页，含 `next_cursor`。 |
| `change_blacklist` | `op=add/del`，1–20 个显式 OpenID；不会暗中先踢人。 |
| `list_join_requests` | 官方待处理申请单页，返回当前有效 `flag`；重新查询可能更新旧 flag。 |
| `approve_join_request` | 上一项的真实待处理 `flag`、明确批准/拒绝与有限拒绝理由。 |
| `list_join_strategies` | 机器人级策略官方游标单页，返回 `strategies` 摘要及 `next_cursor`。 |
| `get_operation_status` | 仅本群同会话、同操作者提交的工具写操作；未知/部分成功只查状态。 |

