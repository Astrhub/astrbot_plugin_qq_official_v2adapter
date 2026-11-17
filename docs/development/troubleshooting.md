# 开发者排障

## 先看哪几处

1. Pages 的实例状态：`state`、`failure`、`failure_details`、`gateway_group`、`network`、`raw_disposition`。
2. 原始收件进度：使用 receipt 查看 `core_state`、host 交付和留置原因。
3. 发送账本：用原 `operation_id` 查询 send/extension status。
4. 代码日志：只记录业务码、HTTP 状态、phase、trace ID 和实例身份，不记录正文、token、票据或 presigned URL。

## 常见错误

| 错误 | 含义 | 处理 |
| --- | --- | --- |
| `reload_required` / `stale_generation` | 平台配置已经变更 | 停止旧对象，重载新代次 |
| `duplicate_receiver` | 同机器人接收模式或分片冲突 | 保留一个实例，修正 shard/transport |
| `reply_expired` | 被动来源已过期 | 仅按白名单规则选择一次主动发送 |
| `result_unknown` | QQ 最终结果无法确认 | 查询原 operation ID；禁止自动重放 |
| `subscription_gap` | SDK 订阅队列/字节预算超限，或回调任务被取消而关闭 | 重新订阅并处理背压；owner 失效另按 `stale_owner`/`stale_generation` 处理 |
| `stale_owner` | owner 已卸载 | 释放旧句柄，使用当前实例重新获取视图 |
| `panel_drift` | 远端面板与账本基线不一致 | 暂停同步，人工核对 QQ 实际状态 |
| `message_not_found` / `message_expired` / `message_deleted` | 本地历史没有该消息 | 不要伪造正文或改查频道 `get_message` |
| `forward_nodes_unavailable` | 只有引用索引没有真实节点 | 不要用摘要拼转发 |
| `history_storage_unavailable` | 历史库写入失败 | 入站继续；检查磁盘与 `last_error`，不要删账本 |
| `invalid_history_range` | 范围删除没有筛选条件 | 补 scene/target/since/until，或走 purge 确认词 |
| `history_search_unavailable` | FTS5 不可用或查询失败 | 页面显示 LIKE 降级；关键词仍可按 LIKE 查 |
| `history_search_index_failed` | 单条索引写入失败 | 消息已入库；待补索引，不改发送账本 |
| `profile_limit_below_usage` | 新的资料库字节上限低于当前占用 | 提高上限或先清理资料后再保存 |
| `profile_not_ready` | 资料库未挂载或已关闭 | 检查插件初始化，不要改 settings.sqlite3 |
| `invalid_profile_config` | 资料库策略字段非法或不完整 | 按页面显示的上下限提交四个整数 |

## 不要做的事

- 不要捕获 `unknown` 后生成新 operation ID 自动重试。
- 不要用最近一条消息、QQ 号或猜测的 event ID 拼装被动回复。
- 不要把 Webhook HTTP 200 当成 AstrBot 已完成事件处理。
- 不要直接写 SQLite、绕过 `SendingCore` 或调用未公开的任意 URL。
- 不要在平台重载后继续使用旧 `event.bot`、upload handle 或 EventBus subscription。
