# OneBot 兼容说明

这是 **OneBot v11 风格 OpenID 子集**。

## 实时事件与被动回复

群/C2C缺少标准必需的 font、子类型等字段，因此以 `post_type: "qq_event", qq_type: "message"` 提供部分消息视图：保留真实字符串 `message_id/user_id/group_id`、源时间、消息段和可观察昵称，不编造资料。频道消息也只作 QQ 扩展；网络视图对附件仅提示元信息，不替代宿主管线的标准媒体组件及按需读取。原始信封保留。

`self_id` 只来自合法 READY 的真实 Bot ID；未知时省略。已知时生命周期/心跳可用 `meta_event`，否则是 `qq_event` 元事件；它们报告真实网络连接/当前状态，不证明 QQ 可用。心跳按时间间隔生成，不因持续事件流停发。互动、管理和好友变化仅输出有限扩展元信息，不导出回调/审批票据；`FRIEND_ADD` 不是好友请求。

事件的 `_qq_reply_context` 可传给三个发送 action，引用当前实例已观察的群/C2C源。它校验原来源、目标、代次及 TTL；按原群5分钟/C2C保守60分钟上限，容量淘汰或重载也会失效。无句柄时走现有主动策略，不猜最近消息，不接受客户端自报 `msg_id/event_id`。事件在本体交付确认后旁路观察；无订阅恢复历史回放、无 exactly-once 承诺。

外部与本体发送共用原 `_qq_operation_id` 防重账本；本地不预判 QQ 频控，服务端限速错误保留业务码、HTTP 状态与 `Retry-After`。同一操作 ID 不能改变来源、目标或内容。查询：

- `_qq_get_send_status(operation_id)`：发送结果和状态。
- `_qq_get_extension_status(operation_id)`：扩展操作状态/错误摘要，不返回上传回执或管理请求正文。

断开、超时、`unknown/result_unknown/partial` 后先查询账本并核对实际结果；不自动重试、不退款、不删库。新操作 ID 也不代表安全重放。群审批仍需要原服务真实观察的 flag；网络事件不提供审批票据。

## 容量与能力

每实例：最多16条 WS、32个活动 HTTP/WS 会话请求；每条WS至多执行一个 action，前条完成前的新请求明确返回 `network_action_capacity`，不悄悄排队；控制/关闭帧仍及时处理。帧/请求256KiB，JSON深度20/节点4096，动作120秒超时；媒体请求也受帧上限限制。每连接最多32个排队帧/1MiB、总排队4MiB、短期来源引用1024。慢端/超限明确关闭并计入诊断，不阻塞本体或 QQ ACK。上述不等于系统级 TCP 防洪限制，远程入口需自行做网络访问控制。

Pages“实际网络状态与能力”及 `_qq_get_capabilities` 使用同一目录，列参数、实际返回字段、缺失项、OpenID语义和权限证据。实现存在不代表应用获权；完整群/好友列表、缺完整历史的 `get_msg` 等继续不支持。

## 进程内扩展动作与资料来源

`event.bot`、`client.api` 与 `client.call_action` 共用 OneBot 分派；即使可选网络关闭，进程内仍可调用。新增等价动作：`set_group_kick_members`（≤20、可部分成功）、`get_group_shut_list`、`_qq_get_group_blacklist`、`_qq_set_group_blacklist`、`_qq_get_join_approval_strategies`。这些动作在网络入口沿用显式允许表、只读/写开关及 `management_writes`；原生 `.qq` 的其余具名写接口不自动暴露为网络 action。QQ群名单单页和不具备当前成员事件连续性的历史记录不能混成当前完整名单。
`_qq_get_join_approval_strategies` 原样返回官方 `strategies`、`next_cursor` 和额外字段；缺失必需分页字段时报 `pagination_incomplete`。

`get_group_member_info` 默认缓存优先，`no_cache=true` 访问 QQ；`get_group_member_list` 仅复用启用成员事件位 24 且连续接收时的新鲜完整快照，默认未启位 24，查询可能每次逐页访问 QQ，历史资料仍可读取。明确刷新不返回旧缓存；成员 `_qq` 保留来源、状态及观察时间，标准 `role` 只在自身记录时间未过期且成员仍在群时输出，刷新昵称/头像不会续期旧管理员身份。`get_stranger_info` 有真实聊天观察时为 `source=chat_cache`，保留原 `first_seen/last_seen/source_message_id`；只有历史资料时返回 `source=profile_cache`，不造聊天时间或消息 ID；二者皆无则 `identity_not_observed`。查询必须给真实 `scope` 和 `id_kind`；`no_cache=true` 对陌生人明确 unsupported，群名重名不自动选择管理目标。
离线起查、返回时才重连的完整名单仍标 `continuous=false`，不能作当前缓存；后续同一稳定连接上的重查才恢复连续性。

