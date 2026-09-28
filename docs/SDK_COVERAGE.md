# QQ V2 SDK 覆盖范围

| ID | 官方方法及路径 | 原生方法/归属 | 类别/分页 | 首版状态 |
| --- | --- | --- | --- | --- |
| A001 | GET /gateway | get_gateway M4 | 读 | native |
| A002 | GET /gateway/bot | get_ws_url, get_gateway_bot M4 | 读 | native |
| A003 | GET /users/@me | me M4 | 读 | native |
| A004 | GET /users/@me/guilds | me_guilds M4 | 读/页 | native |
| A005 | POST /v2/generate_url_link | generate_url_link M6 | 写/链接副作用 | native |
| A006 | POST /v2/groups/{group_openid}/messages | post_group_message M5 | 写/消息/序号 | native |
| A007 | DELETE /v2/groups/{group_openid}/messages/{message_id} | recall_group_message M5 | 写/撤回 | native |
| A008 | POST /v2/users/{user_openid}/messages | post_c2c_message M5 | 写/消息/序号 | native |
| A009 | DELETE /v2/users/{user_openid}/messages/{message_id} | recall_c2c_message M5 | 写/撤回 | native |
| A010 | POST /v2/users/{user_openid}/stream_messages | post_c2c_stream_message M5 | 写/流式部分成功 | native |
| A011 | GET /channels/{channel_id}/messages/{message_id} | get_message M4 | 读/SDK 1.2.1 兼容 | native |
| A012 | POST /channels/{channel_id}/messages | post_message, post_keyboard_message M5 | 写/消息 | native |
| A013 | PATCH /channels/{channel_id}/messages/{message_id} | patch_guild_message M5 | 写/SDK 1.2.1 兼容 | native |
| A014 | DELETE /channels/{channel_id}/messages/{message_id} | recall_message M5 | 写/撤回 | native |
| A015 | POST /users/@me/dms | create_dms M5 | 写/会话创建 | native |
| A016 | POST /dms/{guild_id}/messages | post_dms M5 | 写/消息 | native |
| A017 | DELETE /dms/{guild_id}/messages/{message_id} | recall_dms M5 | 写/撤回 | native |
| A018 | POST /v2/groups/{group_openid}/files | post_group_file M5 | 写/上传/可选发送 | native |
| A019 | POST /v2/users/{user_openid}/files | post_c2c_file M5 | 写/上传/可选发送 | native |
| A020 | POST /v2/groups/{group_id}/upload_prepare | post_group_upload_prepare M5 | 写/上传准备 | native |
| A021 | POST /v2/groups/{group_id}/upload_part_finish | post_group_upload_part_finish M5 | 写/分片完成 | native |
| A022 | POST /v2/users/{user_id}/upload_prepare | post_c2c_upload_prepare M5 | 写/上传准备 | native |
| A023 | POST /v2/users/{user_id}/upload_part_finish | post_c2c_upload_part_finish M5 | 写/分片完成 | native |
| A024 | GET /v2/groups/{group_openid}/info | get_group_info M4 | 读/OneBot 同名异义 | native |
| A025 | GET /v2/groups/{group_openid}/bot_state | get_group_bot_state M4 | 读 | native |
| A026 | GET /v2/groups/{group_openid}/members/{member_openid} | get_group_member_info M3 | 读/OneBot 同名异义 | native |
| A027 | GET /v2/groups/{group_openid}/members | get_group_member_list M3 | 读/官方 cursor 单页 | native |
| A028 | GET /v2/groups/{group_openid}/restrict_chat_setting | get_group_restrict_chat_setting M4 | 读 | native |
| A029 | POST /v2/groups/{group_openid}/restrict_chat_setting | set_group_restrict_chat_setting M6 | 写/批量≤20 | native |
| A030 | POST /v2/groups/{group_openid}/batch_remove_members | batch_remove_group_members M6 | 写/批量≤20/可部分成功 | native |
| A031 | GET /v2/groups/{group_openid}/member_blacklist | get_group_member_blacklist M4 | 读/游标≤100 | native |
| A032 | POST /v2/groups/{group_openid}/member_blacklist | set_group_member_blacklist M6 | 写/批量≤20 | native |
| A033 | GET /v2/groups/{group_openid}/join_request_list | get_group_join_requests M4 | 读/游标≤50 | native |
| A034 | POST /v2/groups/{group_openid}/approval_join_request/{member_openid} | approve_group_join_request M6 | 写/申请/不可猜 flag | native |
| A035 | GET /v2/groups/join_approval_strategy | get_join_approval_strategies M4 | 读/机器人级游标≤50 | native |
| A036 | POST /v2/groups/join_approval_strategy | create_join_approval_strategy M6 | 写/机器人级 | native |
| A037 | PATCH /v2/groups/join_approval_strategy/{strategy_id} | update_join_approval_strategy M6 | 写/机器人级 | native |
| A038 | DELETE /v2/groups/join_approval_strategy/{strategy_id} | delete_join_approval_strategy M6 | 写/机器人级 | native |
| A039 | POST /v2/groups/join_approval_strategy/{strategy_id}/execute | execute_join_approval_strategy M6 | 写/机器人级 | native |
| A040 | POST /v2/groups/join_approval_strategy/{strategy_id}/whitelist_users | set_join_approval_whitelist M6 | 写/QQ 号码≤10000/不由 OpenID 推导 | native |
| A041 | GET /v2/menu | get_menu M4 | 读 | native |
| A042 | PUT /v2/menu | put_menu M6 | 写/全局版本 | native |
| A043 | GET /v2/panels | get_panels M4 | 读/指定 scope、游标≤50 | native |
| A044 | POST /v2/panels | create_panel M6 | 写/托管对象 | native |
| A045 | GET /v2/panels/{panel_id} | get_panel M4 | 读 | native |
| A046 | PUT /v2/panels/{panel_id} | update_panel M6 | 写/托管对象 | native |
| A047 | DELETE /v2/panels/{panel_id} | delete_panel M6 | 写/托管对象 | native |
| A048 | PUT /v2/panels/{panel_id}/target | set_panel_target M6 | 写/托管对象 | native |
| A049 | PUT /interactions/{interaction_id} | on_interaction_result M5 | 写/ACK 单次 | native |
| A050 | GET /guilds/{guild_id} | get_guild M4 | 读 | native |
| A051 | GET /guilds/{guild_id}/channels | get_channels M4 | 读 | native |
| A052 | POST /guilds/{guild_id}/channels | create_channel M6 | 写 | native |
| A053 | GET /channels/{channel_id} | get_channel M4 | 读 | native |
| A054 | PATCH /channels/{channel_id} | update_channel M6 | 写 | native |
| A055 | DELETE /channels/{channel_id} | delete_channel M6 | 写 | native |
| A056 | GET /guilds/{guild_id}/members | get_guild_members M4 | 读/after 单页 | native |
| A057 | GET /guilds/{guild_id}/members/{user_id} | get_guild_member M4 | 读 | native |
| A058 | DELETE /guilds/{guild_id}/members/{user_id} | get_delete_member, delete_guild_member M6 | 写/即使名称以 get 开头 | native |
| A059 | GET /guilds/{guild_id}/roles/{role_id}/members | get_guild_role_members M4 | 读/页 | native |
| A060 | GET /channels/{channel_id}/voice/members | get_voice_members M4 | 读/SDK 1.2.1 兼容 | native |
| A061 | GET /channels/{channel_id}/online_nums | get_channel_online_nums M4 | 读 | native |
| A062 | PATCH /guilds/{guild_id}/mute | mute_all, cancel_mute_all, mute_multi_member, cancel_mute_multi_member M6 | 写/多目标 | native |
| A063 | PATCH /guilds/{guild_id}/members/{user_id}/mute | mute_member M6 | 写 | native |
| A064 | GET /guilds/{guild_id}/message/setting | get_guild_message_setting M4 | 读 | native |
| A065 | GET /guilds/{guild_id}/roles | get_guild_roles M4 | 读 | native |
| A066 | POST /guilds/{guild_id}/roles | create_guild_role M6 | 写 | native |
| A067 | PATCH /guilds/{guild_id}/roles/{role_id} | update_guild_role M6 | 写 | native |
| A068 | DELETE /guilds/{guild_id}/roles/{role_id} | delete_guild_role M6 | 写 | native |
| A069 | PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id} | create_guild_role_member M6 | 写 | native |
| A070 | DELETE /guilds/{guild_id}/members/{user_id}/roles/{role_id} | delete_guild_role_member M6 | 写 | native |
| A071 | GET /channels/{channel_id}/members/{user_id}/permissions | get_channel_user_permissions M4 | 读 | native |
| A072 | PUT /channels/{channel_id}/members/{user_id}/permissions | update_channel_user_permissions M6 | 写 | native |
| A073 | GET /channels/{channel_id}/roles/{role_id}/permissions | get_channel_role_permissions M4 | 读 | native |
| A074 | PUT /channels/{channel_id}/roles/{role_id}/permissions | update_channel_role_permissions M6 | 写 | native |
| A075 | GET /guilds/{guild_id}/api_permission | get_permissions M4 | 读 | native |
| A076 | POST /guilds/{guild_id}/api_permission/demand | post_permission_demand M6 | 写/申请 | native |
| A077 | POST /guilds/{guild_id}/announces | create_announce, create_recommend_announce M6 | 写 | native |
| A078 | DELETE /guilds/{guild_id}/announces/{message_id} | delete_announce M6 | 写 | native |
| A079 | POST /channels/{channel_id}/announces | 无 M0 | 官方废弃/改用精华消息 | excluded |
| A080 | DELETE /channels/{channel_id}/announces/{message_id} | 无 M0 | 官方废弃/改用精华消息 | excluded |
| A081 | GET /channels/{channel_id}/pins | get_pins M4 | 读 | native |
| A082 | PUT /channels/{channel_id}/pins/{message_id} | put_pin M6 | 写 | native |
| A083 | DELETE /channels/{channel_id}/pins/{message_id} | delete_pin M6 | 写 | native |
| A084 | PUT /channels/{channel_id}/messages/{message_id}/reactions/{type}/{id} | put_reaction M6 | 写 | native |
| A085 | DELETE /channels/{channel_id}/messages/{message_id}/reactions/{type}/{id} | delete_reaction M6 | 写 | native |
| A086 | GET /channels/{channel_id}/messages/{message_id}/reactions/{type}/{id} | get_reaction_users M4 | 读/cookie 页 | native |
| A087 | GET /channels/{channel_id}/schedules | get_schedules M4 | 读/since 过滤 | native |
| A088 | GET /channels/{channel_id}/schedules/{schedule_id} | get_schedule M4 | 读 | native |
| A089 | POST /channels/{channel_id}/schedules | create_schedule M6 | 写 | native |
| A090 | PATCH /channels/{channel_id}/schedules/{schedule_id} | update_schedule M6 | 写 | native |
| A091 | DELETE /channels/{channel_id}/schedules/{schedule_id} | delete_schedule M6 | 写 | native |
| A092 | POST /channels/{channel_id}/audio | update_audio M6 | 写/实时播放 | native |
| A093 | PUT /channels/{channel_id}/mic | on_microphone M6 | 写 | native |
| A094 | DELETE /channels/{channel_id}/mic | off_microphone M6 | 写 | native |
| A095 | GET /channels/{channel_id}/threads | get_threads M4 | 读/仅 is_finish，无续页游标 | native |
| A096 | GET /channels/{channel_id}/threads/{thread_id} | get_thread_detail M4 | 读 | native |
| A097 | PUT /channels/{channel_id}/threads | post_thread M6 | 写 | native |
| A098 | DELETE /channels/{channel_id}/threads/{thread_id} | delete_thread M6 | 写 | native |

