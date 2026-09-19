# QQ 官方 V2 适配器 (astrbot_plugin_qq_official_v2adapter)

独立的 AstrBot `qq_official_v2` 平台适配器，提供 QQ 官方协议连接、独立扫码接入、本地配置控制台与 OneBot v11 风格调用骨架。当前 **v0.2.0 是连接层预览版，尚不提供聊天消息转换与发送**。

## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.12 | |
| AstrBot | >= v4.28.1, < v4.29 | 平台注册与 Plugin Pages |

**平台支持**：仅本插件注册的 `qq_official_v2`；不依赖原生 QQ 适配器或旧补丁插件。HTTP/token、WebSocket、统一 Webhook 和扫码流程已实现，但未做真实 QQ 联调；连接状态不代表聊天能力已实现。

## 功能

当前可用：

- 平台注册/注销、多实例身份与代次隔离、禁用及失败清理
- 有界 HTTP/token 重试、WS 心跳与恢复、Webhook 原始体验签及持久收件
- 独立扫码短租约、一次性凭据提交、手填配置；保存与重载分离
- Plugin Pages 控制台：真实指令目录、参数帮助、自选快捷项、分级布局、本地预览
- 非敏感配置的草稿、版本冲突检测、应用、撤销与恢复；重启保留
- OneBot v11 风格 OpenID 调用骨架；未实现动作明确报错，不返回假消息 ID

尚未实现：AstrBot 聊天事件转换、消息发送、远端面板发布、媒体、OneBot 网络接口。原始事件不触发 LLM 或插件指令。

## 安装

1. 在 AstrBot 插件界面选择从链接安装：`https://github.com/Astrhub/astrbot_plugin_qq_official_v2adapter`
2. 或手动克隆到 `data/plugins/` 后在 WebUI 重载

## 配置

### 基础开关（插件设置）

| 配置项 | 类型 | 默认 | 说明 |
|--------|------|------|------|
| `webui_enabled` | bool | true | 启用 V2 管理页面 API |
| `remote_menu_sync` | bool | false | 远端菜单自动同步总闸（原型未实现，打开也不会发布） |
| `onebot_network_enabled` | bool | false | OneBot 网络总闸（原型未实现，打开也不会监听） |

### 连接与凭据

在 Pages 接入区或本体平台管理维护 AppID、AppSecret、环境、传输、intents/shard；两者都以本体平台配置为唯一权威存储。未编辑 secret 时保留原值，替换/清空和改绑均需确认；服务端不返回原 secret，浏览器不持久保存凭据。扫码提交后保持禁用，需另行启用并重载。

生产 OpenAPI 使用 `https://api.bot.qq.com`，WSS 仅接受 `wss://api.bot.qq.com`；不后备到旧域名。**沙箱网络接入暂不支持**：现行官方环境选择规则尚未确认，保留 `sandbox` 配置和本地数据，但在 token、连接及扫码创建前报 `unsupported_environment`，不会自动转向生产。

布局、快捷项等保存在 `data/plugin_data/astrbot_plugin_qq_official_v2adapter/settings.sqlite3`。连接层原始收件暂存同目录 `transport.sqlite3`（全局1024条/64 MiB、每实例256条）；满额会拒收，不删除未交付事件。损坏时停止使用并备份后处理，不自动清空。当前没有 P3 业务消费，不宜作为生产收发机器人部署。

## 使用

### 启用原型

1. 安装并启用插件（验证基线 AstrBot 4.28.1）。
2. 插件详情 → Pages → `control`，在接入区读取已有目标或输入新平台 ID；手填连接信息，或显式创建官方扫码任务。仅体验布局时无需启用平台。
3. 保存连接配置不会自动连接；确认配置后启用并重载。扫码租约30秒、最长180秒；取消/关闭页面或到期会清理待提交凭据，不授予管理员权限。
4. 下方目录实例独立选择；编辑布局后先预览、再保存草稿，按需应用到本地。插件热重载不会自动重启平台。

目录预览基于本体默认配置，不代表所有会话的权限或前缀。关闭页面 API 后可从插件设置重新打开。

WebSocket 鉴权/恢复成功才报告在线，重连预算耗尽后需检查配置并手动重载。Webhook 地址在接入区显示，沿用本体统一入口；需自行配置公网 HTTPS 反代及 QQ 允许的端口。`webhook_ready` 只表示本地可接收，回答验证不证明 QQ 已验证；`online` 表示近5分钟有合法签名回调。接入区“读取”可刷新状态。

保存前检查目标指纹和本体内存/磁盘是否一致，冲突时先重新读取，不覆盖外部改动；宿主没有跨进程原子 CAS。保存失败保留原运行配置，重载/连接失败保留已保存配置供修复；旧代次停止，不切回旧适配器。

### 进程内接口

```python
client = platform.get_client()
status = await client.api.call_action("get_status")  # 真实连接状态，另有 message_delivery 限制
await client.call_action("send_group_msg", group_id="真实群OpenID", message="测试")
# 当前抛出 V2Error(code="unsupported")；client.send_group_msg(...) 同样如此。
```

官方 ID 保持字符串，不伪造 QQ 号或 OneBot 数字 ID。身份缓存查询要求真实聊天观察及明确场景；头像构造不证明身份存在，未实现动作报错而非假成功。

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
