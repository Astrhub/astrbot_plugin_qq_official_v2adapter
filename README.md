# QQ 官方 V2 适配器 (astrbot_plugin_qq_official_v2adapter)

独立的 AstrBot `qq_official_v2`（WebSocket）与 `qq_official_v2_webhook`（Webhook）适配器，共用业务核心，提供可选 OneBot 兼容。

## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.12 | |
| AstrBot | >= v4.28.1 | 平台注册与 Plugin Pages |


## 功能

当前可用：

- 平台注册/注销、多实例身份与代次隔离、禁用及失败清理
- 有界 HTTP/token 重试、WS 心跳与恢复、Webhook 原始体验签及持久收件
- 独立扫码短租约、一次性凭据提交、手填配置；保存与重载分离
- 六类官方聊天事件进入正常 AstrBot 管线；全量群聊兼容结构化自身 @ 标记，身份、回复凭据与发送结果持久化
- 文本 / Markdown / At / Reply 共用发送核心；QQ 限速以实际拒绝响应为准，未知结果不自动重放
- Plugin Pages：真实目录、参数帮助、自选快捷项、分级布局、草稿及版本恢复
- 默认关闭的托管指令面板；预览、应用本地、确认发布相互独立，只修改自有资源
- OneBot `send_group_msg` / `send_private_msg` / `send_msg`，缓存资料和头像 URL 查询
- 外链交QQ转存，本地文件/Base64分片上传；C2C原生流式、其他场景有界聚合、显式C2C typing
- 互动11/12独立ACK、短期点击者票据与正常权限管线；群/频道具名管理、分享及有真实记录的撤回

| 场景 | 基础发送 | 限制 |
|------|----------|------|
| 群聊 / C2C | 文本、Markdown、真实引用索引 | At 仅群聊；群回复5分钟、C2C保守60分钟；次数由QQ判定 |
| 文字子频道 / 频道私信 | 文本、Markdown、真实消息引用 | 需在线 WS；At 仅文字子频道 |
| 所有场景 | 指令帮助、原生格式会话 ID、保守主动发送 | 主动目标须已观察；配额/权限仍以服务端为准 |
| 入站附件 | 完整元数据与原始payload保留 | 不自动下载、转码或作为图片输入LLM；普通聊天链接不触发媒体发送 |

OneBot 接入通过 Pages“连接配置”管理，每实例单独端口和专用token，复用现有动作与发送防重账本；字符串ID与`qq_event`扩展不伪装完整v11。HTTP/WS路由、被动回复、限制和回退见[网络接入说明](docs/ONEBOT.md)。

| 扩展 | 实现与条件 |
|---|---|
| 群/C2C媒体 | 图片、语音、视频、文件；格式接受由QQ判断，URL转存及prepare→PUT→finish→files；整链预检，不静默丢caption或拆多条 |
| 频道/DM图片 | HTTP(S)图片URL直传；频道本地图片另支持multipart。DM本地文件及两场景语音/视频/文件拒绝 |
| 流式/typing | C2C原生流；其他场景先声明聚合模式。typing需开关和被动来源，使用一次回复序号、不续期 |
| 键盘 | 群/C2C开启后使用 `v2menu kb`；导航/详情/预填与无参指令二次确认分开，权限未知，有参不自动执行 |
| 管理/分享 | 原生具名接口；OneBot同步群资料/成员/禁言/移除/审批、真实bot资料与撤回。写入默认关闭，实际权限由QQ决定 |

本地字节超过图片/语音/视频软限制时默认按文件类型提交，可传 `allow_file_fallback=false` 禁止降级；硬限制和本地 `media_max_bytes` 仍生效。上传回执的 `ttl=0` 按长期有效处理，内存缓存最长24小时；分片媒体可在当前成功返回中取得 `raw_url`，预签名地址不写入发送账本。

## 安装

1. 在 AstrBot 插件界面选择从链接安装：`https://github.com/Astrhub/astrbot_plugin_qq_official_v2adapter`
2. 或手动克隆到 `data/plugins/` 后在 WebUI 重载
3. 在平台对应配置档的插件列表中启用本插件，确保群 @ 唤醒和 `v2menu` 等指令可用

## 配置

在本体平台管理选择「QQ 官方 V2（WebSocket）」或「QQ 官方 V2（Webhook）」。基础表单填写 AppID、AppSecret，可切换「沙箱模式」；沙箱端点尚未确认，开启后拒绝联网。实例名称、启用沿用宿主管理。

事件意图、WS 分片和 OneBot 网络均在插件 Pages → `control` 配置，保存后另行重载。新 WS 默认自动发现并启动完整分片组（最多 32 片，超限明确报错）；手动输入片号和总数，仅启动指定片，需自行覆盖完整组。同一机器人不能混用自动、Webhook 与手动接收；不同手动片必须总数一致。

各片独立恢复和推进持久接收序号，共享 AppID/环境的 Identify 额度与每 5 秒创建限制；额度耗尽按重置时间等待，Resume 不消耗新建额度，未知新建不退款。控制台显示建议/计划/已连接片数，部分在线标为 degraded，健康片继续交付和回复；全部离线、心跳失效或代次撤销时停止发送。
单片重连次数耗尽后保留失败诊断并等待管理员重载，其他健康或仍在启动/恢复的片继续运行；全部片终结或发生组级致命错误才关闭整组。

