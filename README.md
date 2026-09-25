# QQ 官方 V2 适配器 (astrbot_plugin_qq_official_v2adapter)

独立的 AstrBot `qq_official_v2` 平台适配器，提供官方协议接入、媒体与基础收发、流式、指令键盘和可选 OneBot HTTP/正向WS OpenID 子集。当前 **v0.5.0 为预览版**；网络默认关闭，本阶段为离线验收，完整接入、回调及Windows部署仍待验。

## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.12 | |
| AstrBot | >= v4.28.1, < v4.29 | 平台注册与 Plugin Pages |

**平台支持**：仅本插件注册的 `qq_official_v2`；不依赖原生 QQ 适配器或旧补丁插件。连接在线、消息交付和具体应用权限分别报告；代码支持不等于 QQ 已授权。

## 功能

当前可用：

- 平台注册/注销、多实例身份与代次隔离、禁用及失败清理
- 有界 HTTP/token 重试、WS 心跳与恢复、Webhook 原始体验签及持久收件
- 独立扫码短租约、一次性凭据提交、手填配置；保存与重载分离
- 六类官方聊天事件进入正常 AstrBot 管线；全量群聊兼容结构化自身 @ 标记，身份、回复凭据、额度和发送结果持久化
- 文本 / Markdown / At / Reply 共用发送核心；未知结果不自动重放或退还额度
- Plugin Pages：真实目录、参数帮助、自选快捷项、分级布局、草稿及版本恢复
- 默认关闭的托管指令面板；预览、应用本地、确认发布相互独立，只修改自有资源
- OneBot `send_group_msg` / `send_private_msg` / `send_msg`，缓存资料和头像 URL 查询
- 外链交QQ转存，本地文件/Base64分片上传；C2C原生流式、其他场景有界聚合、显式C2C typing
- 互动11/12独立ACK、短期点击者票据与正常权限管线；群/频道具名管理、分享及有真实记录的撤回

| 场景 | 基础发送 | 限制 |
|------|----------|------|
| 群聊 / C2C | 文本、Markdown、真实引用索引 | At 仅群聊；被动窗口分别5分钟/5次、60分钟/4次 |
| 文字子频道 / 频道私信 | 文本、Markdown、真实消息引用 | 需在线 WS；At 仅文字子频道 |
| 所有场景 | 指令帮助、原生格式会话 ID、保守主动发送 | 主动目标须已观察；配额/权限仍以服务端为准 |
| 入站附件 | 完整元数据与原始payload保留 | 不自动下载、转码或作为图片输入LLM；普通聊天链接不触发媒体发送 |

群/C2C 会话使用当前实例 ID 和 QQ 的群/用户 OpenID 组成 UMO，例如 `qq_v2:GroupMessage:<group_openid>`；内部仍独立记录机器人、场景与发送目标。旧 `v2.…` 会话仅保留已验证来源的发送兼容，旧精确白名单/配置路由不会自动迁移。

C2C 官方概述60分钟与字段5分钟仍有冲突，服务端拒绝优先。未知写入、配置轮换及等待后过期不会转为主动补发。

未开放：反向WS/HTTP事件上报、完整好友/群枚举、全员禁言、群文件系统、合并转发及缺完整记录的 `get_msg`。`/v2/menu` 自定义菜单仍独立于已实现的 `/v2/panels`（含C2C all/specific），不会接管人工菜单。管理资料与互动不会建立聊天身份；不支持的事件保留，不意外触发LLM。

OneBot 接入通过 Pages“连接配置”管理，每实例单独端口和专用token，复用现有动作/额度/unknown账本；字符串ID与`qq_event`扩展不伪装完整v11。HTTP/WS路由、被动回复、限制和回退见[网络接入说明](docs/ONEBOT.md)。

| 扩展 | 实现与条件 |
|---|---|
| 群/C2C媒体 | 图片、语音、视频、文件；格式接受由QQ判断，URL转存及prepare→PUT→finish→files；整链预检，不静默丢caption或拆多条 |
| 频道/DM图片 | HTTP(S)图片URL直传；频道本地图片另支持multipart。DM本地文件及两场景语音/视频/文件拒绝 |
| 流式/typing | C2C原生流；其他场景先声明聚合模式。typing需开关和被动来源，使用一次本地回复额度、不续期 |
| 键盘 | 群/C2C开启后使用 `v2menu kb`；导航/详情/预填与无参指令二次确认分开，权限未知，有参不自动执行 |
| 管理/分享 | 原生具名接口；OneBot同步群资料/成员/禁言/移除/审批、真实bot资料与撤回。写入默认关闭，实际权限由QQ决定 |

