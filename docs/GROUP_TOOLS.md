# QQ V2 群工具

插件注册 13 个 `qq_v2_` 前缀的宿主 LLM 工具，使用触发消息绑定的机器人、群和当前会话；模型不能指定其他平台或群。不自动将原生 SDK 全部暴露给模型。宿主禁用工具/插件会话过滤、旧实例、过期来源或非真实群消息均不可调用。

| 工具后缀 | 输入及返回范围 |
| --- | --- |
| `get_group` | 当前群名称/人数及来源；QQ 不可读时只返回事件中可确认的群 ID。 |
| `get_member` | 显式成员 OpenID、可选 `refresh`；通常缓存优先，刷新请求 QQ，保留 `_qq` 来源/当前性。 |
| `list_members` | 官方游标单页；返回 `next_cursor` 与 `non_atomic=true`，不冒充当前原子完整名单。 |
| `find_known_members` | 名称或 OpenID 片段的历史候选，最多 20 项；重名不自动选人。 |
| `list_mutes` | 当前群禁言查询，最多呈现 100 条、明确截断。 |
| `mute_members` | 1–20 个显式 OpenID、`duration_seconds`（0 为解禁）。 |
| `kick_members` | 1–20 个显式 OpenID、可选 `blacklist`；部分移除/拉黑失败不重放已成功步骤。 |
| `list_blacklist` | 官方游标单页，含 `next_cursor`。 |
| `change_blacklist` | `op=add/del`，1–20 个显式 OpenID；不会暗中先踢人。 |
| `list_join_requests` | 官方待处理申请单页，返回当前有效 `flag`；重新查询可能更新旧 flag。 |
| `approve_join_request` | 上一项的真实待处理 `flag`、明确批准/拒绝与有限拒绝理由。 |
| `list_join_strategies` | 机器人级策略单页摘要，不向模型默认展示跨群名单。 |
| `get_operation_status` | 仅本群同会话、同操作者提交的工具写操作；未知/部分成功只查状态。 |

查询仍受宿主启用与插件会话过滤；群管理查询/写入要求 AstrBot 管理员或本群实时 QQ owner/admin，不能用历史 `last_known_role` 授权。机器人级策略要求 AstrBot 管理员。QQ 执行权限与宿主操作者权限分别校验，管理写还受默认关闭的 `management_writes` 开关和实际 QQ 响应约束。仅管理调用需要时联网核实操作者，不为每轮模型描述全群名单。

写操作 ID 绑定原始消息、场景、目标、操作者和规范参数，同次模型重试复用；相对禁言时长冻结成首次请求的截止时间。结果只给必要状态/有限成员摘要，不默认返回 `union_openid`、原始响应或跨群策略内容；unknown、取消和部分成功不能换新 ID 盲发。`extension_schema` v3→v4 新增工具归属表，旧操作和 unknown 保留；回退须停机恢复匹配代码与数据库备份。