升级初始化会一次性规范已有自有配置：保留原环境、传输与显式手动分片，写入新类型/沙箱开关/分片模式并清理旧别名，实例 ID、AppID、凭据、已有 Webhook UUID 及账本归属不变；矛盾的显式字段报错，保存失败恢复原内存配置，规范化和页面读取不连接 QQ。

升级前备份本体平台配置；回退旧版本需恢复该备份，勿清空消息数据库。

### 基础开关（插件设置）

| 配置项 | 类型 | 默认 | 说明 |
|--------|------|------|------|
| `webui_enabled` | bool | true | 启用 V2 管理页面 API |
| `remote_menu_sync` | bool | false | 托管面板总闸；还须在 Pages 确认具体应用和范围 |
| `onebot_network_enabled` | bool | false | HTTP/正向WS总闸；还须配置实例监听/独立token并重载，默认只读 |


## 使用

### 启用原型

1. 安装并启用插件（验证基线 AstrBot 4.28.1）。
2. 插件详情 → Pages → `control`，读取接入目标后点「生成二维码」，用手机 QQ 扫码再保存凭据。不需要事先填写 AppSecret。保存后机器人仍关闭。
3. 勾选启用并重载才会连接。关闭页面会取消还没保存的二维码；不会授予管理员权限。
4. 下方目录实例独立选择；编辑布局后先预览、再保存草稿，按需应用到本地。插件热重载会恢复已启用的两种自有平台。

### 进程内接口

```python
client = event.bot  # 真实群聊 V2 事件：绑定该事件的被动回复凭据
status = await client.api.call_action("get_status")
result = await client.send_group_msg(group_id=event.route.target, message="你好")
print(result["message_id"])
# client.call_action(...)、client.api.call_action(...) 与同名方法共用发送核心。
```

`event.send`、`send_by_session`、`client.qq.send(scene, target, ...)` 共用策略；无来源的实例调用仅走主动策略，不能借最近消息。CQ 字符串/数组全量校验，`auto_escape=True` 仅对字符串保留字面量；不能等价的组合拒绝，不偷偷拆消息。原生调用可给 `operation_id` 并用 `client.qq.send_status(id)` 查询；未知结果不重试。

媒体可用原生组件或CQ数组`image`/`record`/`video`；文件使用显式`_qq_file`段或`client.qq.send_file(scene, target, input, name=...)`。仅支持一项媒体与可等价引用/文本组合，不接受任意原生写请求。流式先用`client.qq.streaming_mode(scene, target)`检查模式，再调用`event.send_streaming`或`client.qq.send_streaming`。

`client.qq.group_members(group)`、`join_requests(group)`返回完整结果或明确失败；审批flag有效5分钟，只来自真实待处理申请查询，不代表好友或机器人受邀审批。管理/分享/撤回需高级区开启`management_writes`；用`client.qq.extension_status(operation_id)`核对扩展结果，`extension_events()`读取typed状态。已发送后超时/取消、部分成功不能整批重放；来源/路由缺失或压缩后不能伪造撤回目标。

扩展未完成/unknown限2048，ACK独立128，完整终态明细2048；旧结果压缩后保留24小时防重记录，不挤占未完成预算，仍受整库128MiB上限约束。

ID 均为字符串。群/C2C必须提供场景专用OpenID，不用通用 `id` 补缺；频道/DM仍使用其 `id`。`get_stranger_info` 仅查未过期聊天缓存，实例级查询须给 `id_kind` 和 `scope`；`no_cache=True` 不支持。头像优先真实事件 URL。

## 本地测试

复用已有 AstrBot 依赖环境，需要 Linux、bubblewrap、Python 3.12、Node 24：

```bash
ASTRBOT_SOURCE=/patch/AstrBot bash scripts/test-isolated.sh -q
```

脚本使用独立用户/PID/网络命名空间、只读源码与临时数据，QQ 上游仅用本地模拟服务，不读取现有实例配置。依赖 `requirements.txt`。

## 项目结构

```
astrbot_plugin_qq_official_v2adapter/
├── main.py               # 插件主入口：注册/注销两种自有平台
├── assets/               # QQ 官方平台图标与来源许可
├── v2/                   # 适配器核心
│   ├── adapter.py        #   平台适配器与实例生命周期
│   ├── event.py          #   V2 事件与发送入口
│   ├── client.py         #   OneBot 风格调用客户端
│   ├── protocol.py       #   信封、请求、重放保护、错误与头像契约
│   ├── transport/        #   HTTP/token、WS、Webhook 与有界原始收件箱
│   ├── messaging/        #   真实聊天消费、持久身份/发送账本、统一发送
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
├── pages/control/        # Plugin Pages 
├── tests/                # 测试
├── scripts/              # 隔离测试脚本
├── .github/workflows/    # CI 
├── _conf_schema.json     # 配置文件
└── metadata.yaml         # 插件元数据
```

## 相关链接

- [AstrBot](https://docs.astrbot.app/)
- 平台图标复用 [AstrBot 的 QQ 图标](https://github.com/AstrBotDevs/AstrBot/blob/8b5e24ba2eac3375a0e17bdf3f0ea790eb91de15/dashboard/src/assets/images/platform_logos/qq.png)，其 Dashboard MIT 许可见 [随附声明](assets/LICENSE.AstrBot-Dashboard)。
- [Issues](https://github.com/Astrhub/astrbot_plugin_qq_official_v2adapter/issues)
- [QQ 机器人官方文档](https://bot.q.qq.com/wiki/develop/api-v2/)

## 许可

AGPL-3.0 License
