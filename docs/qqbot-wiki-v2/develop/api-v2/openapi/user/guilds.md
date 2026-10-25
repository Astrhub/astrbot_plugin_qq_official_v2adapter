<!-- 来源: https://bot.q.qq.com/wiki/develop/api-v2/openapi/user/guilds.html · 抓取 2026-10-25 -->

# 获取用户频道列表
### 接口
```http
GET /users/@me/guilds
```
### 功能描述
用于获取当前用户（机器人）所加入的频道列表，支持分页。
当 `HTTP Authorization` 中填入 `Bot Token` 是获取机器人的数据，填入 `Bearer Token` 则获取用户的数据。
[票据说明](../../index.md#%E6%8E%A5%E5%85%A5%E7%A5%A8%E6%8D%AE)。
### Content-Type
```http
application/json
```
### 参数
| 字段名 | 类型 | 描述 | 要求 |
| --- | --- | --- | --- |
| before | string | 读此 guild id 之前的数据 | before 设置时， 先反序，再分页 |
| after | string | 读此 guild id 之后的数据 | after 和 before 同时设置时， after 参数无效 |
| limit | int | 每次拉取多少条数据 | 默认 100, 最大 100 |
### 返回
返回 [Guild](../../server-inter/channel/manage/guild/model.md#guild) 对象数组。
> 镜像校注（2026-09-19）：原目标 `openapi/guild/model.html` 已跳转至启动首页；本地引用修正为现有频道对象文档。
### 错误码
详见[错误码](../error/error.md)。
### 示例
响应数据包
```json
[{
    "id": "696527283900292399",
    "name": "鹅们的萌宠啦咔咔啦",
    "icon": "https://groupprohead-76292.picgzc.qpic.cn/482231626508223/100?t=1626508224633",
    "owner_id": "4828365788198541698",
    "owner": false,
    "joined_at": "2021-12-08T16:12:31+08:00",
    "member_count": 17,
    "max_members": 300,
    "description": "123"
}]
```
