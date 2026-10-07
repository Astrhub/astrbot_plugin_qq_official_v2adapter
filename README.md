# QQ 官方 V2 适配器 (astrbot_plugin_qq_official_v2adapter)

接入 QQ 官方开放平台的 AstrBot 平台适配器，支持 WebSocket 与 Webhook 接收方式，覆盖群聊、C2C、频道文字子频道和频道私信。
> [!TIP]
> 这是 QQ 官方机器人适配器。路由、OpenID 与 OneBot ID 均按字符串处理；个别官方管理字段仍按 QQ API 类型校验。

## 环境要求

| 依赖 | 版本要求 | 说明 |
| --- | --- | --- |
| AstrBot | `>= 4.24.2` | 平台注册与 Plugin Pages |

**平台支持**：QQ 官方 WebSocket、QQ 官方 Webhook。

## 功能

- WebSocket 接收：主动连接 QQ 官方网关，支持自动分片、手动分片、心跳和断线恢复。
- Webhook 接收：验签后把原始事件写入持久收件箱，再交给 AstrBot 处理。
- 消息场景：群聊、C2C、频道文字子频道和频道私信。
- 消息格式：文本、Markdown、At、Reply、QQ 原生 Json 卡片、键盘和模板卡片。
- 媒体发送：群/C2C 支持图片、语音、视频和文件；频道支持图片 URL 或本地 multipart；DM 支持图片 URL。
- 流式与输入状态：C2C 使用 QQ 原生流；其他场景可配置有界聚合或明确拒绝，C2C typing 受 `typing_enabled` 控制。
- 管理能力：群成员、禁言、踢出、黑名单、入群申请、频道管理、撤回、菜单和面板。
- 原生 SDK：`event.bot.qq` 提供具名 API、事件订阅、媒体上传、管理 API 和操作状态查询。
- 管理页面：Plugin Pages 的 `control` 页面提供连接、扫码、菜单草稿、预览、托管面板和保留事件管理；Vue 源码位于 `dash/`。

## 页面预览

Vue 控制页候选实现提供连接状态、SDK 能力、本地菜单和 QQ 面板的统一入口。下面用同一份示例数据展示桌面和手机宽度下的布局；手机端会将内容卡片按纵向排列，方便触屏操作。

> [!NOTE]
> 图片只展示页面结构和响应式排版，使用内存示例数据。

<table>
  <tr>
    <td width="72%" align="center">
      <img src="https://raw.githubusercontent.com/Astrhub/astrbot_plugin_qq_official_v2adapter/04af399a33dcd5c83b37fd8af55f8a7c5fddedff/docs/assets/vue-control-preview-pc.svg" alt="QQ 官方 V2 SDK 控制页桌面预览" width="100%" />
    </td>
    <td width="28%" align="center">
      <img src="https://raw.githubusercontent.com/Astrhub/astrbot_plugin_qq_official_v2adapter/04af399a33dcd5c83b37fd8af55f8a7c5fddedff/docs/assets/vue-control-preview-mobile.svg" alt="QQ 官方 V2 SDK 控制页手机预览" width="100%" />
    </td>
  </tr>
  <tr>
    <td align="center"><sub>桌面宽度：双栏连接状态与配置</sub></td>
    <td align="center"><sub>手机宽度：卡片纵向排列</sub></td>
  </tr>
</table>

正式的 Plugin Pages 入口仍是 `pages/control`；Vue 源码和迁移阶段的构建产物分别位于 `dash/` 和 `pages/test`。

## 安装

### 两种方式
**插件市场**：搜索 `QQ 官方 V2 适配器` 并安装。

**链接安装**： 在 AstrBot 插件管理中选择“从 Git 仓库安装”，输入：

   ```text
   https://github.com/Astrhub/astrbot_plugin_qq_official_v2adapter
   ```



## 配置

### 平台接收方式

| 类型 | 适用场景 | 要求 |
| --- | --- | --- |
| `qq_official_v2` | WebSocket 接收 | AstrBot 可以访问 QQ 官方网关；无需公网入站端口 |
| `qq_official_v2_webhook` | Webhook 接收 | QQ 可以访问 AstrBot 的统一 HTTPS 地址 |

同一个机器人不能同时启用冲突的 WebSocket、Webhook 或分片实例。不同实例也不能重复占用同一个机器人和冲突的分片。

