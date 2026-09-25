# OneBot 网络接入（v0.5.0 预览）

这是 **OneBot v11 风格 OpenID 子集**，不是数字 QQ 号协议或完整 OneBot 服务。HTTP 与正向 WS 共用端口，每个端点固定一个平台实例及当前代次；不接受 `self_id` 动态路由，不另连 QQ。反向 WS、HTTP 事件上报、quick-operation、私有 action 均不提供。

## 启用与撤销

1. 在 Plugin Pages 的“连接配置”读取目标实例，开启网络总闸和该实例监听。
2. 配置绑定 IP、独立端口及专用随机 token（16–512 个可打印 ASCII 字符，建议至少32字节随机值）。默认 `127.0.0.1:5700`、关闭、只读；不要复用 QQ 凭据或 Dashboard JWT。
3. 保存不启动监听；确认重载该平台后生效。写动作还需选择并确认“允许网络写动作”；撤回/管理操作另受原 `management_writes` 开关及 QQ 实际权限限制。

实例设置只存于宿主平台配置的 `onebot`；本体平台表单隐藏原始对象，请通过 Pages 调整并确认写入权限。

```json
{"enable": false, "host": "127.0.0.1", "port": 5700, "token": "", "writes": false}
```

Pages 不回传 token，仅提供已配置状态与保留/替换/清空；清空前关闭该实例监听。保存修改会撤销旧代次，token 轮换需重载。每实例使用不同端口；冲突会明确启动失败，不抢占端口或启动另一个 QQ 连接。配置损坏时拒绝启动，可在宿主平台配置修复。

关闭页面或管理 API 不停止监听。关闭网络总闸立即拒绝新动作，现有配置监测周期（约1秒）内收束请求/监听；重新开启总闸仍需重载。撤销不回滚上游已经接受的写入。

监听仅提供 HTTP/WS，不内建 TLS。非回环地址须由管理员显式配置；只在受控网络或自行配置的可信 TLS 反向代理后使用，代理也应屏蔽 URL 中的鉴权参数。插件不修改防火墙、代理或证书。

## HTTP / 正向 WS

推荐 `Authorization: Bearer <专用token>`。也支持 URL 编码的 `access_token` query；它在动作解析前剥离，访问日志关闭，不回显原 URL。两种令牌同时提供时必须一致；缺失401、错误403。

| 入口 | 请求 |
|---|---|
| HTTP `/:action`、`/:action/` | GET query；POST JSON 或 `application/x-www-form-urlencoded` |
| WS `/api`、`/api/` | action 请求/响应，不推送事件 |
| WS `/event`、`/event/` | 仅事件；收到的回复数据不执行 |
| WS `/` | action 与事件混合 |

```http
POST /send_group_msg HTTP/1.1
Authorization: Bearer <专用token>
Content-Type: application/json

{"group_id":"真实group_openid","message":[{"type":"text","data":{"text":"你好"}}],"_qq_reply_context":"事件给出的短期句柄","_qq_operation_id":"调用方稳定的操作ID"}
```

WS 请求：

```json
{"action":"get_status","params":{},"echo":{"request":"status-1"}}
```

成功为 `{"status":"ok","retcode":0,"data":...}`；失败为 `failed` 并保留 `code/business_code/trace_id/http_status/phase/operation_id` 等诊断。不会返回假成功或把异步受理当发送完成。管理写的 `data` 保留适配器已确认结果对象（如 `state`），不是标准 v11 的 `null`；这是部分兼容差异。

未知 action：HTTP404 / WS1404；已知但不支持：HTTP200 + `unsupported` / retcode1501。类型不支持406、畸形输入400；已知业务失败仍HTTP200，QQ 的 HTTP 状态放在 `http_status`，不冒充协议状态。活动请求容量拒绝为HTTP200 + `network_capacity`，WS连接容量满在升级前拒绝。

