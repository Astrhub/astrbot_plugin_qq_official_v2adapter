# QQ 官方 V2 适配器

`astrbot_plugin_qq_official_v2adapter` 是 AstrBot 的 QQ 官方机器人 V2 适配器。它直接接入 QQ 官方开放平台，提供 WebSocket 与 Webhook 两种接收方式，并把群聊、C2C、频道文字子频道和频道私信接入 AstrBot 的标准消息管线。

插件的目标是让你完成三件事：在 AstrBot 中接入一个 QQ 官方机器人、用现有插件处理 QQ 消息、在插件进程内使用官方 V2 SDK。

> 这不是 QQ 个人号协议，也不会把 QQ 号伪装成官方机器人 OpenID。适配器路由、OpenID 与 OneBot ID 均按字符串处理；个别官方管理字段仍按 QQ API 类型校验。

## 你需要什么

| 项目 | 要求 |
| --- | --- |
| AstrBot | `>= 4.28.1` |
| Python | `>= 3.12` |
| QQ 机器人 | QQ 开放平台的 AppID 与 AppSecret，或可用的扫码绑定流程 |
| 网络 | WebSocket 模式需要 AstrBot 能访问 QQ 官方网关；Webhook 模式需要 QQ 能访问 AstrBot 的统一 Webhook 地址 |

插件没有单独的构建步骤。运行依赖会从 `requirements.txt` 安装：`aiohttp`、`cryptography` 与 `qrcode`。

## 安装

### 从 AstrBot WebUI 安装

在插件管理中选择“从 Git 仓库安装”，填入：

```text
https://github.com/Astrhub/astrbot_plugin_qq_official_v2adapter
```

安装完成后重载插件。

