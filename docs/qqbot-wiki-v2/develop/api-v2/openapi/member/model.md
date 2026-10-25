<!-- 来源: https://bot.q.qq.com/wiki/develop/api-v2/openapi/member/model.html · 抓取 2026-10-25 -->

> 镜像校注（2026-09-19）：原 `openapi/guild/role_model.html#DefaultRoles` 已失效；本地引用改指现有角色对象页，其中默认角色章节名为 DefaultRoleIDs。

# 成员对象(Member)
### Member
| 字段名 | 类型 | 描述 |
| --- | --- | --- |
| user | [User](../user/model.md#user) | 用户的频道基础信息，只有成员相关接口中会填充此信息 |
| nick | string | 用户的昵称 |
| roles | string 数组 | 用户在频道内的身份组ID, 默认值可参考[DefaultRoles](../../server-inter/channel/role/member/role_model.md) |
| joined_at | ISO8601 timestamp | 用户加入频道的时间 |
### MemberWithGuildID
| 字段名 | 类型 | 描述 |
| --- | --- | --- |
| guild_id | string | 频道id |
| user | [User](../user/model.md#user) | 用户的频道基础信息 |
| nick | string | 用户的昵称 |
| roles | string 数组 | 用户在频道内的身份 |
| joined_at | ISO8601 timestamp | 用户加入频道的时间 |