来源定位：A001–A055 主要见[官方 autogen 接口目录](https://bot.q.qq.com/wiki/develop/api-v2/autogen/)；其余频道业务见[官方频道业务目录](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/)；A079/A080 见[废弃说明](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/announces/post_channel_announces.html)。`group_openid` 与 `group_id` 的官方占位符在分片端点按各自文档保留，不视为不同端点。运行代码和 CI 不依赖开发阶段的本地镜像。
A011/A013/A060 2026-09-28 对当前 [autogen 接口目录](https://bot.q.qq.com/wiki/develop/api-v2/autogen/)检索无专页；对应猜测链接均落到通用“启动接入”页，并非 API 契约或废弃证据。三项按目标 `botpy==1.2.1` 的实际方法、路径和可核参数兼容请求，实际账号权限未验；不称“现行专页已证实”。

## 64 个目标 SDK 方法

下列 64 项逐名与安装包 `botpy/api.py` 的 `BotAPI` 异步公开方法核对：M4/M5/M6 均有具名方法，旧 SDK 未收录的新 QQ V2 方法另见上表；原生实现位于 `v2/sdk/api/{reads,messages,media,group_admin,guild_admin,menus}.py`。`.api` 仅对无 OneBot 同名冲突的方法转发。

| 目标阶段 | 方法（逐项状态以对应行及源码为准） |
| --- | --- |
| M4 native | get_guild, get_guild_roles, get_guild_member, get_guild_members, get_guild_role_members, get_voice_members, get_channel, get_channels, get_channel_user_permissions, get_channel_role_permissions, get_message, me, me_guilds, get_ws_url, get_permissions, get_schedules, get_schedule, get_reaction_users, get_pins, get_threads, get_thread_detail |
| M5 native | post_message, recall_message, post_keyboard_message, on_interaction_result, patch_guild_message, create_dms, post_dms, post_group_message, post_c2c_message, post_group_file, post_c2c_file；另有新增 recall_group_message/recall_c2c_message/recall_dms/post_c2c_stream_message/上传准备与分片方法 |
| M6 native | create_guild_role, update_guild_role, delete_guild_role, create_guild_role_member, delete_guild_role_member, get_delete_member, create_channel, update_channel, delete_channel, update_channel_user_permissions, update_channel_role_permissions, update_audio, on_microphone, off_microphone, mute_all, cancel_mute_all, mute_member, mute_multi_member, cancel_mute_multi_member, create_announce, create_recommend_announce, delete_announce, post_permission_demand, create_schedule, update_schedule, delete_schedule, put_reaction, delete_reaction, put_pin, delete_pin, post_thread, delete_thread |

旧 SDK 的群 `msg_seq=1`、C2C 注解 `str=1` 已按现行序号协调改为 `int|None=None`；`patch_guild_message.keyboard` 的旧可变字典默认值改为 `None`。可选 `None` 不写线传字段，显式 `""`、`False`、`0` 按官方字段类型保留；只有文档接受的 JSON 结构可传，QQ 最终响应决定权限。单页接口只请求一次；成员完整聚合、游标迭代受 200 页、5000 项、4 MiB、120 秒约束，帖子无续页游标时不声称完整。

## 56 个业务事件与连接通知

`v2/sdk/catalog.py` 注册 56 个业务事件及 READY/RESUMED；`v2/sdk/event_types.py` 为逐名官方形状、字段类型和来源登记的自有 TypedDict／判别类型。`NativeEvent.typed` 在可证实结构有效时返回隔离副本，`schema_missing/schema_invalid` 标记不足，不丢失 `raw()` 额外字段；3 个删除事件当前专页负载未确认，按 SDK 1.2.1 名称兼容，不捏造必填字段。无订阅不分配原生视图，未知事件只给通配符原始诊断；注册不代表 QQ 账号已获 Intent。6 类聊天继续宿主投递，其他事件不伪装聊天或无依据的 OneBot 通知；每事件独立 fixture 见 `tests/test_sdk_event_shapes.py`。

| 族 / bit | 名称 |
| --- | --- |
| GUILDS / 0 | GUILD_CREATE, GUILD_UPDATE, GUILD_DELETE, CHANNEL_CREATE, CHANNEL_UPDATE, CHANNEL_DELETE |
| GUILD_MEMBERS / 1 | GUILD_MEMBER_ADD, GUILD_MEMBER_UPDATE, GUILD_MEMBER_REMOVE |
| GUILD_MESSAGES / 9 | MESSAGE_CREATE, MESSAGE_DELETE |
| GUILD_MESSAGE_REACTIONS / 10 | MESSAGE_REACTION_ADD, MESSAGE_REACTION_REMOVE |
| DIRECT_MESSAGE / 12 | DIRECT_MESSAGE_CREATE, DIRECT_MESSAGE_DELETE |
| OPEN_FORUM_EVENT / 18 | OPEN_FORUM_THREAD_CREATE, OPEN_FORUM_THREAD_UPDATE, OPEN_FORUM_THREAD_DELETE, OPEN_FORUM_POST_CREATE, OPEN_FORUM_POST_DELETE, OPEN_FORUM_REPLY_CREATE, OPEN_FORUM_REPLY_DELETE |
| AUDIO_OR_LIVE_CHANNEL_MEMBER / 19 | AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER, AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT |
| GROUP_MEMBER_EVENT / 24 | GROUP_MEMBER_ADD, GROUP_MEMBER_REMOVE, GROUP_JOIN_REQUEST |
| GROUP_AND_C2C_EVENT / 25 | C2C_MESSAGE_CREATE, FRIEND_ADD, FRIEND_DEL, C2C_MSG_REJECT, C2C_MSG_RECEIVE, GROUP_AT_MESSAGE_CREATE, GROUP_MESSAGE_CREATE, GROUP_ADD_ROBOT, GROUP_DEL_ROBOT, GROUP_MSG_REJECT, GROUP_MSG_RECEIVE, SUBSCRIBE_MESSAGE_STATUS |
| INTERACTION / 26 | INTERACTION_CREATE |
| MESSAGE_AUDIT / 27 | MESSAGE_AUDIT_PASS, MESSAGE_AUDIT_REJECT |
| FORUMS_EVENT / 28 | FORUM_THREAD_CREATE, FORUM_THREAD_UPDATE, FORUM_THREAD_DELETE, FORUM_POST_CREATE, FORUM_POST_DELETE, FORUM_REPLY_CREATE, FORUM_REPLY_DELETE, FORUM_PUBLISH_AUDIT_RESULT |
| AUDIO_ACTION / 29 | AUDIO_START, AUDIO_FINISH, AUDIO_ON_MIC, AUDIO_OFF_MIC |
| PUBLIC_GUILD_MESSAGES / 30 | AT_MESSAGE_CREATE, PUBLIC_MESSAGE_DELETE |
| 连接通知 | READY, RESUMED（`d` 可为 `""`） |

位 18/19 的业务名见[论坛事件](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/forum/open_forum.html)与[音视频成员事件](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/role/audio_or_live_channel_member.html)；这两页**没有确认位数**，18/19 来自目标 SDK `flags.py`，保持显式自定义掩码且不默认启用。群成员位 24 来自[加入](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_add.html)/退出专页，默认掩码也不新增该位。

## 别名、类型与调用边界

| 入口 | 决议 |
| --- | --- |
| `client.qq.get_group_info/get_group_member_info/get_group_member_list` | 原生 dict、单页含 `next_cursor`；同名 OneBot 方法仍在 `client.api`，不能按参数名猜想自动分派。 |
| `client.qq.iter_group_members` | 原生逐页迭代，迭代中途可能失败，不冒称当前原子名单。 |
| `client.api.get_guild` 等无冲突原生名 | 进程内转发；OneBot 同名异义保留 `.api` 旧投影，不自动列入网络动作表。 |
| `client.qq.with_options(operation_id=..., owner=...)` | 局部调用视图，不改变共享连接；已有便捷发送可继承操作 ID，旧显式 ID 不一致时报冲突。 |
| 原生显式来源/目标 | 调用者给出消息/事件来源由 QQ 裁定；既有便捷发送仍要求观察和来源，不跨 AppID 借用。 |
| 回退 | `messaging.sqlite3` v4、其 `extension_schema` v4（v3→v4 增加工具操作归属）、`transport.sqlite3` v4、`profiles.sqlite3` v1；回退应停机恢复匹配代码版本的配置和全套数据库备份，不能删 unknown/部分成功重发。 |
