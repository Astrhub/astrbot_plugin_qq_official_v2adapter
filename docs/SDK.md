# 进程内 QQ V2 SDK（M0–M7 首版候选）

从 V2 事件的 `event.bot.qq` 或平台的 `adapter.get_client().qq` 获取当前实例视图；不另开网络服务。原生群成员读取 `.get_group_member_info(group_openid, member_openid)`、`.get_group_member_list(group_openid, cursor="")` 返回 QQ 原始 dict，后者仅一页；`iter_group_members(group_openid)` 逐页迭代，不能当作原子名单。旧 `client.api.get_group_member_info/get_group_member_list/get_group_info` 保留 OneBot 投影与现有聚合行为，同名不能互换。其余原生方法逐项状态见 [覆盖台账](SDK_COVERAGE.md)。

```python
client = adapter.get_client()
member = await client.qq.get_group_member_info("群 OpenID", "成员 OpenID")
sub = client.qq.events.subscribe({"GROUP_MEMBER_ADD", "GROUP_MEMBER_REMOVE"},
                                 callback=on_member, owner=self)
# 插件 terminate 时：await sub.close()
history = await client.qq.profiles.get_member("群 OpenID", "成员 OpenID", mode="cache_only")
```

原生只读用 `await client.qq.get_guild(guild_id)`、`await client.qq.get_panels("group", limit=20)` 请求 QQ；无冲突方法也可经 `client.api.get_guild` 调用，`call_action` 与网络动作表不随之扩权。带 `cursor/after/cookie` 的单次方法保留原始返回；`get_schedules(since)` 用 GET JSON 的 uint64 毫秒数，`get_threads` 仅 `is_finish`、无续页游标。`iter_*` 有 200 页／5000 项／4 MiB／120 秒上限，途中失败会抛错，不能把已 yield 的部分名单当作完整快照；`complete_threads` 对未完成回复报错。A011/A013/A060 属 SDK 1.2.1 兼容路径，现行专页与账号权限未确认。
```python
view = client.qq.with_options(operation_id="my-send", owner=self)
sent = await view.post_group_message("群 OpenID", content="你好", msg_id="入站消息 ID")
uploaded = await client.qq.post_group_file("群 OpenID", 1, "https://example.org/photo.png", srv_send_msg=False)
await client.qq.post_group_message("群 OpenID", msg_type=7, media={"file_info": uploaded["file_info"]}, msg_id="入站消息 ID")
```
原生发送保留官方 JSON 字段和原始响应，`operation_id`/`owner` 仅是本地 SDK 选项，不进入 QQ 请求；同一操作 ID 的不同来源、目标或内容冲突，已确认但含敏感字段的原生回执重复查询不重放。`msg_id`/`event_id` 二选一；有来源时自动序号与显式正整数共用发送账本，已知错机器人／错目标来源拒绝，未知 caller-supplied 来源由 QQ 判定有效期和权限。显式原生目标不会暗改主动/被动模式；原有 `client.qq.send` 和 OneBot 便捷发送保留 QQ 明确被动过期/次数拒绝后最多一次主动降级，超时、5xx、unknown 与已发部分流绝不从头补发。
原生 C2C 流单片 `post_c2c_stream_message` 要求 index 从 0 递增、续片带 QQ 首片返回的 `stream_msg_id`，全流复用同一来源序号；未知片或重启中的不确定结果阻止续发。频道可用 `post_message(file_image=bytes_or_stream)`，SDK 只关闭自建临时副本，调用者的流自行管理。本机路径通过宿主 MediaResolver；HTTP(S) 图片/富媒体 URL 只交 QQ 转存，本机不 GET/HEAD；`begin_upload(...)` 返回需要 `put_part(server_index) → finish_part(server_index) → complete()` 的可关闭句柄，预签名 PUT 没有 QQ 鉴权、Cookie 或跳转，服务端索引保留 0/1 起点。`post_group_file/post_c2c_file(..., srv_send_msg=True)` 是真实发送，进入消息账本。
```python
async with await client.qq.begin_upload("group", "群 OpenID", local_path, kind="image", name="photo.png") as task:
    for part in task.parts:
        await task.put_part(part["index"])
        await task.finish_part(part["index"])
    uploaded = await task.complete(srv_send_msg=False)
```

群/机器人策略、菜单/面板、频道管理/内容均有具名原生写入口；信任插件可显式传 QQ 参数，但 `management_writes` 默认关闭，QQ 账号权限仍由本次响应决定。群禁言和踢人单次至多 20 人，不自动拆批；名单详情权限不足不阻断其他管理写。部分成功/unknown 查询同一 `operation_id`，不能换新 ID 重做已完成的步骤；策略白名单参数是 QQ 号码字符串，不将 OpenID 反推 QQ 号。修改已托管面板会暂停该面板的自动同步，须由 Pages 显式确认恢复。
```python
await client.qq.with_options(operation_id="manage-001", owner=self).set_group_member_blacklist(
    "群 OpenID", "add", ["成员 OpenID"])
await client.qq.put_menu({"items": []}, operation_id="menu-001")
```
纯 HTTPS 的撤回/频道修改不依赖接收 WS/Webhook ready；新消息/流、`srv_send_msg=True` 及媒体发送仍受原有接收连接条件限制。


