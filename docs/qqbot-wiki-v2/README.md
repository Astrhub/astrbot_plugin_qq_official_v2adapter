# QQ 机器人官方 API V2 文档镜像

来源：[QQ 机器人官方 API V2](https://bot.q.qq.com/wiki/develop/api-v2/) · 抓取 2026-10-25 · 官方站点 v1.42.0 · 192 页。

本目录为文本镜像，便于在 GitHub 中直接查阅或提供给 Agent。每页首行保留官方 URL 和抓取日期；抓取日期不等于官方更新时间，协议以当前官方页面为准。

## 快速入口

- [官方变更记录](develop/api-v2/changelog.md)
- [素材管理与 qqbot-media://](develop/api-v2/server-inter/message/material-management.md)
- [启动接入](develop/api-v2/index.md)
- [API 调用指南](develop/api-v2/dev-prepare/api-call-guide.md)
- [事件订阅与通知](develop/api-v2/dev-prepare/interface-framework/event-emit.md)
- [WebSocket 接收](develop/api-v2/dev-prepare/event-emit/websocket.md)
- [Webhook 接收](develop/api-v2/dev-prepare/event-emit/webhook.md)
- [覆盖与差异审计](MIRROR_AUDIT.md)
- [逐页响应、更新时间与 SHA-256](MANIFEST.json)

## 全部页面

以下按官方路由分组；目录别名已归一化，不重复收录。

### develop/api-v2

- [变更记录](develop/api-v2/changelog.md)
- [启动接入](develop/api-v2/index.md)

### develop/api-v2/autogen

- [接口文档索引](develop/api-v2/autogen/index.md)

### develop/api-v2/autogen/api

- [删除子频道](develop/api-v2/autogen/api/channels_channel_id.delete.md)
- [获取子频道详情](develop/api-v2/autogen/api/channels_channel_id.get.md)
- [修改子频道](develop/api-v2/autogen/api/channels_channel_id.patch.md)
- [获取通用 WSS 接入点](develop/api-v2/autogen/api/gateway.get.md)
- [获取频道详情](develop/api-v2/autogen/api/guilds_guild_id.get.md)
- [获取子频道列表](develop/api-v2/autogen/api/guilds_guild_id_channels.get.md)
- [创建子频道](develop/api-v2/autogen/api/guilds_guild_id_channels.post.md)
- [互动事件响应](develop/api-v2/autogen/api/interactions_interaction_id.put.md)
- [获取机器人详情](develop/api-v2/autogen/api/users_me.get.md)
- [获取机器人频道列表](develop/api-v2/autogen/api/users_me_guilds.get.md)
- [生成分享链接](develop/api-v2/autogen/api/v2_generate_url_link.post.md)
- [群聊分片上传完成](develop/api-v2/autogen/api/v2_groups_group_id_upload_part_finish.post.md)
- [群聊富媒体预上传](develop/api-v2/autogen/api/v2_groups_group_id_upload_prepare.post.md)
- [入群申请审批](develop/api-v2/autogen/api/v2_groups_group_openid_approval_join_request_member_openid.post.md)
- [群成员批量移除](develop/api-v2/autogen/api/v2_groups_group_openid_batch_remove_members.post.md)
- [获取机器人群内状态](develop/api-v2/autogen/api/v2_groups_group_openid_bot_state.get.md)
- [群聊富媒体上传](develop/api-v2/autogen/api/v2_groups_group_openid_files.post.md)
- [获取群基本信息](develop/api-v2/autogen/api/v2_groups_group_openid_info.get.md)
- [入群申请列表拉取](develop/api-v2/autogen/api/v2_groups_group_openid_join_request_list.get.md)
- [群黑名单查询](develop/api-v2/autogen/api/v2_groups_group_openid_member_blacklist.get.md)
- [群黑名单操作](develop/api-v2/autogen/api/v2_groups_group_openid_member_blacklist.post.md)
- [获取群成员列表](develop/api-v2/autogen/api/v2_groups_group_openid_members.get.md)
- [获取群成员信息](develop/api-v2/autogen/api/v2_groups_group_openid_members_member_openid.get.md)
- [发送群聊消息](develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.md)
- [撤回群聊消息](develop/api-v2/autogen/api/v2_groups_group_openid_messages_message_id.delete.md)
- [查询群禁言状态](develop/api-v2/autogen/api/v2_groups_group_openid_restrict_chat_setting.get.md)
- [设置群成员禁言](develop/api-v2/autogen/api/v2_groups_group_openid_restrict_chat_setting.post.md)
- [查询入群自动审批策略列表](develop/api-v2/autogen/api/v2_groups_join_approval_strategy.get.md)
- [创建入群自动审批策略](develop/api-v2/autogen/api/v2_groups_join_approval_strategy.post.md)
- [删除入群自动审批策略](develop/api-v2/autogen/api/v2_groups_join_approval_strategy_strategy_id.delete.md)
- [修改入群自动审批策略](develop/api-v2/autogen/api/v2_groups_join_approval_strategy_strategy_id.patch.md)
- [执行入群自动审批策略](develop/api-v2/autogen/api/v2_groups_join_approval_strategy_strategy_id_execute.post.md)
- [修改入群自动审批策略的白名单号码](develop/api-v2/autogen/api/v2_groups_join_approval_strategy_strategy_id_whitelist_users.post.md)
- [查询全局自定义菜单](develop/api-v2/autogen/api/v2_menu.get.md)
- [修改全局自定义菜单](develop/api-v2/autogen/api/v2_menu.put.md)
- [查询指令面板列表](develop/api-v2/autogen/api/v2_panels.get.md)
- [创建指令面板](develop/api-v2/autogen/api/v2_panels.post.md)
- [删除指令面板](develop/api-v2/autogen/api/v2_panels_panel_id.delete.md)
- [查询指令面板详情](develop/api-v2/autogen/api/v2_panels_panel_id.get.md)
- [修改指令面板](develop/api-v2/autogen/api/v2_panels_panel_id.put.md)
- [修改指令面板关联对象](develop/api-v2/autogen/api/v2_panels_panel_id_target.put.md)
- [单聊分片上传完成](develop/api-v2/autogen/api/v2_users_user_id_upload_part_finish.post.md)
- [单聊富媒体预上传](develop/api-v2/autogen/api/v2_users_user_id_upload_prepare.post.md)
- [单聊富媒体上传](develop/api-v2/autogen/api/v2_users_user_openid_files.post.md)
- [发送单聊消息](develop/api-v2/autogen/api/v2_users_user_openid_messages.post.md)
- [撤回单聊消息](develop/api-v2/autogen/api/v2_users_user_openid_messages_message_id.delete.md)
- [流式发送单聊消息](develop/api-v2/autogen/api/v2_users_user_openid_stream_messages.post.md)

### develop/api-v2/autogen/event

- [单聊消息事件](develop/api-v2/autogen/event/c2c_message_create.md)
- [单聊消息接收开启](develop/api-v2/autogen/event/c2c_msg_receive.md)
- [单聊消息接收关闭](develop/api-v2/autogen/event/c2c_msg_reject.md)
- [子频道创建](develop/api-v2/autogen/event/channel_create.md)
- [子频道删除](develop/api-v2/autogen/event/channel_delete.md)
- [子频道更新](develop/api-v2/autogen/event/channel_update.md)
- [用户添加好友](develop/api-v2/autogen/event/friend_add.md)
- [用户删除好友](develop/api-v2/autogen/event/friend_del.md)
- [机器人加入群聊](develop/api-v2/autogen/event/group_add_robot.md)
- [群@机器人消息](develop/api-v2/autogen/event/group_at_message_create.md)
- [机器人退出群聊](develop/api-v2/autogen/event/group_del_robot.md)
- [用户申请加群事件](develop/api-v2/autogen/event/group_join_request.md)
- [群成员加入](develop/api-v2/autogen/event/group_member_add.md)
- [群成员退出](develop/api-v2/autogen/event/group_member_remove.md)
- [群消息（全量模式）](develop/api-v2/autogen/event/group_message_create.md)
- [群聊消息接收开启](develop/api-v2/autogen/event/group_msg_receive.md)
- [群聊消息接收关闭](develop/api-v2/autogen/event/group_msg_reject.md)
- [频道创建](develop/api-v2/autogen/event/guild_create.md)
- [频道解散](develop/api-v2/autogen/event/guild_delete.md)
- [频道更新](develop/api-v2/autogen/event/guild_update.md)
- [互动事件](develop/api-v2/autogen/event/interaction_create.md)
- [订阅消息授权状态变更](develop/api-v2/autogen/event/subscribe_message_status.md)

### develop/api-v2/dev-prepare

- [获取访问凭证](develop/api-v2/dev-prepare/access-token.md)
- [API 调用指南](develop/api-v2/dev-prepare/api-call-guide.md)
- [启动接入](develop/api-v2/dev-prepare/getting-started.md)

### develop/api-v2/dev-prepare/error-trace

- [错误与调试](develop/api-v2/dev-prepare/error-trace/index.md)
- [WebSocket](develop/api-v2/dev-prepare/error-trace/websocket.md)

### develop/api-v2/dev-prepare/event-emit

- [通用数据结构](develop/api-v2/dev-prepare/event-emit/payload.md)
- [Webhook 方式](develop/api-v2/dev-prepare/event-emit/webhook.md)
- [WebSocket 方式](develop/api-v2/dev-prepare/event-emit/websocket.md)

### develop/api-v2/dev-prepare/interface-framework

- [接口调用与鉴权](develop/api-v2/dev-prepare/interface-framework/api-use.md)
- [事件订阅与通知](develop/api-v2/dev-prepare/interface-framework/event-emit.md)
- [接口通信基础框架](develop/api-v2/dev-prepare/interface-framework/index.md)
- [opcode](develop/api-v2/dev-prepare/interface-framework/opcode.md)
- [使用 Websocket 接入](develop/api-v2/dev-prepare/interface-framework/reference.md)
- [安全和授权](develop/api-v2/dev-prepare/interface-framework/sign.md)

### develop/api-v2/gateway


### develop/api-v2/gateway/error

- [websocket 错误](develop/api-v2/gateway/error/error.md)

### develop/api-v2/gateway/error/data

- [业务报错返回的数据信息(Data)](develop/api-v2/gateway/error/data/model.md)

### develop/api-v2/openapi


### develop/api-v2/openapi/emoji

- [表情对象](develop/api-v2/openapi/emoji/model.md)

### develop/api-v2/openapi/error

- [错误与调试](develop/api-v2/openapi/error/error.md)

### develop/api-v2/openapi/error/data

- [业务报错返回的数据信息(Data)](develop/api-v2/openapi/error/data/model.md)

### develop/api-v2/openapi/member

- [删除频道成员](develop/api-v2/openapi/member/delete_member.md)
- [获取成员详情](develop/api-v2/openapi/member/get_member.md)
- [获取频道成员列表](develop/api-v2/openapi/member/get_members.md)
- [成员对象(Member)](develop/api-v2/openapi/member/model.md)

### develop/api-v2/openapi/reaction

- [表情表态对象](develop/api-v2/openapi/reaction/model.md)

### develop/api-v2/openapi/user

- [获取用户频道列表](develop/api-v2/openapi/user/guilds.md)
- [获取用户详情](develop/api-v2/openapi/user/me.md)
- [用户对象(User)](develop/api-v2/openapi/user/model.md)

### develop/api-v2/openapi/wss

- [获取带分片 WSS 接入点](develop/api-v2/openapi/wss/shard_url_get.md)
- [获取通用 WSS 接入点](develop/api-v2/openapi/wss/url_get.md)

### develop/api-v2/server-inter


### develop/api-v2/server-inter/channel

- [频道模块](develop/api-v2/server-inter/channel/index.md)

### develop/api-v2/server-inter/channel/api_permissions

- [获取机器人在频道可用权限列表](develop/api-v2/server-inter/channel/api_permissions/get_guild_api_permission.md)
- [频道模块](develop/api-v2/server-inter/channel/api_permissions/index.md)
- [接口权限对象](develop/api-v2/server-inter/channel/api_permissions/model.md)
- [发送机器人在频道接口权限的授权链接](develop/api-v2/server-inter/channel/api_permissions/post_api_permission_demand.md)

### develop/api-v2/server-inter/channel/content

- [频道模块](develop/api-v2/server-inter/channel/content/index.md)

### develop/api-v2/server-inter/channel/content/announces

- [删除子频道公告（2022年3月15日后废弃）](develop/api-v2/server-inter/channel/content/announces/delete_channel_announces.md)
- [删除频道公告](develop/api-v2/server-inter/channel/content/announces/delete_guild_announces.md)
- [公告对象(Announces)](develop/api-v2/server-inter/channel/content/announces/model.md)
- [创建子频道公告（2022年3月15日后废弃）](develop/api-v2/server-inter/channel/content/announces/post_channel_announces.md)
- [创建频道公告](develop/api-v2/server-inter/channel/content/announces/post_guild_announces.md)

### develop/api-v2/server-inter/channel/content/audio

- [音频控制](develop/api-v2/server-inter/channel/content/audio/audio_control.md)
- [机器人下麦](develop/api-v2/server-inter/channel/content/audio/delete_mic.md)
- [语音对象](develop/api-v2/server-inter/channel/content/audio/model.md)
- [机器人上麦](develop/api-v2/server-inter/channel/content/audio/put_mic.md)

### develop/api-v2/server-inter/channel/content/forum

- [删除帖子](develop/api-v2/server-inter/channel/content/forum/delete_thread.md)
- [论坛事件对象(ForumEvent)](develop/api-v2/server-inter/channel/content/forum/forum.md)
- [获取帖子详情](develop/api-v2/server-inter/channel/content/forum/get_thread.md)
- [获取帖子列表](develop/api-v2/server-inter/channel/content/forum/get_threads_list.md)
- [论坛对象(forum)](develop/api-v2/server-inter/channel/content/forum/model.md)
- [开放论坛事件对象(OpenForumEvent)](develop/api-v2/server-inter/channel/content/forum/open_forum.md)
- [发表帖子](develop/api-v2/server-inter/channel/content/forum/put_thread.md)

### develop/api-v2/server-inter/channel/content/pins

- [删除精华消息](develop/api-v2/server-inter/channel/content/pins/delete_pins_message.md)
- [获取精华消息](develop/api-v2/server-inter/channel/content/pins/get_pins_message.md)
- [精华消息对象(PinsMessage)](develop/api-v2/server-inter/channel/content/pins/model.md)
- [添加精华消息](develop/api-v2/server-inter/channel/content/pins/put_pins_message.md)

### develop/api-v2/server-inter/channel/content/schedule

- [删除日程](develop/api-v2/server-inter/channel/content/schedule/delete_schedule.md)
- [获取日程详情](develop/api-v2/server-inter/channel/content/schedule/get_schedule.md)
- [获取频道日程列表](develop/api-v2/server-inter/channel/content/schedule/get_schedules.md)
- [日程对象(Schedule)](develop/api-v2/server-inter/channel/content/schedule/model.md)
- [修改日程](develop/api-v2/server-inter/channel/content/schedule/patch_schedule.md)
- [创建日程](develop/api-v2/server-inter/channel/content/schedule/post_schedule.md)

### develop/api-v2/server-inter/channel/manage

- [频道模块](develop/api-v2/server-inter/channel/manage/index.md)

### develop/api-v2/server-inter/channel/manage/channel

- [子频道对象(Channel)](develop/api-v2/server-inter/channel/manage/channel/model.md)

### develop/api-v2/server-inter/channel/manage/guild

- [频道对象(Guild)](develop/api-v2/server-inter/channel/manage/guild/model.md)

### develop/api-v2/server-inter/channel/manage/user

- [用户对象(User)](develop/api-v2/server-inter/channel/manage/user/model.md)

### develop/api-v2/server-inter/channel/message

- [频道私信](develop/api-v2/server-inter/channel/message/dms.md)
- [频道消息事件](develop/api-v2/server-inter/channel/message/event.md)
- [内嵌格式](develop/api-v2/server-inter/channel/message/format.md)
- [撤回子频道消息](develop/api-v2/server-inter/channel/message/recall.md)
- [发送子频道消息](develop/api-v2/server-inter/channel/message/send.md)

### develop/api-v2/server-inter/channel/miniapp

- [频道模块](develop/api-v2/server-inter/channel/miniapp/index.md)
- [获取频道和当前人信息](develop/api-v2/server-inter/channel/miniapp/interface.md)
- [开放数据域加密](develop/api-v2/server-inter/channel/miniapp/opendata.md)

### develop/api-v2/server-inter/channel/role

- [音视频/直播子频道成员进出事件](develop/api-v2/server-inter/channel/role/audio_or_live_channel_member.md)
- [获取子频道在线成员数](develop/api-v2/server-inter/channel/role/get_online_nums.md)
- [频道成员事件](develop/api-v2/server-inter/channel/role/guild_member.md)
- [频道模块](develop/api-v2/server-inter/channel/role/index.md)

### develop/api-v2/server-inter/channel/role/member

- [删除频道成员](develop/api-v2/server-inter/channel/role/member/delete_member.md)
- [获取频道成员详情](develop/api-v2/server-inter/channel/role/member/get_member.md)
- [获取频道成员列表](develop/api-v2/server-inter/channel/role/member/get_members.md)
- [获取频道身份组成员列表](develop/api-v2/server-inter/channel/role/member/get_role_members.md)
- [成员对象(Member)](develop/api-v2/server-inter/channel/role/member/model.md)
- [频道身份组对象(Role)](develop/api-v2/server-inter/channel/role/member/role_model.md)

### develop/api-v2/server-inter/channel/role-group

- [删除频道身份组成员](develop/api-v2/server-inter/channel/role-group/delete_guild_member_role.md)
- [删除频道身份组](develop/api-v2/server-inter/channel/role-group/delete_guild_role.md)
- [获取频道身份组列表](develop/api-v2/server-inter/channel/role-group/get_guild_roles.md)
- [频道模块](develop/api-v2/server-inter/channel/role-group/index.md)
- [修改频道身份组](develop/api-v2/server-inter/channel/role-group/patch_guild_role.md)
- [创建频道身份组](develop/api-v2/server-inter/channel/role-group/post_guild_role.md)
- [创建频道身份组成员](develop/api-v2/server-inter/channel/role-group/put_guild_member_role.md)

### develop/api-v2/server-inter/channel/role-group/channel_permissions

- [获取子频道用户权限](develop/api-v2/server-inter/channel/role-group/channel_permissions/get_channel_permissions.md)
- [获取子频道身份组权限](develop/api-v2/server-inter/channel/role-group/channel_permissions/get_channel_roles_permissions.md)
- [子频道权限对象(ChannelPermissions)](develop/api-v2/server-inter/channel/role-group/channel_permissions/model.md)
- [修改子频道用户权限](develop/api-v2/server-inter/channel/role-group/channel_permissions/put_channel_permissions.md)
- [修改子频道身份组权限](develop/api-v2/server-inter/channel/role-group/channel_permissions/put_channel_roles_permissions.md)

### develop/api-v2/server-inter/channel/speak

- [频道模块](develop/api-v2/server-inter/channel/speak/index.md)
- [频道指定成员禁言](develop/api-v2/server-inter/channel/speak/patch_guild_member_mute.md)
- [频道全员禁言](develop/api-v2/server-inter/channel/speak/patch_guild_mute.md)
- [频道批量成员禁言](develop/api-v2/server-inter/channel/speak/patch_guild_mute_multi_member.md)

### develop/api-v2/server-inter/channel/speak/setting

- [获取频道消息频率的设置详情](develop/api-v2/server-inter/channel/speak/setting/message_setting.md)
- [频道消息频率设置对象 (MessageSetting)](develop/api-v2/server-inter/channel/speak/setting/model.md)

### develop/api-v2/server-inter/group

- [群组模块](develop/api-v2/server-inter/group/index.md)

### develop/api-v2/server-inter/group/manage

- [群管理](develop/api-v2/server-inter/group/manage/index.md)

### develop/api-v2/server-inter/menu-panel

- [自定义菜单与指令面板](develop/api-v2/server-inter/menu-panel/index.md)

### develop/api-v2/server-inter/message

- [消息相关](develop/api-v2/server-inter/message/index.md)
- [素材管理](develop/api-v2/server-inter/message/material-management.md)
- [消息收发概述](develop/api-v2/server-inter/message/overview.md)
- [富媒体消息概述](develop/api-v2/server-inter/message/rich-media.md)

### develop/api-v2/server-inter/message/template

- [消息对象(Message)](develop/api-v2/server-inter/message/template/model.md)

### develop/api-v2/server-inter/message/trans

- [表情表态](develop/api-v2/server-inter/message/trans/emoji.md)
- [消息按钮](develop/api-v2/server-inter/message/trans/msg-btn.md)
- [消息交互概述](develop/api-v2/server-inter/message/trans/overview.md)
- [文本交互](develop/api-v2/server-inter/message/trans/text-chain.md)

### develop/api-v2/server-inter/message/type

- [结构化卡片消息](develop/api-v2/server-inter/message/type/ark.md)
- [Embed 消息](develop/api-v2/server-inter/message/type/embed.md)
- [Markdown 消息](develop/api-v2/server-inter/message/type/markdown.md)
- [消息类型](develop/api-v2/server-inter/message/type/overview.md)

### develop/api-v2/server-inter/user

- [用户模块](develop/api-v2/server-inter/user/index.md)

### develop/api-v2/server-inter/user/manage

- [事件](develop/api-v2/server-inter/user/manage/event.md)
- [用户管理](develop/api-v2/server-inter/user/manage/index.md)
