# QQ 官方 V2 适配器 (astrbot_plugin_qq_official_v2adapter)

独立的 AstrBot `qq_official_v2` 平台适配器，提供官方协议接入、基础收发、指令帮助和 OneBot v11 风格 OpenID 子集。当前 **v0.3.0 为预览版**，尚未进行真实 QQ 联调。

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
- 六类官方聊天事件进入正常 AstrBot 管线；真实身份、回复凭据、额度和发送结果持久化
- 文本 / Markdown / At / Reply 共用发送核心；未知结果不自动重放或退还额度
- Plugin Pages：真实目录、参数帮助、自选快捷项、分级布局、草稿及版本恢复
- 默认关闭的托管指令面板；预览、应用本地、确认发布相互独立，只修改自有资源
- OneBot `send_group_msg` / `send_private_msg` / `send_msg`，缓存资料和头像 URL 查询

| 场景 | 基础发送 | 限制 |
|------|----------|------|
| 群聊 / C2C | 文本、Markdown、真实引用索引 | At 仅群聊；被动窗口分别5分钟/5次、60分钟/4次 |
| 文字子频道 / 频道私信 | 文本、Markdown、真实消息引用 | 需在线 WS；At 仅文字子频道 |
| 所有场景 | 指令帮助、可逆会话、保守主动发送 | 主动目标须已观察；配额/权限仍以服务端为准 |

C2C 官方概述60分钟与字段5分钟仍有冲突，服务端拒绝优先。未知写入、配置轮换及等待后过期不会转为主动补发。

未开放：媒体上传/下载、流式、typing、互动 ACK/键盘回调、群/频道管理、C2C 自定义菜单（`/v2/menu`）、OneBot 网络服务及缺完整契约的 `get_msg` / `delete_msg` / `get_login_info`。已实现的指令面板使用独立 `/v2/panels`，包括 C2C 的 `all` 和 `specific` 范围；不要混同两类菜单。管理/互动/未知事件保留，不伪装聊天触发 LLM。

## 安装

1. 在 AstrBot 插件界面选择从链接安装：`https://github.com/Astrhub/astrbot_plugin_qq_official_v2adapter`
2. 或手动克隆到 `data/plugins/` 后在 WebUI 重载

## 配置

### 基础开关（插件设置）

| 配置项 | 类型 | 默认 | 说明 |
|--------|------|------|------|
| `webui_enabled` | bool | true | 启用 V2 管理页面 API |
| `remote_menu_sync` | bool | false | 托管面板总闸；还须在 Pages 确认具体应用和范围 |
| `onebot_network_enabled` | bool | false | OneBot 网络总闸（原型未实现，打开也不会监听） |

### 连接与凭据

在 Pages 接入区或本体平台管理维护 AppID、AppSecret、环境、传输、intents/shard；两者都以本体平台配置为唯一权威存储。未编辑 secret 时保留原值，替换/清空和改绑均需确认；服务端不返回原 secret，浏览器不持久保存凭据。扫码提交后保持禁用，需另行启用并重载。

生产 OpenAPI 使用 `https://api.bot.qq.com`，WSS 仅接受 `wss://api.bot.qq.com`；不后备到旧域名。**沙箱网络接入暂不支持**：现行官方环境选择规则尚未确认，保留 `sandbox` 配置和本地数据，但在 token、连接及扫码创建前报 `unsupported_environment`，不会自动转向生产。

数据保存在 `data/plugin_data/astrbot_plugin_qq_official_v2adapter/`：`settings.sqlite3` 存草稿与快捷项来源，`transport.sqlite3` 存有界原始收件，`messaging.sqlite3` 存机器人共享身份、配额、发送结果和面板所有权，旁边 `.lock` 用于单写入者互斥。满额或损坏时拒绝新操作，不删除未交付或未知记录；备份/恢复应先停止插件并保留整套文件。

原始收件正文限全局1024条/64MiB、每实例256条；已确认去重键独立上限32768条、去重有效期最长5分钟，满额只淘汰最旧已确认键，不挤占待处理容量。

有效READY/RESUMED处理后确认，不堆积为保留正文。其他无效/未支持事件不自动丢弃；入口满额时可在控制台“保留事件恢复”读取元数据、勾选并确认丢弃（60秒快照）。该操作不可恢复，仅限所选保留项，不清理待交付聊天或发送账本；改绑/删除实例前先处理保留项。

v0.3.0 会升级数据库结构，不能只回退到 P2 代码；回退前须停用插件、保留新旧完整快照并核对未完成/未知操作，不能丢弃新账本来恢复发送。

宿主入队不等于插件/LLM业务完成；崩溃窗口可能重投或未完成，不承诺 exactly-once。不要删库重试未知发送或面板创建；先核对实际结果，未能确认时继续保留未知状态。

回复来源到期后在后续清理中释放；仍被预留、在途或未知发送引用的来源继续保留，不重置额度。身份、引用和交付去重仍使用各自的有界缓存期限，不承诺无限保留。

普通交付去重和引用缓存各默认32768条、最长24小时，满额提前淘汰最旧可回收记录；活跃宿主管线及有效/受保护回复来源的交付标记保留，全受保护时才背压。引用缺失明确报 `reference_not_observed`，不伪造映射；历史淘汰可能缩短去重窗口，仍不保证 exactly-once。

未完成操作与完整终态结果各有独立32768条预算。旧成功结果只压缩为24小时防重凭据，查询返回 `operation_history_evicted`，不能重发；旧拒绝/未发送详情可淘汰，查不到不等于未执行。额度和未知操作不清理；全库仍受128MiB存储上限约束，无法安全写入时拒绝发送。已压缩详情不可恢复，回退需匹配代码版本的整库备份。

## 使用

### 启用原型

1. 安装并启用插件（验证基线 AstrBot 4.28.1）。
2. 插件详情 → Pages → `control`，在接入区读取已有目标或输入新平台 ID；手填连接信息，或显式创建官方扫码任务。仅体验布局时无需启用平台。
3. 保存连接配置不会自动连接；确认配置后启用并重载。扫码租约30秒、最长180秒；取消/关闭页面或到期会清理待提交凭据，不授予管理员权限。
4. 下方目录实例独立选择；编辑布局后先预览、再保存草稿，按需应用到本地。插件热重载不会自动重启平台。

目录预览基于本体默认配置，不代表所有会话的权限或前缀。关闭页面 API 后可从插件设置重新打开。

聊天按当前前缀使用 `v2menu`（示例 `/v2menu`），支持 `system`、`plugins`、`search 0 关键词` 及返回/翻页；`v2menu md` 请求 Markdown，确定的权限拒绝才退回纯文本。群/文字子频道链接只预填，必要时真实 @ 机器人再发送。保持本插件在当前会话 `plugin_set` 中启用；不会伪造 At 或放宽管理员权限。

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
│   ├── protocol.py       #   P0 契约：信封/去重/错误/头像/身份缓存
│   ├── transport/        #   HTTP/token、WS、Webhook 与有界原始收件箱
│   ├── messaging/        #   真实聊天消费、持久身份/额度、统一发送
│   ├── help.py           #   文本与 Markdown 指令帮助
│   ├── panels.py         #   有所有权保护的托管面板
│   ├── connections.py    #   本体连接配置写入口与重载
│   ├── onboarding.py     #   扫码租约、解密与提交
│   ├── commands.py       #   真实指令目录
│   ├── settings.py       #   本地 SQLite 配置存储
│   ├── host_auth.py      #   管理鉴权
│   └── web_api.py        #   Pages 控制台 API
├── pages/control/        # Plugin Pages 控制台前端
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
