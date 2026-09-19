# QQ 官方 V2 适配器 (astrbot_plugin_qq_official_v2adapter)

独立的 AstrBot `qq_official_v2` 平台适配器，直接接入 QQ 官方机器人协议，提供本地配置控制台与 OneBot v11 风格调用骨架。当前 **v0.1.0 为 P0/P1 骨架原型，不是可收发消息的机器人**。

## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.12 | |
| AstrBot | >= v4.28.1, < v4.29 | 平台注册与 Plugin Pages |

**平台支持**: 仅本插件注册的 `qq_official_v2` 平台类型。QQ HTTP/WS/Webhook 传输尚未实现，启用平台后出现 `transport_not_ready` 属预期行为，不代表 QQ 在线。

## 功能

当前可用：

- 平台注册/注销、多实例身份与代次隔离、禁用及失败清理
- Plugin Pages 控制台：真实指令目录、参数帮助、自选快捷项、分级布局、本地预览
- 非敏感配置的草稿、版本冲突检测、应用、撤销与恢复；重启保留
- OneBot v11 风格 OpenID 调用骨架；未实现动作明确报错，不返回假消息 ID

尚未实现：QQ 传输与扫码、真实收发、远端面板发布、媒体、OneBot 网络接口。

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

AppID、AppSecret、环境、传输、intents/shard 只在本体平台管理中维护，插件不保存凭据。其余设置（布局、快捷项等）在 Pages 控制台修改，版本化持久化于 `data/plugin_data/astrbot_plugin_qq_official_v2adapter/settings.sqlite3`；文件损坏时拒绝使用、不自动清空，先停用插件并备份后再处理。

## 使用

### 启用原型

1. 安装并启用插件（验证基线 AstrBot 4.28.1）。
2. 本体平台管理 → 添加 **QQ 官方 V2 · 原型**；无需真实凭据即可体验本地能力。
3. 插件详情 → Pages → `control`：读取配置与真实目录，编辑后先预览、再保存草稿，按需应用到本地。
4. 插件热重载不会自动重启平台；需要时从本体平台管理重载。

目录预览基于本体默认配置，不代表所有会话的权限或前缀。关闭页面 API 后可从插件设置重新打开。

### 进程内接口

```python
client = platform.get_client()
status = await client.api.call_action("get_status")  # online=False
await client.call_action("send_group_msg", group_id="真实群OpenID", message="测试")
# 当前抛出 V2Error(code="transport_not_ready")；client.send_group_msg(...) 同样如此。
```

官方 ID 保持字符串，不伪造 QQ 号或 OneBot 数字 ID。身份缓存查询要求真实聊天观察及明确场景；头像构造不证明身份存在，未实现动作报错而非假成功。

## 本地测试

复用已有 AstrBot 依赖环境，需要 Linux、bubblewrap、Python 3.12、Node 24：

```bash
ASTRBOT_SOURCE=/root/work/AstrBot bash scripts/test-isolated.sh -q
```

脚本使用独立用户/PID/网络命名空间、只读源码与临时数据，不读取现有实例配置。无额外插件运行依赖；候选 QQ SDK 尚未引入。

## 项目结构

```
astrbot_plugin_qq_official_v2adapter/
├── main.py               # 插件主入口：注册/注销 qq_official_v2 平台
├── v2/                   # 适配器核心
│   ├── adapter.py        #   平台适配器与实例生命周期
│   ├── event.py          #   V2 事件与发送入口
│   ├── client.py         #   OneBot 风格调用客户端
│   ├── protocol.py       #   P0 契约：信封/去重/错误/头像/身份缓存
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