ID 必须是原始字符串，数值 ID 不自动转换。布尔参数可用布尔值或字符串 `true/false`；整数参数支持规范十进制字符串。消息支持现有 CQ/数组 codec；字符串形式的 JSON 对象数组也会识别，普通方括号文本仍按 CQ 字符串处理；如需原样发送 JSON 数组文字，使用 `auto_escape=true`。重复/冲突参数、NaN/Infinity、错误编码和未知业务参数明确失败。

`echo` 缺省与显式 `null` 不同；支持有界 JSON 的全部类型，原样回传。可解析且不含凭据的非法请求对象也保留 echo，解析失败或含凭据时不回显。鉴权字段和当前 token 不能放进动作、echo 或输出；含凭据的事件会给出显式脱敏通知。**echo 不是幂等键**。

## 实时事件与被动回复

群/C2C缺少标准必需的 font、子类型等字段，因此以 `post_type: "qq_event", qq_type: "message"` 提供部分消息视图：保留真实字符串 `message_id/user_id/group_id`、源时间、消息段和可观察昵称，不编造资料。频道消息也只作 QQ 扩展；附件仅提示元信息，不下载、不送入 LLM。原始信封和宿主管线不被替换。

`self_id` 只来自合法 READY 的真实 Bot ID；未知时省略。已知时生命周期/心跳可用 `meta_event`，否则是 `qq_event` 元事件；它们报告真实网络连接/当前状态，不证明 QQ 可用。心跳按时间间隔生成，不因持续事件流停发。互动、管理和好友变化仅输出有限扩展元信息，不导出回调/审批票据；`FRIEND_ADD` 不是好友请求。

事件的 `_qq_reply_context` 可传给三个发送 action，引用当前实例已观察的群/C2C源。它校验原来源、目标、代次及 TTL；按原群5分钟/C2C保守60分钟上限，容量淘汰或重载也会失效。无句柄时走现有主动策略，不猜最近消息，不接受客户端自报 `msg_id/event_id`。事件在本体交付确认后旁路观察；无订阅恢复历史回放、无 exactly-once 承诺。

外部与本体发送共用原 `_qq_operation_id` 防重账本；本地不预判 QQ 频控，服务端限速错误保留业务码、HTTP 状态与 `Retry-After`。同一操作 ID 不能改变来源、目标或内容。查询：

- `_qq_get_send_status(operation_id)`：发送结果和状态。
- `_qq_get_extension_status(operation_id)`：扩展操作状态/错误摘要，不返回上传回执或管理请求正文。

断开、超时、`unknown/result_unknown/partial` 后先查询账本并核对实际结果；不自动重试、不退款、不删库。新操作 ID 也不代表安全重放。群审批仍需要原服务真实观察的 flag；网络事件不提供审批票据。

## 容量与能力

每实例：最多16条 WS、32个活动 HTTP/WS 会话请求；每条WS至多执行一个 action，前条完成前的新请求明确返回 `network_action_capacity`，不悄悄排队；控制/关闭帧仍及时处理。帧/请求256KiB，JSON深度20/节点4096，动作120秒超时；媒体请求也受帧上限限制。每连接最多32个排队帧/1MiB、总排队4MiB、短期来源引用1024。慢端/超限明确关闭并计入诊断，不阻塞本体或 QQ ACK。上述不等于系统级 TCP 防洪限制，远程入口需自行做网络访问控制。

Pages“实际网络状态与能力”及 `_qq_get_capabilities` 使用同一目录，列参数、实际返回字段、缺失项、OpenID语义和权限证据。实现存在不代表应用获权；完整群/好友列表、缺完整历史的 `get_msg` 等继续不支持。

## 回退与验证边界

P5不升级数据库/schema。回退前关闭网络总闸并停用实例，备份宿主配置与完整插件数据，核对未完成/未知账本；不要通过删库重放。旧P4不提供网络入口；不回退到无法识别现有账本的更早版本。

本阶段验收为隔离 loopback、合成身份、真实宿主装配和页面 Node 夹具，不代表真实 QQ 权限、扫码/按钮/部署、浏览器视觉 E2E 或长期负载验证。