### 平台配置

在 AstrBot 平台配置中填写 AppID 与 AppSecret。插件当前只连接正式环境，旧配置中的 `environment` 和 `is_sandbox` 会被忽略。

Webhook 实例还需要唯一的 `webhook_uuid`，QQ 回调地址为：

```text
/api/platform/webhook/{webhook_uuid}
```

WebSocket 支持自动分片和手动分片。自动模式按 QQ 网关返回的推荐值启动完整分片组，插件自身最多启动 32 片；手动模式使用 `[index, count]` 指定分片。

启用 `webui_enabled` 后，也可以在插件 Pages 的 `control` 页面扫码绑定：生成二维码、手机 QQ 扫码、确认凭据并提交，再回到平台配置启用实例并重载。扫码租约短期有效，过期后需要重新生成。

### 插件设置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | ---: | --- |
| `webui_enabled` | bool | `true` | 提供 V2 Pages 管理接口 |
| `remote_menu_sync` | bool | `false` | 允许托管面板同步到 QQ |
| `onebot_network_enabled` | bool | `false` | 旧 OneBot HTTP/正向 WebSocket 入口，计划废弃 |
| `profile_max_records` | int | `32768` | 资料库记录上限 |
| `profile_max_bytes` | int | `64 MiB` | 资料库字节上限 |
| `profile_stale_seconds` | int | `300` | 资料标记为过时前的秒数 |
| `profile_cooldown_seconds` | int | `600` | 资料查询失败后的冷却时间 |

### 高级设置

Pages 的本地设置支持草稿、应用、回滚和版本恢复。常用默认值如下：

| 配置项 | 默认值 |
| --- | ---: |
| `media_max_bytes` | `32000000` |
| `stream_fallback` | `aggregate` |
| `stream_max_chars` | `4096` |
| `stream_timeout` | `120` 秒 |
| `typing_enabled` | `false` |
| `keyboard_enabled` | `false` |
| `keyboard_execute` | `false` |
| `ticket_ttl` | `120` 秒 |
| `management_writes` | `false` |

`apply` 只应用到本地运行设置。远端菜单和面板还需要开启 `remote_menu_sync`，并在 Pages 中确认具体作用域。

### 消息格式

平台配置中的 `use_markdown` 默认是 `true`。它只影响没有显式指定格式的普通文本回复和文本流；图片、文件、卡片和显式指定的消息格式不会被它改写。

## 使用

### 五分钟接入

1. 创建 QQ 官方机器人，准备 AppID 与 AppSecret。
2. 在 AstrBot 中选择 WebSocket 或 Webhook 平台类型，填写平台配置并启用实例。
3. 保存后重载平台，在 Pages 状态页确认 `online` 和 `message_ready`。
4. 在已配置的群中 @机器人发送消息，或向机器人发送 C2C 消息。

插件注册了 `v2menu` 帮助命令：

```text
v2menu
v2menu md
v2menu kb
```

群聊菜单需要真实的 @ 唤醒。帮助、Markdown 或键盘在当前场景不可用时会按实现规则回退或返回明确错误。

### 进程内 SDK

具体查看[SDK 使用指南](docs/development/sdk.md)。管理页面的 Vue 前端通过认证的 `ControlAPI` 使用同一套 SDK 服务和操作状态，接入边界见[Vue 控制页面与 SDK 集成](docs/development/vue-control-page.md)。

### 发送范围

| 场景 | 支持 | 约束 |
| --- | --- | --- |
| 群聊 | 文本、Markdown、At、Reply、媒体、Json 卡片、键盘 | 被动回复来源通常有效 5 分钟；主动发送受观察和 QQ 权限限制 |
| C2C | 文本、Markdown、媒体、原生流、typing | 被动回复来源使用保守的 60 分钟窗口；typing 需要真实来源 |
| 频道文字子频道 | 文本、Markdown、At、Reply、图片 | 出站发送需要在线 WebSocket |
| 频道私信 | 文本、Markdown、图片 | 出站发送需要在线 WebSocket，DM 只支持图片 URL |

入站附件只在消费时按需读取，不会收到一条消息就下载所有附件。无法安全转换的媒体会保留为文件或 Unknown，并保留原始元数据。

## 旧 OneBot 网络入口（计划废弃）