原生撤回、`create_dms`、`patch_guild_message` 需显式启用既有 `management_writes`；非托管互动经 `on_interaction_result(d.id, code)` 共享 ACK 台账，相同 code 查询原结果、不同 code 冲突，已由核心持有的按钮／菜单 ACK 即使票据无效也不能被 SDK 接管。权限与限流只以 QQ 本次响应为准。
崩溃后仅当原 `operation_id` 的同机器人、同互动、同请求及 ACK 结果均已确认成功才修复摘要并返回成功；未知、缺失或历史淘汰的回执不会重发。


回调必须是异步函数；队列默认 64 项，单实例最多 32 个订阅和 16 MiB 待消费事件。每订阅顺序执行，默认 5 秒超时；异常隔离，队列满时关闭该句柄并报告 `subscription_gap`。`close()` 幂等、owner 卸载或平台重载撤销句柄。`events.stream(names, owner=...)` 返回同语义异步流；`include_recovered=True` 才观察启动时未被核心处置的旧收件，进程崩溃、未订阅期或跨分片不保证 exactly-once 或全局顺序。`NativeEvent.payload/d` 递归只读，`raw()` 返回隔离副本；`key` 优先取机器人+真实外层事件 ID，缺失时用本地实例代次/收件凭据（不保证跨重启等价）。`context` 分开记录代次、传输、分片、session 和收件凭据。连接 `RESUMED.d` 可以是空串，Webhook 没有 WS 游标；`reply_context` 仅真实且未过期的当前消息。宿主排队、观察者发布、核心处置、收件接受各有独立进度；共享收件箱仍保持有界背压。
RawInbox `core_state` 为 `pending → done/degraded/invalid`：`degraded` 保留容量错误并继续宿主聊天投递，`invalid` 进入可检索的留置区；过深原始数据仅推送留置凭据的最小诊断，完整内容留在操作员收件箱。非聊天有效通知由核心确认，聊天另由宿主交付确认。核心标记后/收件确认前崩溃时只补确认，不重复应用成员变化或 ACK；跨库中断时重做幂等资料合并，非聊天观察仍仅实时交付。
`client.qq.events.progress(event.context.receipt)` 可在同实例查询 core/host/raw 进度；确认后 tombstone 保留最多 300 秒且受容量裁剪，过期返回 `event_not_found`，不代表 QQ 端业务已完成。
`NativeEvent.typed` 按 `t` 提供隔离的 TypedDict 判别视图；只有 `schema_valid=True` 才可消费，缺字段/错类型分别列于 `schema_missing/schema_invalid`，原始 `d/raw()` 不被裁掉。事件形状、当前来源与位 18/19 未确认的说明见 [覆盖台账](SDK_COVERAGE.md)；已知 3 种频道删除事件仅按 SDK 1.2.1 注册名称，现行负载专页未确认，不伪造必填字段。SDK 本地订阅不会改变 WS Intents 或 Webhook 管理端监听。

资料独立存 `profiles.sqlite3`：`cache_only` 绝不联网，`prefer_cache` 在有昵称时复用（stale 仅作提示），`refresh` 总是明确访问 QQ；确定性失败冷却时缓存回退附 `refresh_error`，权限不足/限流不会变成退群。字段含 `source/as_of/received`，成员状态 `present/left/unknown` 与 `last_known_role` 分开，历史昵称不授予当前管理员权限。`list_known_members(group_openid, cursor="", limit=100)` 列历史已知资料，`refresh_roster(group_openid)` 显式拉取完整名单，`get_roster_status(group_openid)` 表示当前名单完整性，不把历史成员冒充当前名单。未启用成员 Intent、Webhook 未配置监听、Identify/离线缺口或库写失败时 `continuous=False`；重启后保留资料，当前名单须重新核实。入站内容不按消息逐条补查 QQ。
容量或存储写故障只使实际机器人/群的当前名单降级，`last_error` 仅作共享诊断；其他群可复用已验证的完整快照，数据库不可读或关闭则明确失败。

AstrBot `V2MessageEvent` 的 6 个公共便捷方法仍保留其边界：`get_group(group_id=None)` 实时请求 QQ，权限拒绝时仅对本事件群返回可信 ID；`send(message)` 与 `send_streaming(generator,use_fallback=False)` 复用旧发送核心/流账本；`upload_group_and_c2c_image(image_base64,file_type,...)`（file_type 仅 1）与 `upload_group_and_c2c_media(file_source,file_type,...)` 必须显式指定 `group_openid` 或 `openid`，共享媒体服务并返回 `V2MediaReceipt`，`srv_send_msg=True` 的派生发送另记账；`post_c2c_message(openid,...)` 转原生字段，旧 `stream` 参数不等价时明确拒绝、改用逐片 `post_c2c_stream_message`。标准 Image/Record/Video/File/Reply 链按需消费，入站转换不下载附件。


插件配置 `profile_max_records`（32768）、`profile_max_bytes`（64 MiB）、`profile_stale_seconds`（300）、`profile_cooldown_seconds`（600）保留默认值与旧历史。升级应停机备份整套配置/数据库：`messaging.sqlite3` v4（来源 event_id）、其 `extension_schema` v4（工具操作归属）、`transport.sqlite3` v4、`profiles.sqlite3` v1；回退恢复匹配代码版本的全套备份，不清 unknown。默认 Intents 不增群成员位 24 或尚未确认的 18/19；必须另行授权显式配置。OneBot 边界见 [ONEBOT](ONEBOT.md)，13 个工具及权限见 [GROUP_TOOLS](GROUP_TOOLS.md)。
