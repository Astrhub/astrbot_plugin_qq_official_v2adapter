# 进程内 QQ V2 SDK（M0–M3）

从 V2 事件的 `event.bot.qq` 或平台的 `adapter.get_client().qq` 获取当前实例视图；不另开网络服务。原生群成员读取 `.get_group_member_info(group_openid, member_openid)`、`.get_group_member_list(group_openid, cursor="")` 返回 QQ 原始 dict，后者仅一页；`iter_group_members(group_openid)` 逐页迭代，不能当作原子名单。旧 `client.api.get_group_member_info/get_group_member_list/get_group_info` 保留 OneBot 投影与现有聚合行为，同名不能互换。其余原生方法逐项状态见 [覆盖台账](SDK_COVERAGE.md)。

```python
client = adapter.get_client()
member = await client.qq.get_group_member_info("群 OpenID", "成员 OpenID")
sub = client.qq.events.subscribe({"GROUP_MEMBER_ADD", "GROUP_MEMBER_REMOVE"},
                                 callback=on_member, owner=self)
# 插件 terminate 时：await sub.close()
history = await client.qq.profiles.get_member("群 OpenID", "成员 OpenID", mode="cache_only")
```

回调必须是异步函数；队列默认 64 项，单实例最多 32 个订阅和 16 MiB 待消费事件。每订阅顺序执行，默认 5 秒超时；异常隔离，队列满时关闭该句柄并报告 `subscription_gap`。`close()` 幂等、owner 卸载或平台重载撤销句柄。`events.stream(names, owner=...)` 返回同语义异步流；`include_recovered=True` 才观察启动时未被核心处置的旧收件，进程崩溃、未订阅期或跨分片不保证 exactly-once 或全局顺序。`NativeEvent.payload/d` 递归只读，`raw()` 返回隔离副本；`key` 优先取机器人+真实外层事件 ID，缺失时用本地实例代次/收件凭据（不保证跨重启等价）。`context` 分开记录代次、传输、分片、session 和收件凭据。连接 `RESUMED.d` 可以是空串，Webhook 没有 WS 游标；`reply_context` 仅真实且未过期的当前消息。宿主排队、观察者发布、核心处置、收件接受各有独立进度；共享收件箱仍保持有界背压。
RawInbox `core_state` 为 `pending → done/degraded/invalid`：`degraded` 保留容量错误并继续宿主聊天投递，`invalid` 进入可检索的留置区；过深原始数据仅推送留置凭据的最小诊断，完整内容留在操作员收件箱。非聊天有效通知由核心确认，聊天另由宿主交付确认。核心标记后/收件确认前崩溃时只补确认，不重复应用成员变化或 ACK；跨库中断时重做幂等资料合并，非聊天观察仍仅实时交付。
`client.qq.events.progress(event.context.receipt)` 可在同实例查询 core/host/raw 进度；确认后 tombstone 保留最多 300 秒且受容量裁剪，过期返回 `event_not_found`，不代表 QQ 端业务已完成。

资料独立存 `profiles.sqlite3`：`cache_only` 绝不联网，`prefer_cache` 在有昵称时复用（stale 仅作提示），`refresh` 总是明确访问 QQ；确定性失败冷却时缓存回退附 `refresh_error`，权限不足/限流不会变成退群。字段含 `source/as_of/received`，成员状态 `present/left/unknown` 与 `last_known_role` 分开，历史昵称不授予当前管理员权限。`list_known_members(group_openid, cursor="", limit=100)` 列历史已知资料，`refresh_roster(group_openid)` 显式拉取完整名单，`get_roster_status(group_openid)` 表示当前名单完整性，不把历史成员冒充当前名单。未启用成员 Intent、Webhook 未配置监听、Identify/离线缺口或库写失败时 `continuous=False`；重启后保留资料，当前名单须重新核实。入站内容不按消息逐条补查 QQ。

插件顶层配置 `profile_max_records`（32768）、`profile_max_bytes`（64 MiB）、`profile_stale_seconds`（300）、`profile_cooldown_seconds`（600）默认保持旧配置；调低容量不清除历史。升级 `messaging.sqlite3` v2→v3、`transport.sqlite3` v3→v4、新建 profiles v1 前先停机或使用 SQLite backup API 做一致性备份；回退恢复匹配旧代码的**整套**配置与数据库。未知发送结果继续不自动重放。默认 Intents 未增加成员位 24；仅在取得权限并配置 WS 掩码或 Webhook 管理台监听后显式启用。
