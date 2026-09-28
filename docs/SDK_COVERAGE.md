# QQ V2 SDK 覆盖台账（W001）

口径：QQ 官方 v2 [目录](https://bot.q.qq.com/wiki/develop/api-v2/)（离线核对了 2026-09-19 本地镜像）、AstrBot 4.28.1 所用 `qq-botpy==1.2.1` 的 `BotAPI` 公开签名（64 个）。2026-09-28 在线复核了 Payload、成员事件与成员列表；镜像抓取日期不是页面更新时间。路径中的 `{...}` 为独立 URL 路径段，调用前编码。**本台账的 pending 是未实现，不代表 QQ 拒绝或账号无权限。**

状态：`native` 为已提供完整原生单次请求；`internal` 为既有便捷/OneBot 核心可用、原生签名或返回待核；`pending` 为未提供具名实现；`excluded` 为官方废弃。已实现的单次请求由 `tests/test_sdk_profiles.py` 独立断言 HTTPS 域名、路径及分页；其余接口的独立请求样例/字段校验须在 M4–M6 落地，不能用本台账冒充验收。所有原生 GET 真正访问 QQ，不用缓存替代；缓存走 `.qq.profiles`。所有写操作最终须经现有账本、权限开关、实例和代次校验。

| ID | 官方方法及路径 | 原生方法/归属 | 类别/分页 | W001 状态 |
| --- | --- | --- | --- | --- |
| A001 | GET /gateway | get_gateway M4 | 读 | pending |
| A002 | GET /gateway/bot | get_ws_url, get_gateway_bot M4 | 读 | internal |
| A003 | GET /users/@me | me M4 | 读 | internal |
| A004 | GET /users/@me/guilds | me_guilds M4 | 读/页 | pending |
| A005 | POST /v2/generate_url_link | generate_url_link M6 | 写/链接副作用 | internal |
| A006 | POST /v2/groups/{group_openid}/messages | post_group_message M5 | 写/消息/序号 | internal |
| A007 | DELETE /v2/groups/{group_openid}/messages/{message_id} | recall_group_message M5 | 写/撤回 | internal |
| A008 | POST /v2/users/{user_openid}/messages | post_c2c_message M5 | 写/消息/序号 | internal |
| A009 | DELETE /v2/users/{user_openid}/messages/{message_id} | recall_c2c_message M5 | 写/撤回 | internal |
| A010 | POST /v2/users/{user_openid}/stream_messages | post_c2c_stream_message M5 | 写/流式部分成功 | internal |
| A011 | GET /channels/{channel_id}/messages/{message_id} | get_message M4 | 读 | pending |
| A012 | POST /channels/{channel_id}/messages | post_message, post_keyboard_message M5 | 写/消息 | internal |
| A013 | PATCH /channels/{channel_id}/messages/{message_id} | patch_guild_message M5 | 写/修改 | pending |
| A014 | DELETE /channels/{channel_id}/messages/{message_id} | recall_message M5 | 写/撤回 | internal |
| A015 | POST /users/@me/dms | create_dms M5 | 写/会话创建 | pending |
| A016 | POST /dms/{guild_id}/messages | post_dms M5 | 写/消息 | internal |
| A017 | DELETE /dms/{guild_id}/messages/{message_id} | recall_dms M5 | 写/撤回 | internal |
| A018 | POST /v2/groups/{group_openid}/files | post_group_file M5 | 写/上传/可选发送 | internal |
| A019 | POST /v2/users/{user_openid}/files | post_c2c_file M5 | 写/上传/可选发送 | internal |
| A020 | POST /v2/groups/{group_id}/upload_prepare | post_group_upload_prepare M5 | 写/上传准备 | internal |
| A021 | POST /v2/groups/{group_id}/upload_part_finish | post_group_upload_part_finish M5 | 写/分片完成 | internal |
| A022 | POST /v2/users/{user_id}/upload_prepare | post_c2c_upload_prepare M5 | 写/上传准备 | internal |
| A023 | POST /v2/users/{user_id}/upload_part_finish | post_c2c_upload_part_finish M5 | 写/分片完成 | internal |
| A024 | GET /v2/groups/{group_openid}/info | get_group_info M4 | 读/OneBot 同名异义 | native |
| A025 | GET /v2/groups/{group_openid}/bot_state | get_group_bot_state M4 | 读 | internal |
| A026 | GET /v2/groups/{group_openid}/members/{member_openid} | get_group_member_info M3 | 读/OneBot 同名异义 | native |
| A027 | GET /v2/groups/{group_openid}/members | get_group_member_list M3 | 读/官方 cursor 单页 | native |
| A028 | GET /v2/groups/{group_openid}/restrict_chat_setting | get_group_restrict_chat_setting M4 | 读 | internal |
| A029 | POST /v2/groups/{group_openid}/restrict_chat_setting | set_group_restrict_chat_setting M6 | 写/批量≤20 | internal |
| A030 | POST /v2/groups/{group_openid}/batch_remove_members | batch_remove_group_members M6 | 写/批量≤20/可部分成功 | internal |
| A031 | GET /v2/groups/{group_openid}/member_blacklist | get_group_member_blacklist M4 | 读/游标≤100 | pending |
| A032 | POST /v2/groups/{group_openid}/member_blacklist | set_group_member_blacklist M6 | 写/批量≤20 | pending |
| A033 | GET /v2/groups/{group_openid}/join_request_list | get_group_join_requests M4 | 读/游标≤50 | internal |
| A034 | POST /v2/groups/{group_openid}/approval_join_request/{member_openid} | approve_group_join_request M6 | 写/申请/不可猜 flag | internal |
| A035 | GET /v2/groups/join_approval_strategy | get_join_approval_strategies M4 | 读/机器人级游标≤50 | pending |
| A036 | POST /v2/groups/join_approval_strategy | create_join_approval_strategy M6 | 写/机器人级 | pending |
| A037 | PATCH /v2/groups/join_approval_strategy/{strategy_id} | update_join_approval_strategy M6 | 写/机器人级 | pending |
| A038 | DELETE /v2/groups/join_approval_strategy/{strategy_id} | delete_join_approval_strategy M6 | 写/机器人级 | pending |
| A039 | POST /v2/groups/join_approval_strategy/{strategy_id}/execute | execute_join_approval_strategy M6 | 写/机器人级 | pending |
| A040 | POST /v2/groups/join_approval_strategy/{strategy_id}/whitelist_users | set_join_approval_whitelist M6 | 写/QQ 号码≤10000/不由 OpenID 推导 | pending |
| A041 | GET /v2/menu | get_menu M4 | 读 | pending |
| A042 | PUT /v2/menu | put_menu M6 | 写/全局版本 | pending |
| A043 | GET /v2/panels | get_panels M4 | 读 | internal |
| A044 | POST /v2/panels | create_panel M6 | 写/托管对象 | internal |
| A045 | GET /v2/panels/{panel_id} | get_panel M4 | 读 | internal |
| A046 | PUT /v2/panels/{panel_id} | update_panel M6 | 写/托管对象 | internal |
| A047 | DELETE /v2/panels/{panel_id} | delete_panel M6 | 写/托管对象 | pending |
| A048 | PUT /v2/panels/{panel_id}/target | set_panel_target M6 | 写/托管对象 | pending |
| A049 | PUT /interactions/{interaction_id} | on_interaction_result M5 | 写/ACK 单次 | internal |
| A050 | GET /guilds/{guild_id} | get_guild M4 | 读 | internal |
| A051 | GET /guilds/{guild_id}/channels | get_channels M4 | 读 | internal |
| A052 | POST /guilds/{guild_id}/channels | create_channel M6 | 写 | internal |
| A053 | GET /channels/{channel_id} | get_channel M4 | 读 | internal |
| A054 | PATCH /channels/{channel_id} | update_channel M6 | 写 | internal |
| A055 | DELETE /channels/{channel_id} | delete_channel M6 | 写 | internal |
| A056 | GET /guilds/{guild_id}/members | get_guild_members M4 | 读/after 单页 | internal |
| A057 | GET /guilds/{guild_id}/members/{user_id} | get_guild_member M4 | 读 | internal |
| A058 | DELETE /guilds/{guild_id}/members/{user_id} | get_delete_member, delete_guild_member M6 | 写/即使名称以 get 开头 | internal |
| A059 | GET /guilds/{guild_id}/roles/{role_id}/members | get_guild_role_members M4 | 读/页 | pending |
| A060 | GET /channels/{channel_id}/voice/members | get_voice_members M4 | 读 | pending |
| A061 | GET /channels/{channel_id}/online_nums | get_channel_online_nums M4 | 读 | pending |
| A062 | PATCH /guilds/{guild_id}/mute | mute_all, cancel_mute_all, mute_multi_member, cancel_mute_multi_member M6 | 写/多目标 | internal |
| A063 | PATCH /guilds/{guild_id}/members/{user_id}/mute | mute_member M6 | 写 | internal |
| A064 | GET /guilds/{guild_id}/message/setting | get_guild_message_setting M4 | 读 | pending |
| A065 | GET /guilds/{guild_id}/roles | get_guild_roles M4 | 读/页 | pending |
| A066 | POST /guilds/{guild_id}/roles | create_guild_role M6 | 写 | pending |
| A067 | PATCH /guilds/{guild_id}/roles/{role_id} | update_guild_role M6 | 写 | pending |
| A068 | DELETE /guilds/{guild_id}/roles/{role_id} | delete_guild_role M6 | 写 | pending |
| A069 | PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id} | create_guild_role_member M6 | 写 | pending |
| A070 | DELETE /guilds/{guild_id}/members/{user_id}/roles/{role_id} | delete_guild_role_member M6 | 写 | pending |
| A071 | GET /channels/{channel_id}/members/{user_id}/permissions | get_channel_user_permissions M4 | 读 | pending |
| A072 | PUT /channels/{channel_id}/members/{user_id}/permissions | update_channel_user_permissions M6 | 写 | pending |
| A073 | GET /channels/{channel_id}/roles/{role_id}/permissions | get_channel_role_permissions M4 | 读 | pending |
| A074 | PUT /channels/{channel_id}/roles/{role_id}/permissions | update_channel_role_permissions M6 | 写 | pending |
| A075 | GET /guilds/{guild_id}/api_permission | get_permissions M4 | 读 | pending |
| A076 | POST /guilds/{guild_id}/api_permission/demand | post_permission_demand M6 | 写/申请 | pending |
| A077 | POST /guilds/{guild_id}/announces | create_announce, create_recommend_announce M6 | 写 | pending |
| A078 | DELETE /guilds/{guild_id}/announces/{message_id} | delete_announce M6 | 写 | pending |
| A079 | POST /channels/{channel_id}/announces | 无 M0 | 官方废弃/改用精华消息 | excluded |
| A080 | DELETE /channels/{channel_id}/announces/{message_id} | 无 M0 | 官方废弃/改用精华消息 | excluded |
| A081 | GET /channels/{channel_id}/pins | get_pins M4 | 读 | pending |
| A082 | PUT /channels/{channel_id}/pins/{message_id} | put_pin M6 | 写 | pending |
| A083 | DELETE /channels/{channel_id}/pins/{message_id} | delete_pin M6 | 写 | pending |
| A084 | PUT /channels/{channel_id}/messages/{message_id}/reactions/{type}/{id} | put_reaction M6 | 写 | pending |
| A085 | DELETE /channels/{channel_id}/messages/{message_id}/reactions/{type}/{id} | delete_reaction M6 | 写 | pending |
| A086 | GET /channels/{channel_id}/messages/{message_id}/reactions/{type}/{id} | get_reaction_users M4 | 读/cookie 页 | pending |
| A087 | GET /channels/{channel_id}/schedules | get_schedules M4 | 读/since 页 | pending |
| A088 | GET /channels/{channel_id}/schedules/{schedule_id} | get_schedule M4 | 读 | pending |
| A089 | POST /channels/{channel_id}/schedules | create_schedule M6 | 写 | pending |
| A090 | PATCH /channels/{channel_id}/schedules/{schedule_id} | update_schedule M6 | 写 | pending |
| A091 | DELETE /channels/{channel_id}/schedules/{schedule_id} | delete_schedule M6 | 写 | pending |
| A092 | POST /channels/{channel_id}/audio | update_audio M6 | 写/实时播放 | pending |
| A093 | PUT /channels/{channel_id}/mic | on_microphone M6 | 写 | pending |
| A094 | DELETE /channels/{channel_id}/mic | off_microphone M6 | 写 | pending |
| A095 | GET /channels/{channel_id}/threads | get_threads M4 | 读/页 | pending |
| A096 | GET /channels/{channel_id}/threads/{thread_id} | get_thread_detail M4 | 读 | pending |
| A097 | PUT /channels/{channel_id}/threads | post_thread M6 | 写 | pending |
| A098 | DELETE /channels/{channel_id}/threads/{thread_id} | delete_thread M6 | 写 | pending |

