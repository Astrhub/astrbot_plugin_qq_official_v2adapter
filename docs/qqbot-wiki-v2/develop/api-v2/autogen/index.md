<!-- 来源: https://bot.q.qq.com/wiki/develop/api-v2/autogen/index.html · 抓取 2026-10-25 -->

# 接口文档索引

## 接口

### 消息/单聊消息

- [POST /v2/users/{user_openid}/messages](api/v2_users_user_openid_messages.post.md) — 发送单聊消息
- [POST /v2/users/{user_openid}/stream_messages](api/v2_users_user_openid_stream_messages.post.md) — 流式发送单聊消息
- [DELETE /v2/users/{user_openid}/messages/{message_id}](api/v2_users_user_openid_messages_message_id.delete.md) — 撤回单聊消息

### 消息/群消息

- [POST /v2/groups/{group_openid}/messages](api/v2_groups_group_openid_messages.post.md) — 发送群聊消息
- [DELETE /v2/groups/{group_openid}/messages/{message_id}](api/v2_groups_group_openid_messages_message_id.delete.md) — 撤回群聊消息

### 消息/富媒体

- [POST /v2/groups/{group_openid}/files](api/v2_groups_group_openid_files.post.md) — 群聊富媒体上传
- [POST /v2/users/{user_openid}/files](api/v2_users_user_openid_files.post.md) — 单聊富媒体上传
- [POST /v2/users/{user_id}/upload_prepare](api/v2_users_user_id_upload_prepare.post.md) — 单聊富媒体预上传
- [POST /v2/users/{user_id}/upload_part_finish](api/v2_users_user_id_upload_part_finish.post.md) — 单聊分片上传完成
- [POST /v2/groups/{group_id}/upload_prepare](api/v2_groups_group_id_upload_prepare.post.md) — 群聊富媒体预上传
- [POST /v2/groups/{group_id}/upload_part_finish](api/v2_groups_group_id_upload_part_finish.post.md) — 群聊分片上传完成

### 互动/自定义菜单

- [GET /v2/menu](api/v2_menu.get.md) — 查询全局自定义菜单
- [PUT /v2/menu](api/v2_menu.put.md) — 修改全局自定义菜单

### 互动/指令面板

- [GET /v2/panels](api/v2_panels.get.md) — 查询指令面板列表
- [POST /v2/panels](api/v2_panels.post.md) — 创建指令面板
- [PUT /v2/panels/{panel_id}/target](api/v2_panels_panel_id_target.put.md) — 修改指令面板关联对象
- [GET /v2/panels/{panel_id}](api/v2_panels_panel_id.get.md) — 查询指令面板详情
- [PUT /v2/panels/{panel_id}](api/v2_panels_panel_id.put.md) — 修改指令面板
- [DELETE /v2/panels/{panel_id}](api/v2_panels_panel_id.delete.md) — 删除指令面板

### 互动/互动回调

- [PUT /interactions/{interaction_id}](api/interactions_interaction_id.put.md) — 互动事件响应

### QQ频道/频道管理

- [GET /channels/{channel_id}](api/channels_channel_id.get.md) — 获取子频道详情
- [PATCH /channels/{channel_id}](api/channels_channel_id.patch.md) — 修改子频道
- [DELETE /channels/{channel_id}](api/channels_channel_id.delete.md) — 删除子频道
- [GET /guilds/{guild_id}](api/guilds_guild_id.get.md) — 获取频道详情
- [GET /guilds/{guild_id}/channels](api/guilds_guild_id_channels.get.md) — 获取子频道列表
- [POST /guilds/{guild_id}/channels](api/guilds_guild_id_channels.post.md) — 创建子频道

### 用户/机器人

- [GET /users/@me](api/users_me.get.md) — 获取机器人详情
- [GET /users/@me/guilds](api/users_me_guilds.get.md) — 获取机器人频道列表

### 用户/网关

- [GET /gateway](api/gateway.get.md) — 获取通用 WSS 接入点

### 用户/机器人管理

- [POST /v2/generate_url_link](api/v2_generate_url_link.post.md) — 生成分享链接

### QQ群/群信息

- [GET /v2/groups/{group_openid}/info](api/v2_groups_group_openid_info.get.md) — 获取群基本信息
- [GET /v2/groups/{group_openid}/bot_state](api/v2_groups_group_openid_bot_state.get.md) — 获取机器人群内状态

### QQ群/群成员

