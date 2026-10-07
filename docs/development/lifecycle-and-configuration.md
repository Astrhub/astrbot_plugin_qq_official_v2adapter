# 连接、代次与配置

## 实例身份

`InstanceKey` 由平台 ID、机器人 AppID、传输方式、环境和分片信息组成。平台配置发生变化时，实例的 fingerprint 不再匹配，旧实例进入 `reload_required`，所有绑定的 client、source、订阅和上传句柄都必须停止发起新请求。

不要缓存以下对象跨重载使用：

- `event.bot` / `event.bot.qq`；
- `SessionRoute`、reply source、`NativeView`；
- EventBus subscription；
- `NativeUploadHandle`；
- 带 `owner` 的 `with_options()` 视图。

## 平台配置

基础配置由 AstrBot 平台表单保存，核心字段如下：

| 字段 | 规则 |
| --- | --- |
| `type` | `qq_official_v2` 或 `qq_official_v2_webhook` |
| `appid` / `secret` | 必填；secret 不写入日志或错误摘要 |
| `use_markdown` | 布尔值，默认 `true` |
| `transport` | WebSocket 或 Webhook，由平台类型归一化 |
| `shard_mode` | `auto` 或 `manual` |
| `shard` | 手动模式为 `[index, count]`，`0 <= index < count <= 1024` |
| `intents` | 0 到 `2**32-1` 的整数位掩码 |
| `webhook_uuid` | Webhook 实例需要唯一 UUID |

当前连接固定正式环境。旧 `environment`/`is_sandbox` 输入会被删除或忽略；不要新增沙箱分支。

保存配置与重载实例是两个动作：保存只修改本体配置并标记需要重载，重载才会创建新代次和新传输资源。冲突、指纹不一致、磁盘校验失败时应保留旧运行实例，不能半应用。

## WebSocket

自动模式先调用网关信息接口，按 QQ 推荐值创建分片，插件自身最多启动 32 片；Identify 预算按机器人共享，并遵守官方创建窗口。每片可独立 Resume 和重连：

- 临时断线使用 `Retry-After` 或 1 秒到 15 分钟的抖动退避；
- heartbeat ACK 超时会断开当前片；
- 认证、配置或无法释放 socket 等终止错误不会无限重试；
- 部分在线状态为 `degraded`，健康片仍可接收和发送；
- 整组不可用或配置冲突时需要修正后重载。

## Webhook

Webhook 只接受 POST。`Webhook` 在进入 `RawInbox` 前检查：

- `X-Signature-Ed25519` 与 timestamp；
- AppID 请求头与当前实例；
- timestamp 约 -300/+60 秒窗口；
- body 不超过 1 MiB；
- 事件去重窗口（4096 条、约 300 秒）。

挑战请求返回 QQ 所需的挑战响应。普通回调只写入持久收件箱；Webhook 本身不执行互动 ACK，也不直接运行命令。

## Pages 设置

插件设置由 `_conf_schema.json` 声明：`webui_enabled`、`remote_menu_sync`、`onebot_network_enabled` 和四个资料库限制。Pages 本地设置的 schema version 当前为 2，`draft` 与 `applied` 分离，支持 save/discard/defaults/restore/apply 和乐观 revision；最近保留 20 个版本。

高级默认值：

```text
media_max_bytes=32000000
stream_fallback=aggregate
stream_max_chars=4096
stream_timeout=120
typing_enabled=false
keyboard_enabled=false
keyboard_execute=false
ticket_ttl=120
management_writes=false
```

`apply` 只应用到本地运行设置。远端菜单/面板需要额外的 `remote_menu_sync` 总开关、Pages 确认和操作账本校验。