### 手动安装

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/Astrhub/astrbot_plugin_qq_official_v2adapter.git
```

然后在 AstrBot WebUI 重载插件。插件会在首次初始化时创建自己的数据目录，不需要手动建表。

## 五分钟接入

### 1. 选择接收方式

在 AstrBot 的平台配置中选择其中一个平台类型：

- **QQ 官方 V2（WebSocket）**：适合大多数部署。插件主动连接 QQ 网关，不需要从公网暴露入站端口。
- **QQ 官方 V2（Webhook）**：适合已经有公网 HTTPS 入口、希望由 QQ 回调事件的部署。需要为实例保存唯一的统一 Webhook UUID，并让 QQ 回调 AstrBot 暴露的统一路径 `/api/platform/webhook/{webhook_uuid}`。

同一个机器人不能同时启用冲突的 WebSocket、Webhook 或分片接收实例。不同实例也不能重复占用同一个机器人和冲突的分片。

### 2. 填写凭据

手动配置时填写 AppID 和 AppSecret。插件只连接正式环境；旧配置中的 `environment`、`is_sandbox` 会被忽略，不要把它们当作当前可用的沙箱开关。

启用 `webui_enabled` 后，在插件 Pages 的 `control` 页面使用扫码绑定：

1. 点击“生成二维码”，确认本次操作。
2. 使用手机 QQ 扫码。
3. 等待页面显示凭据已准备好，再确认身份并提交。
4. 回到实例配置，勾选启用并重载平台。

扫码租约是短期的，取消页面或过期后需要重新生成；提交前不会把凭据写入平台配置。扫码完成后机器人仍保持关闭，必须显式启用并重载才会连接。

### 3. 选择消息格式

平台配置中的 `use_markdown` 默认是 `true`。它只影响没有显式指定格式的普通文本回复和文本流；图片、文件、卡片和显式指定的消息格式不会被它改写。

### 4. 启用并重载

保存平台配置后，启用实例并重载。Pages 的状态页可以查看：

- `online`：传输是否在线；
- `message_ready`：是否具备消息收发条件；
- WebSocket 分片状态或 Webhook 最近一次验签回调；
- 最近一次失败原因、重试计划和收件箱积压。

WebSocket 自动模式会按 QQ 网关返回的推荐值启动完整分片组，最多 32 片。手动分片只启动配置的 `[index, count]`，需要你自己保证所有片的配置完整且总数一致。部分分片异常时健康分片仍可继续工作；整组无法运行时请按状态页提示处理并重载。

### 5. 验证消息

在已配置的群中 @机器人发送一条消息，或向机器人发送 C2C 消息。插件还注册了 `v2menu` 帮助命令（也支持查询词和分页）：

```text
v2menu
v2menu md
v2menu kb
```

群聊的菜单需要真实的 @ 唤醒；帮助、Markdown 或键盘在当前场景不可用时会按实现规则回退或返回明确错误。

## 能做什么

| 能力 | 支持范围 |
| --- | --- |
| 接收 | 群 AT/普通消息、C2C、频道 AT/普通消息、频道私信；Webhook 事件会先验签并持久接收，再交给 AstrBot |
| 回复 | 文本、Markdown、群/频道 At、Reply，以及 QQ 原生 Json 卡片；消息链一次性校验，不会悄悄拆成多条 |
| 媒体 | 群/C2C 支持图片、语音、视频、文件；频道支持图片 URL 或本地 multipart；DM 只支持图片 URL |
| 流式 | C2C 使用 QQ 原生流；其他场景可配置为有界聚合或拒绝。群/频道/DM 没有伪造的原生流 |
| 互动 | 群/C2C 原生 Json 卡片与键盘、模板/自定义卡片、按钮回调；频道/DM 按 QQ 官方字段限制，回调默认关闭 |
| 管理 | 群成员、禁言、踢出、黑名单、入群申请、频道管理、撤回、菜单/面板等，权限由 QQ 当次响应决定 |
| 资料 | 成员与陌生人资料缓存、头像 URL、当前名单状态；历史资料会标注来源，不冒充实时成员 |
| SDK | `event.bot.qq` 原生具名 API、事件订阅、媒体上传、管理 API 和操作状态查询 |
| 旧 OneBot 网络入口 | 现有外部客户端的迁移入口，计划废弃；复用同一发送账本和权限边界 |

频道和 DM 的出站发送需要在线 WebSocket；Webhook 实例可以接收回调，但不能替这两个场景提供发送通道。

### 插件开发者直接使用 SDK

收到事件后，从 `event.bot.qq` 获取绑定当前机器人、场景和代次的 SDK 视图：

```python
member = await event.bot.qq.get_group_member_info(
    event.route.target,
    "成员 OpenID",
)
await event.send("你好")
```

需要订阅原生事件时使用 `event.bot.qq.events.subscribe(...)`。后台任务或明确指定目标时使用 `event.bot.qq.send(scene, target, message)`。SDK 与适配器共用连接、权限检查和发送账本。

入站附件只在消费时按需读取，不会因为收到一条 QQ 消息就下载所有附件。图片、音频、视频和未知 MIME 会按官方类型转换为 AstrBot 组件；无法安全转换时保留为文件或 Unknown，并保留原始元数据。

## 插件设置

在插件设置中可以调整全局能力：

| 设置 | 默认值 | 作用 |
| --- | ---: | --- |
| `webui_enabled` | `true` | 提供 V2 Pages 管理接口 |
| `remote_menu_sync` | `false` | 允许托管面板同步到 QQ；默认关闭，不会发布远端菜单 |
| `onebot_network_enabled` | `false` | 旧 OneBot HTTP/正向 WebSocket 入口，计划废弃；新集成直接使用进程内 SDK |
| `profile_max_records` | `32768` | 资料库最多保留的记录数 |
| `profile_max_bytes` | `64 MiB` | 资料库大小上限 |
| `profile_stale_seconds` | `300` | 资料被标记为过时前的秒数 |
| `profile_cooldown_seconds` | `600` | 资料查询失败后的冷却时间 |

高级选项在 Pages 的本地设置中管理，默认值包括：本地媒体读取上限 32,000,000 字节、非 C2C 流聚合、流文本上限 4096 字符、流超时 120 秒、`typing_enabled=false`、`keyboard_enabled=false`、`keyboard_execute=false`、按钮票据 TTL 120 秒、`management_writes=false`。

设置采用“草稿 → 应用到本地”的流程。应用本地设置不会自动发布 QQ 远端面板；远端托管面板还需要打开 `remote_menu_sync` 并在 Pages 中确认具体作用域。

## 旧 OneBot 网络入口（计划废弃）

当前版本仍保留独立 OneBot 网络入口，用于已有外部客户端迁移。新插件和新集成直接使用进程内 SDK。入口默认关闭；打开全局 `onebot_network_enabled` 后，还要在对应平台实例的 `onebot` 配置中设置：

```json
{
  "enable": true,
  "host": "127.0.0.1",
  "port": 5700,
  "token": "replace-with-a-16-char-token",
  "writes": false
}
```

每个实例使用独立端口和专用 token；token 必须是 16–512 个可打印 ASCII 字符，不能复用 QQ AppSecret。HTTP 使用 `/:action`，正向 WebSocket 使用 `/api`；`/event` 只用于观察事件，不执行动作。`writes=true` 门控所有网络写 action，包括发送和撤回；具名管理、黑名单等管理写还需要 `management_writes`，托管面板同步另受 `remote_menu_sync` 控制。所有写入都受 QQ 权限和服务端限流约束。

这是 OpenID 子集，不是完整 OneBot v11：不支持反向 WebSocket、HTTP POST 事件、quick operations、跨实例路由、数字 ID 语义或历史消息重放。详细边界见 [OneBot 说明](docs/ONEBOT.md)。

## 你需要知道的限制

- QQ 官方权限、配额和频控以当次服务端响应为准。插件不会因为本地“看起来支持”就绕过 QQ 权限。
- QQ 官方平台使用场景专用 OpenID 和官方 ID；不要把 QQ 号、群号或频道 ID 互相替换。
- 群/C2C 回复来源有有效期：群通常 5 分钟，C2C 使用保守的 60 分钟窗口。来源过期时，只有白名单拒绝码才允许一次有限的主动降级。
- 超时、断线、`unknown`、`result_unknown` 或部分成功都不会自动重放。先用原 `operation_id` 查询状态，再决定是否由业务自行处理。
- C2C typing 需要开启 `typing_enabled`，且必须来自真实的被动消息来源；其他场景不会伪造输入状态。
- 本地文件受 AstrBot 进程文件权限和媒体边界限制；外部 URL 交给 QQ 转存，本机不会替你下载公网媒体。
- 管理写操作和按钮回调默认关闭。开启后仍需满足当前实例、会话、操作者和 QQ 权限条件。

## 常见问题

### 页面显示 `reload_required`

平台配置的指纹已经变化，旧实例会主动失效。保存后重载对应平台；不要继续使用旧事件里的 `event.bot` 或旧 SDK 视图。

### WebSocket 一直重连

先检查 AppID/AppSecret、实例是否重复、分片模式是否冲突，以及状态页的 `failure_details`。插件尊重 QQ 的 `Retry-After`，没有该字段时使用带抖动的指数退避，最长 15 分钟。明确的鉴权或配置错误需要修正配置后重载。

### Webhook 收不到消息

确认统一 Webhook UUID 唯一、路由指向 AstrBot、请求能到达实例，并检查签名时间、AppID 请求头和 body 大小。Webhook 只接受 QQ 官方签名的 POST；挑战请求会被处理，普通回调先进入持久收件箱。

### 主动发送被拒绝

确认机器人已在线、目标已被观察、QQ 允许主动消息，并检查是否返回 `40034105` 或被动来源过期码。只有实现明确列出的拒绝场景才会进行一次模式转换；未知结果不要改用新 operation ID 重发。

### 按钮没有执行

确认 `keyboard_enabled=true`，按钮由本插件的 `callback_button` 生成且票据未过期；按钮回调必须绑定已加载 Star 的异步方法。QQ 管理员身份不等于 AstrBot 管理员身份。

### 旧 OneBot 网络入口返回 `network_not_ready`

这个错误来自旧网络监听器。已有外部客户端需要检查全局 `onebot_network_enabled`、实例 `onebot.enable`、监听地址/端口和 token，并确认修改后已重载。写动作还需要 `onebot.writes=true`；这不会自动打开插件的 `management_writes`。新集成直接改用 `event.bot.qq`。

## 开发者入口

如果你要编写 AstrBot 插件或为本适配器贡献代码，从 [开发者文档索引](docs/README.md) 开始：

- [架构与数据流](docs/development/architecture.md)
- [连接、代次与配置](docs/development/lifecycle-and-configuration.md)
- [SDK 与事件订阅](docs/development/sdk.md) / [事件模型](docs/development/events.md)
- [持久化与恢复](docs/development/persistence-and-recovery.md)
- [发送、媒体与幂等](docs/development/delivery-and-media.md)
- [扩展、按钮与群工具](docs/development/extensions.md)
- [旧 OneBot 网络协议（计划废弃）](docs/ONEBOT.md)
- [从旧 OneBot 网络入口迁移到 SDK](docs/development/sdk-migration.md)
- [测试与 CI](docs/development/testing.md)

## 相关链接

- [AstrBot 文档](https://docs.astrbot.app/)
- [QQ 机器人官方 API V2](https://bot.q.qq.com/wiki/develop/api-v2/)
- [Issue 区](https://github.com/Astrhub/astrbot_plugin_qq_official_v2adapter/issues)

## 许可

本项目使用 [AGPL-3.0](LICENSE)。平台图标的来源与许可见 [assets/LICENSE.AstrBot-Dashboard](assets/LICENSE.AstrBot-Dashboard)。