- [GET /v2/groups/{group_openid}/members](api/v2_groups_group_openid_members.get.md) — 获取群成员列表
- [GET /v2/groups/{group_openid}/members/{member_openid}](api/v2_groups_group_openid_members_member_openid.get.md) — 获取群成员信息
- [POST /v2/groups/{group_openid}/batch_remove_members](api/v2_groups_group_openid_batch_remove_members.post.md) — 群成员批量移除

### QQ群/入群申请

- [GET /v2/groups/{group_openid}/join_request_list](api/v2_groups_group_openid_join_request_list.get.md) — 入群申请列表拉取
- [POST /v2/groups/{group_openid}/approval_join_request/{member_openid}](api/v2_groups_group_openid_approval_join_request_member_openid.post.md) — 入群申请审批
- [POST /v2/groups/join_approval_strategy](api/v2_groups_join_approval_strategy.post.md) — 创建入群自动审批策略
- [PATCH /v2/groups/join_approval_strategy/{strategy_id}](api/v2_groups_join_approval_strategy_strategy_id.patch.md) — 修改入群自动审批策略
- [POST /v2/groups/join_approval_strategy/{strategy_id}/execute](api/v2_groups_join_approval_strategy_strategy_id_execute.post.md) — 执行入群自动审批策略
- [POST /v2/groups/join_approval_strategy/{strategy_id}/whitelist_users](api/v2_groups_join_approval_strategy_strategy_id_whitelist_users.post.md) — 修改入群自动审批策略的白名单号码
- [GET /v2/groups/join_approval_strategy](api/v2_groups_join_approval_strategy.get.md) — 查询入群自动审批策略列表
- [DELETE /v2/groups/join_approval_strategy/{strategy_id}](api/v2_groups_join_approval_strategy_strategy_id.delete.md) — 删除入群自动审批策略

### QQ群/群黑名单

- [GET /v2/groups/{group_openid}/member_blacklist](api/v2_groups_group_openid_member_blacklist.get.md) — 群黑名单查询
- [POST /v2/groups/{group_openid}/member_blacklist](api/v2_groups_group_openid_member_blacklist.post.md) — 群黑名单操作

### QQ群/群禁言

- [GET /v2/groups/{group_openid}/restrict_chat_setting](api/v2_groups_group_openid_restrict_chat_setting.get.md) — 查询群禁言状态
- [POST /v2/groups/{group_openid}/restrict_chat_setting](api/v2_groups_group_openid_restrict_chat_setting.post.md) — 设置群成员禁言

## 事件

### 通用/互动

- [INTERACTION_CREATE](event/interaction_create.md) — 互动事件

### 通用/订阅消息

- [SUBSCRIBE_MESSAGE_STATUS](event/subscribe_message_status.md) — 订阅消息授权状态变更

### 好友/好友关系

- [FRIEND_ADD](event/friend_add.md) — 用户添加好友
- [FRIEND_DEL](event/friend_del.md) — 用户删除好友

### 消息/单聊消息

- [C2C_MESSAGE_CREATE](event/c2c_message_create.md) — 单聊消息事件

### 好友/消息接收开关

- [C2C_MSG_RECEIVE](event/c2c_msg_receive.md) — 单聊消息接收开启
- [C2C_MSG_REJECT](event/c2c_msg_reject.md) — 单聊消息接收关闭

### QQ群/机器人

- [GROUP_ADD_ROBOT](event/group_add_robot.md) — 机器人加入群聊
- [GROUP_DEL_ROBOT](event/group_del_robot.md) — 机器人退出群聊
- [GROUP_JOIN_REQUEST](event/group_join_request.md) — 用户申请加群事件

### QQ群/消息接收开关

- [GROUP_MSG_RECEIVE](event/group_msg_receive.md) — 群聊消息接收开启
- [GROUP_MSG_REJECT](event/group_msg_reject.md) — 群聊消息接收关闭

### 消息/群聊消息

- [GROUP_AT_MESSAGE_CREATE](event/group_at_message_create.md) — 群@机器人消息
- [GROUP_MESSAGE_CREATE](event/group_message_create.md) — 群消息（全量模式）

### QQ群/群成员

- [GROUP_MEMBER_ADD](event/group_member_add.md) — 群成员加入
- [GROUP_MEMBER_REMOVE](event/group_member_remove.md) — 群成员退出

### QQ频道/频道管理

- [GUILD_CREATE](event/guild_create.md) — 频道创建
- [GUILD_UPDATE](event/guild_update.md) — 频道更新
- [GUILD_DELETE](event/guild_delete.md) — 频道解散
- [CHANNEL_CREATE](event/channel_create.md) — 子频道创建
- [CHANNEL_UPDATE](event/channel_update.md) — 子频道更新
- [CHANNEL_DELETE](event/channel_delete.md) — 子频道删除