本地字节超过图片/语音/视频软限制时默认按文件类型提交，可传 `allow_file_fallback=false` 禁止降级；硬限制和本地 `media_max_bytes` 仍生效。上传回执的 `ttl=0` 按长期有效处理，内存缓存最长24小时；分片媒体可在当前成功返回中取得 `raw_url`，预签名地址不写入发送账本。

## 安装

1. 在 AstrBot 插件界面选择从链接安装：`https://github.com/Astrhub/astrbot_plugin_qq_official_v2adapter`
2. 或手动克隆到 `data/plugins/` 后在 WebUI 重载
3. 在平台对应配置档的插件列表中启用本插件，确保群 @ 唤醒和 `v2menu` 等指令可用

## 配置

### 基础开关（插件设置）

| 配置项 | 类型 | 默认 | 说明 |
|--------|------|------|------|
| `webui_enabled` | bool | true | 启用 V2 管理页面 API |
| `remote_menu_sync` | bool | false | 托管面板总闸；还须在 Pages 确认具体应用和范围 |
| `onebot_network_enabled` | bool | false | HTTP/正向WS总闸；还须配置实例监听/独立token并重载，默认只读 |

### 连接与凭据

在 Pages 接入区或本体平台管理维护 AppID、AppSecret、环境、传输、intents/shard；两者都以本体平台配置为唯一权威存储。未编辑 secret 时保留原值，替换/清空和改绑均需确认；服务端不返回原 secret，浏览器不持久保存凭据。扫码提交后保持禁用，需另行启用并重载。

生产 OpenAPI 使用 `https://api.bot.qq.com`，WebSocket 直接使用官方 `/gateway/bot` 返回的地址。**沙箱网络接入暂不支持**：现行官方环境选择规则尚未确认，保留 `sandbox` 配置和本地数据，但在 token、连接及扫码创建前报 `unsupported_environment`，不会自动转向生产。

数据保存在 `data/plugin_data/astrbot_plugin_qq_official_v2adapter/`：`settings.sqlite3` 存草稿与快捷项来源，`transport.sqlite3` 存有界原始收件，`messaging.sqlite3` 存机器人共享身份、配额、发送结果和面板所有权，旁边 `.lock` 用于单写入者互斥。满额或损坏时拒绝新操作，不删除未交付或未知记录；备份/恢复应先停止插件并保留整套文件。

原始收件正文限全局1024条/64MiB、每实例256条；已确认去重键独立上限32768条、去重有效期最长5分钟，满额只淘汰最旧已确认键，不挤占待处理容量。

有效READY/RESUMED处理后确认，不堆积为保留正文。其他无效/未支持事件不自动丢弃；入口满额时可在控制台“保留事件恢复”读取元数据、勾选并确认丢弃（60秒快照）。该操作不可恢复，仅限所选保留项，不清理待交付聊天或发送账本；改绑/删除实例前先处理保留项。

v0.4.0 将消息库升级为schema 2，扩展表版本2；配置库schema 2、配置内容版本2，收件库schema 3。不能只回退P2/P3代码；须停用插件、保留整套新旧快照并先核对未完成/未知操作，不丢弃新账本来恢复发送。
P5没有新增数据库/schema；网络配置及token只在宿主平台配置。回退前关网络总闸、停实例并核对未完成操作，保留完整备份。

宿主入队不等于插件/LLM业务完成；崩溃窗口可能重投或未完成，不承诺 exactly-once。不要删库重试未知发送或面板创建；先核对实际结果，未能确认时继续保留未知状态。

回复来源到期后在后续清理中释放；仍被预留、在途或未知发送引用的来源继续保留，不重置额度。身份、引用和交付去重仍使用各自的有界缓存期限，不承诺无限保留。

普通交付去重和引用缓存各默认32768条、最长24小时，满额提前淘汰最旧可回收记录；活跃宿主管线及有效/受保护回复来源的交付标记保留，全受保护时才背压。引用缺失明确报 `reference_not_observed`，不伪造映射；历史淘汰可能缩短去重窗口，仍不保证 exactly-once。

未完成操作与完整终态结果各有独立32768条预算。旧成功结果只压缩为24小时防重凭据，查询返回 `operation_history_evicted`，不能重发；旧拒绝/未发送详情可淘汰，查不到不等于未执行。额度和未知操作不清理；全库仍受128MiB存储上限约束，无法安全写入时拒绝发送。已压缩详情不可恢复，回退需匹配代码版本的整库备份。

## 使用

### 启用原型

1. 安装并启用插件（验证基线 AstrBot 4.28.1）。
2. 插件详情 → Pages → `control`，读取接入目标后点「生成二维码」，用手机 QQ 扫码再保存凭据。不需要事先填写 AppSecret。保存后机器人仍关闭。
3. 勾选启用并重载才会连接。关闭页面会取消还没保存的二维码；不会授予管理员权限。
4. 下方目录实例独立选择；编辑布局后先预览、再保存草稿，按需应用到本地。插件热重载不会自动重启平台。