来源定位：A001–A055 主要见[官方 autogen 接口目录](https://bot.q.qq.com/wiki/develop/api-v2/autogen/)；其余频道业务见[官方频道业务目录](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/)；A079/A080 见[废弃说明](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/announces/post_channel_announces.html)。`group_openid` 与 `group_id` 的官方占位符在分片端点按各自文档保留，不视为不同端点。运行代码和 CI 不依赖开发阶段的本地镜像。
A011/A013/A060 仅由目标旧 SDK 的公开签名补录；2026-09-28 对当前 v2 目录检索仍未定位相应专页（旧版 SDK 文档不等于现行 v2 权限证据）。保持待实现/待权限验证，M4/M5 不以猜测的返回或永久 unsupported 充数。

## 64 个目标 SDK 方法

以下逐名与安装包 `botpy/api.py` 的 `BotAPI` 异步公开方法做集合核对：64/64 一致。表中 64 个目标方法仍为 **pending-native**（内部有部分等价调用时状态见上表）；`get_group_member_info/get_group_member_list/get_group_info` 是新增具名原生方法，不计入该 64。下列方法不能因内部方法或 `__getattr__` 存在而标完成。

| 目标阶段 | 方法（各个均 pending-native） |
| --- | --- |
| M4 | get_guild, get_guild_roles, get_guild_member, get_guild_members, get_guild_role_members, get_voice_members, get_channel, get_channels, get_channel_user_permissions, get_channel_role_permissions, get_message, me, me_guilds, get_ws_url, get_permissions, get_schedules, get_schedule, get_reaction_users, get_pins, get_threads, get_thread_detail |
| M5 | post_message, recall_message, post_keyboard_message, on_interaction_result, patch_guild_message, create_dms, post_dms, post_group_message, post_c2c_message, post_group_file, post_c2c_file |
| M6 | create_guild_role, update_guild_role, delete_guild_role, create_guild_role_member, delete_guild_role_member, get_delete_member, create_channel, update_channel, delete_channel, update_channel_user_permissions, update_channel_role_permissions, update_audio, on_microphone, off_microphone, mute_all, cancel_mute_all, mute_member, mute_multi_member, cancel_mute_multi_member, create_announce, create_recommend_announce, delete_announce, post_permission_demand, create_schedule, update_schedule, delete_schedule, put_reaction, delete_reaction, put_pin, delete_pin, post_thread, delete_thread |

64 方法的精确旧签名在目标包 `BotAPI`，现阶段已新增的三项群 GET 签名见 `v2/client.py`；M4–M6 分项对照可选 null、空字符串、False、0、返回 dict/list/204，以及新字段 `input_notify/file_name/upload_id` 和两种来源 ID。旧 SDK 的群 `msg_seq=1`、C2C 注解 `str=1` 均不沿用；新原生发送将使用 `msg_seq: int|None=None` 并保留显式整数。单页原生成员接口只请求一次；完整聚合的现有便捷接口受 200 页、5000 项、4 MiB、120 秒约束。

## 56 个业务事件与连接通知

`v2/sdk/catalog.py` 逐名注册下列 56 个业务名及 READY/RESUMED。所有事件：原始只读视图和按 `t` 判别的类型化 `NativeEvent` 可订阅，未知 `t` 保留有界原始诊断；**注册不等于 QQ 端已授权 Intents，也不等于 OneBot 投影**。聊天 6 类继续宿主投递；群成员 2 类及频道成员 3 类在 M3 更新资料与成员状态；互动/入群申请先由核心处理。其余事件为 SDK 实时观察和核心处置，不伪装聊天或缺失角色。更多细分类型模型及具体权限仍需 M4–M7 针对官方负载逐事件回归。

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
| `client.api.get_guild` 等无冲突原生名 | 在各方法实际实现后再转发；不能把所有原生名加入网络动作表。 |
| `client.qq.with_options(operation_id=..., owner=...)` | 局部调用视图，不改变共享连接；已有便捷发送可继承操作 ID，旧显式 ID 不一致时报冲突。 |
| 原生显式来源/目标 | 调用者给出消息/事件来源由 QQ 裁定；既有便捷发送仍要求观察和来源，不跨 AppID 借用。 |
| 回退 | 新 schema 的 `messaging.sqlite3` v3、`transport.sqlite3` v4 和 `profiles.sqlite3` v1 只能与对应版本代码一同使用；回退恢复停机一致性全套备份。 |
