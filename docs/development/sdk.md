# SDK 使用指南

## 选择正确的入口

```python
client = event.bot
native = client.qq       # 官方 V2 具名 API，保留官方字段和响应
compat = client.api       # OneBot v11 风格 OpenID 子集
```

`client.qq.get_group_member_info(group, member)` 返回 QQ 原始响应；`client.api.get_group_member_info(group_id=..., user_id=...)` 返回兼容投影。两者同名但语义不同，不能混用。`client.call_action()` 与 OneBot 分派共用同一发送核心，不会因为调用入口不同而扩大权限。

## 发送与读取

```python
sent = await event.bot.qq.send("group", event.route.target, "你好")
status = event.bot.qq.send_status(sent["operation_id"])
member = await event.bot.qq.get_group_member_info(event.route.target, "成员 OpenID")
```

推荐在收到事件后使用 `event.send(...)`；需要明确场景和目标时使用 `event.bot.qq.send(...)`。主动发送必须遵守 QQ 的观察/权限规则。原生 API 也可以通过 `with_options(operation_id="...", owner=self)` 固定本地操作 ID 和 owner 生命周期。

只读接口返回官方 JSON；带 `cursor`/`after` 的方法只返回一页。`iter_*` 便利方法有 200 页、5000 项、4 MiB 和 120 秒上限，途中失败会抛错，不能把已 yield 的部分当成原子快照。

## API 分组

原生 API 位于 `v2/sdk/api/`：

- `reads.py`：网关、机器人资料、群/频道/成员、菜单/面板和分页读取；
- `messages.py`：群/C2C/频道/DM 消息、撤回、修改和互动 ACK；
- `media.py`：文件发送、上传准备、分片 PUT/finish/complete；
- `group_admin.py`：群禁言、踢人、黑名单和入群申请；
- `guild_admin.py`：频道、成员、角色、权限、公告、日程、语音和帖子；
- `menus.py`：菜单与面板管理。

目录登记 98 个 HTTP target，其中 96 个已实现/可用、2 个明确 excluded；另有 64 个旧 SDK 风格具名方法。逐项目录见 [SDK 覆盖台账](../SDK_COVERAGE.md)；实现不代表账号一定有权调用。

## 管理写入

管理、撤回、菜单和面板写入受 `management_writes` 和 QQ 权限共同控制，默认关闭。原生接口保留官方 JSON 结构，不允许通过任意 URL 绕过共享 sender。

部分成功或 unknown 时先查询原 `operation_id`：

```python
result = await event.bot.qq.with_options(
    operation_id="moderation-42", owner=self
).set_group_member_blacklist("群 OpenID", "add", ["成员 OpenID"])

state = event.bot.qq.extension_status("moderation-42")
```

不要更换 operation ID 重做同一未知操作；账本会阻止相同逻辑操作的冲突请求。
