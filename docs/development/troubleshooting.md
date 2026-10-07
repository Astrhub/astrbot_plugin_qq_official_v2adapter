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
| `network_not_ready` | OneBot 全局或实例监听器未启用 | 检查两个开关、token、端口并重载 |
| `reply_expired` | 被动来源已过期 | 仅按白名单规则选择一次主动发送 |
| `result_unknown` | QQ 最终结果无法确认 | 查询原 operation ID；禁止自动重放 |
| `subscription_gap` | SDK 订阅队列/字节预算超限，或回调任务被取消而关闭 | 重新订阅并处理背压；owner 失效另按 `stale_owner`/`stale_generation` 处理 |
| `stale_owner` | owner 已卸载 | 释放旧句柄，使用当前实例重新获取视图 |
| `panel_drift` | 远端面板与账本基线不一致 | 暂停同步，人工核对 QQ 实际状态 |

## 不要做的事

- 不要捕获 `unknown` 后生成新 operation ID 自动重试。
- 不要用最近一条消息、QQ 号或猜测的 event ID 拼装被动回复。
- 不要把 Webhook HTTP 200 当成 AstrBot 已完成事件处理。
- 不要直接写 SQLite、绕过 `SendingCore` 或调用未公开的任意 URL。
- 不要在平台重载后继续使用旧 `event.bot`、upload handle 或 EventBus subscription。
