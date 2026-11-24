# QQ V2 群工具

插件注册 13 个 `qq_v2_` 前缀的宿主工具。工具从触发消息绑定机器人、群、会话和操作者；模型不能选择其他实例或群。工具接受来源的期限沿用群消息 60 分钟被动窗口，超过后返回 `tool_source_expired`，不会发起新的管理调用。

| 工具 | 说明 |
| --- | --- |
| `get_group` | 当前群名称、人数和来源 |
| `get_member` | 查询一个显式成员 OpenID，可选择刷新 |
| `list_members` | 官方游标单页，返回 `next_cursor` |
| `find_known_members` | 搜索历史已知资料，最多 20 项 |
| `list_mutes` | 查询当前群禁言 |
| `mute_members` | 1–20 个成员，`duration_seconds` 0 表示解除 |
| `kick_members` | 1–20 个成员；官方已删除移除时拉黑，需要拉黑请随后调用 `change_blacklist` |
| `list_blacklist` | 官方游标单页 |
| `change_blacklist` | `add`/`del`，1–20 个显式成员 |
| `list_join_requests` | 查询当前待处理申请和真实 flag |
| `approve_join_request` | 使用上一步的 flag 明确批准/拒绝 |
| `list_join_strategies` | 查询机器人级入群策略 |
| `get_operation_status` | 查询同会话、同操作者提交的写操作 |

成员、禁言、黑名单和申请数据的权限与当前性以 QQ 响应为准。分页必须继续使用官方 cursor；unknown/partial 只能查询原操作状态。QQ 群管理员权限与 AstrBot 管理员权限是两套独立权限。机器人自身群角色不作为写操作的前置校验，管理写直接交给 QQ 判定；QQ 返回 11282/11253 时清掉该群 `bot_state` 缓存并重新读取一次，写请求本身不重放。`bot_state` 读取仍按群缓存 300 秒，11253 权限缺失缓存 60 秒。