目录通过宿主插件/处理器接口只读生成，保留禁用项供编辑；页面预览基于默认配置，不代表所有会话的权限或前缀。关闭页面 API 后可从插件设置重新打开。

聊天按当前前缀使用 `v2menu`（示例 `/v2menu`），支持 `system`、`plugins`、`search 0 关键词` 及返回/翻页；`v2menu md` 请求 Markdown，确定的权限拒绝才退回纯文本。群/文字子频道链接只预填，必要时真实 @ 机器人再发送。保持本插件在当前会话 `plugin_set` 中启用；不会伪造 At 或放宽管理员权限。

媒体目录、大小、流式和扩展开关在Pages高级区随草稿保存/应用。媒体输入中的HTTP(S)外链直接交QQ转存，本机不下载、探测外链或解析其DNS；资源可达性、格式、大小由QQ判断，失败不改走本地下载。本地文件/Base64走官方分片，保留授权目录、普通文件及字节/哈希检查，拒绝符号/硬链接；本机默认32MB、最多200MB，base64另限8MiB，共享临时盘384MB，不限制QQ服务器下载。`allow_file_fallback`只作用于本地字节超软限制；不自动转码。

本地媒体按调用方类型上传原始字节，格式和内容合法性由QQ判定，不做自写容器白名单、宽高预审或重编码；频道multipart以通用二进制提交`file_image`，不声称本地已验证图片格式。

本地读取复用AstrBot通用媒体接口，已原生验证Windows普通文件、file URI与中文路径，Linux使用同一流程。仅授权可信目录；常规链接、junction/reparse、硬链接及非普通文件会拒绝，但不承诺隔离恶意同机进程竞态。Windows整体部署仍待验。

分片兼容服务端从0或1起始的连续索引，完成回执原样回传；重复、缺口、乱序及与实际文件大小不符的响应拒绝处理。
预签名PUT使用系统网络（包括现有Fake-IP/TUN），保留正常HTTPS证书/主机名校验、无QQ认证/cookie及禁止跳转，不要求修改系统DNS。
API、扫码和分片上传复用宿主系统CA+certifi的TLS上下文，仍校验证书和主机名；各实例/用途的会话及认证独立。

互动订阅需intents包含`1 << 26`，群申请为`1 << 24`。ACK与业务状态分别可查；ACK不表示执行成功。票据绑定点击者/目标/版本/代次，重启失效；ACK遵守每机器人50 QPS，内部目标2.5秒，新互动本地接收期限5分钟，不声称这是官方超时。

托管面板先应用本地，再预览范围、开启总闸并确认发布。每机器人最多20面板、每板20项且保留菜单槽；长命令不截断。指定群/C2C按各目标会话的有效配置生成，前缀或所选命令不一致时拒绝发布；可统一配置，或在菜单入口一致时明确只发布菜单。首次固定或 handler/参数变化须确认来源。外部漂移会暂停；确定失败修正后手动核对，停用保留远端资源，不自动迁移已创建面板范围。

面板读取的暂时网络/限流故障会延后自动恢复并遵守Retry-After；确定拒绝仍暂停同快照重试，未知写入只核对。已确认且归属、范围不变的目标不因聊天缓存24小时过期而停止同步；初次/新增目标仍须真实观察，不回填身份缓存。

WebSocket 鉴权/恢复成功才报告在线，重连预算耗尽后需检查配置并手动重载。Webhook 地址在接入区显示，沿用本体统一入口；需自行配置公网 HTTPS 反代及 QQ 允许的端口。`webhook_ready` 只表示本地可接收，回答验证不证明 QQ 已验证；`online` 表示近5分钟有合法签名回调。接入区“读取”可刷新状态。

保存前检查目标指纹和本体内存/磁盘是否一致，冲突时先重新读取，不覆盖外部改动；宿主没有跨进程原子 CAS。保存失败保留原运行配置，重载/连接失败保留已保存配置供修复；旧代次停止，不切回旧适配器。

### 进程内接口

```python
client = event.bot  # 真实群聊 V2 事件：绑定该事件的被动回复凭据
status = await client.api.call_action("get_status")
result = await client.send_group_msg(group_id=event.route.target, message="你好")
print(result["message_id"])
# client.call_action(...)、client.api.call_action(...) 与同名方法共用发送核心。
```

`event.send`、`send_by_session`、`client.qq.send(scene, target, ...)` 共用策略；无来源的实例调用仅走主动策略，不能借最近消息。CQ 字符串/数组全量校验，`auto_escape=True` 仅对字符串保留字面量；不能等价的组合拒绝，不偷偷拆消息。原生调用可给 `operation_id` 并用 `client.qq.send_status(id)` 查询；未知结果不重试，任意原生 POST/DELETE 不开放。