> [!WARNING]
>当前版本仍保留独立 OneBot 网络入口，用于已有外部客户端迁移。新插件和新集成直接使用进程内 SDK。迁移完成后可以关闭全局 `onebot_network_enabled`，并移除实例中的 `onebot` 监听配置。

这是 OpenID 子集，完整边界见 [旧 OneBot 网络说明](docs/ONEBOT.md)。从网络调用迁移到 SDK 的对应关系见 [SDK 迁移指南](docs/development/sdk-migration.md)。

控制页面不通过 OneBot 网络入口执行 QQ 操作。页面请求先经过 Dashboard 管理员认证、CSRF、实例指纹和版本检查，再由 `ControlAPI` 调用连接管理、设置存储、`PanelService` 托管状态机和当前 SDK 服务。已有外部客户端仍可在迁移期间使用 OneBot 网络入口；新页面和新插件应直接使用进程内 SDK。

## Vue 控制页面开发

稳定的 Plugin Pages 入口是 `pages/control/index.html`。`dash/` 是 Vue 源码，迁移阶段的实验构建产物可以输出到 `pages/test`；实验页面不会替代 `control`，也不应当被文档当作正式入口。前端开发、ControlAPI 接口和统一操作调度见[Vue 控制页面与 SDK 集成](docs/development/vue-control-page.md)。

## 限制与排障

- QQ 权限、配额和频控以当次服务端响应为准。
- QQ 使用场景专用 OpenID 和官方 ID；不要把 QQ 号、群号或频道 ID 互相替换。
- 超时、断线、`unknown`、`result_unknown` 或部分成功不会自动重放。先用原 `operation_id` 查询状态。
- 管理写操作和按钮回调默认关闭，开启后仍需满足当前实例、会话、操作者和 QQ 权限条件。
- Webhook 收到 HTTP 200 只表示请求验签并进入接收流程，不代表消息已经完成 AstrBot 投递。

常见错误和处理方法见 [开发者排障](docs/development/troubleshooting.md)。

## 项目结构

```text
astrbot_plugin_qq_official_v2adapter/
├── main.py                         # 插件注册与宿主入口
├── _conf_schema.json               # 插件设置 Schema
├── metadata.yaml                   # 插件元数据
├── dash/                           # Vue 控制页面源码与 Vite 构建配置
├── pages/control/                  # 稳定的 Plugin Pages 控制台入口
├── v2/
│   ├── adapter.py                  # 平台实例、重载与生命周期
│   ├── client.py                   # V2Client 与 NativeView
│   ├── event.py                    # AstrBot 聊天事件
│   ├── network.py                  # 旧 OneBot 网络入口
│   ├── sdk/                        # 原生 SDK、事件和 HTTP 目录
│   ├── messaging/                  # 消息转换、发送与账本
│   ├── media/                      # 媒体读取与上传
│   ├── profiles/                   # 资料缓存与名单状态
│   └── extensions/                 # 按钮、菜单和管理扩展
├── tests/                          # 回归测试
├── docs/                           # 开发者文档
├── requirements.txt
└── README.md
```

## 开发者文档

- [开发者文档索引](docs/README.md)
- [SDK 使用指南](docs/development/sdk.md)
- [Vue 控制页面与 SDK 集成](docs/development/vue-control-page.md)
- [从旧 OneBot 网络入口迁移到 SDK](docs/development/sdk-migration.md)
- [事件模型](docs/development/events.md)
- [发送、媒体与幂等](docs/development/delivery-and-media.md)
- [连接、代次与配置](docs/development/lifecycle-and-configuration.md)
- [旧 OneBot 网络说明](docs/ONEBOT.md)
- [测试与 CI](docs/development/testing.md)

## 支持

- [AstrBot 文档](https://docs.astrbot.app/)
- [QQ 机器人官方 API V2](https://bot.q.qq.com/wiki/develop/api-v2/)
- [Issue 区](https://github.com/Astrhub/astrbot_plugin_qq_official_v2adapter/issues)

## 社区
> [!NOTE]
> 欢迎加入QQ群 **1073129740** 一起来讨论

## 许可

本项目使用 [AGPL-3.0](LICENSE)。平台图标的来源与许可见 [assets/LICENSE.AstrBot-Dashboard](assets/LICENSE.AstrBot-Dashboard)。
