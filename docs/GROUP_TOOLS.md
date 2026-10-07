# QQ V2 群工具

插件注册 13 个 `qq_v2_` 前缀的宿主工具。工具从触发消息绑定机器人、群、会话和操作者；模型不能选择其他实例或群。

| 工具 | 说明 |
| --- | --- |
| `get_group` | 当前群名称、人数和来源 |
| `get_member` | 查询一个显式成员 OpenID，可选择刷新 |
| `list_members` | 官方游标单页，返回 `next_cursor` |
| `find_known_members` | 搜索历史已知资料，最多 20 项 |
| `list_mutes` | 查询当前群禁言 |
| `mute_members` | 1–20 个成员，`duration_seconds` 0 表示解除 |
| `kick_members` | 1–20 个成员，可选加入黑名单 |
| `list_blacklist` | 官方游标单页 |
| `change_blacklist` | `add`/`del`，1–20 个显式成员 |
| `list_join_requests` | 查询当前待处理申请和真实 flag |
| `approve_join_request` | 使用上一步的 flag 明确批准/拒绝 |
| `list_join_strategies` | 查询机器人级入群策略 |
| `get_operation_status` | 查询同会话、同操作者提交的写操作 |

成员、禁言、黑名单和申请数据的权限与当前性以 QQ 响应为准。分页必须继续使用官方 cursor；unknown/partial 只能查询原操作状态。QQ 群管理员权限与 AstrBot 管理员权限是两套独立权限。