媒体可用原生组件或CQ数组`image`/`record`/`video`；文件使用显式`_qq_file`段或`client.qq.send_file(scene, target, input, name=...)`。仅支持一项媒体与可等价引用/文本组合，不接受任意原生写请求。流式先用`client.qq.streaming_mode(scene, target)`检查模式，再调用`event.send_streaming`或`client.qq.send_streaming`。

未发布的`upload_mode`已移除：原生`send_file`、`MediaInput`及CQ段不再接受该参数，方式只由输入来源决定。URL回执不跨操作复用；返回媒体`source=url`且不虚构size/MIME/内容哈希，本地字节返回`source=bytes`及实际size。原操作ID/未知账本不重置，内容绑定不匹配仍明确失败。

`client.qq.group_members(group)`、`join_requests(group)`返回完整结果或明确失败；审批flag有效5分钟，只来自真实待处理申请查询，不代表好友或机器人受邀审批。管理/分享/撤回需高级区开启`management_writes`；用`client.qq.extension_status(operation_id)`核对扩展结果，`extension_events()`读取typed状态。已发送后超时/取消、部分成功不能整批重放；来源/路由缺失或压缩后不能伪造撤回目标。

申请未完成/不确定项上限4096；过期未执行票据释放，已完成申请独立保留24小时防重，不挤占此预算。

扩展未完成/unknown限2048，ACK独立128，完整终态明细2048；旧结果压缩后保留24小时防重记录，不挤占未完成预算，仍受整库128MiB上限约束。诊断不存QQ凭据、预签名URL或流全文，Pages可查近期操作。存储故障时停发，保留账本和operation_id，恢复后核对unknown，不删库或换ID盲重试。

ID 均为字符串，不伪造 QQ 号、资料或成功。群/C2C必须提供场景专用OpenID，不用通用 `id` 补缺；频道/DM仍使用其 `id`。`get_stranger_info` 仅查未过期聊天缓存，实例级查询须给 `id_kind` 和 `scope`；`no_cache=True` 不支持。头像优先真实事件 URL，群/C2C可构造 AppID+OpenID URL，不自动下载。宿主4.28.1只对白名单适配器设置 `_session_isolated`；本插件保持可逆隔离会话，不补丁全局表。

## 本地测试

复用已有 AstrBot 依赖环境，需要 Linux、bubblewrap、Python 3.12、Node 24：

```bash
ASTRBOT_SOURCE=/root/work/AstrBot bash scripts/test-isolated.sh -q
```

脚本使用独立用户/PID/网络命名空间、只读源码与临时数据，QQ 上游仅用本地模拟服务，不读取现有实例配置。依赖 `requirements.txt` 中的 aiohttp、cryptography、qrcode，目标宿主已提供；不引入 QQ SDK。

## 项目结构

```
astrbot_plugin_qq_official_v2adapter/
├── main.py               # 插件主入口：注册/注销 qq_official_v2 平台
├── v2/                   # 适配器核心
│   ├── adapter.py        #   平台适配器与实例生命周期
│   ├── event.py          #   V2 事件与发送入口
│   ├── client.py         #   OneBot 风格调用客户端
│   ├── protocol.py       #   信封、请求、重放保护、错误与头像契约
│   ├── transport/        #   HTTP/token、WS、Webhook 与有界原始收件箱
│   ├── messaging/        #   真实聊天消费、持久身份/额度、统一发送
│   ├── media/            #   有界媒体I/O与目标隔离上传
│   ├── extensions/       #   扩展账本、互动票据、ACK与具名管理
│   ├── help.py           #   文本与 Markdown 指令帮助
│   ├── panels.py         #   有所有权保护的托管面板
│   ├── connections.py    #   本体连接配置写入口与重载
│   ├── onboarding.py     #   扫码租约、解密与提交
│   ├── commands.py       #   真实指令目录
│   ├── settings.py       #   本地 SQLite 配置存储
│   ├── host_auth.py      #   管理鉴权
│   └── web_api.py        #   Pages 控制台 API
├── pages/control/        # Plugin Pages 连接、扫码与目录控制台
├── tests/                # 禁网契约与本体装配测试
├── scripts/              # 隔离测试脚本
├── .github/workflows/    # CI 定义
├── _conf_schema.json     # 基础开关配置 Schema
└── metadata.yaml         # 插件元数据
```

## 相关链接

- [AstrBot](https://docs.astrbot.app/)
- [Issues](https://github.com/Astrhub/astrbot_plugin_qq_official_v2adapter/issues)

## 许可

AGPL-3.0 License（保留原仓库 `LICENSE`）
