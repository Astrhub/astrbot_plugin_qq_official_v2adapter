<!-- 来源: https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_info.get.html · 抓取 2026-11-16 -->

# 获取用户基本信息

获取指定 openid 的用户信息。

## 请求

### 基础信息

| 字段 | 值 |
| --- | --- |
| HTTP URL | /v2/users/{user_openid}/info |
| HTTP Method | GET |
| 接口频率限制 | 10 QPS |

## 路径参数

| 名称 | 类型 | 必填 | 描述 |
| --- | --- | --- | --- |
| user_openid | string | 是 | |

### 请求示例

**GET /v2/users/CADF012424CB537DDBC9950A90732538/info**

```text
GET /v2/users/CADF012424CB537DDBC9950A90732538/info
```

## 响应

### 响应体

| 名称 | 类型 | 描述 |
| --- | --- | --- |
| union_openid | string | 跨应用关联用户openid，当绑定了3方应用时有效 |
| username | string | 用户名 |
| avatar | string | 用户头像原图 |
| allow_proactive_msg | boolean | 是否接收主动推送。true: 接受主动推送 |

## 响应示例

**用户没有开启主动消息返回**

```json
{
  "username": "笑😊心❤️",
  "allow_proactive_msg": false,
  "avatar": "https://thirdqq.qlogo.cn/bqqthird?key=AwviarjcoNVXmyUAEwW0xtGErhFibuWOzWYdUST20wEvz18MYo293c78BvRFt2s5Is&kti=avZwqQAAAAI&s=640"
}
```

**用户开启了主动消息返回**

```json
{
  "username": "笑😊心❤️",
  "allow_proactive_msg": true,
  "avatar": "https://thirdqq.qlogo.cn/bqqthird?key=AwviarjcoNVXmyUAEwW0xtGErhFibuWOzWYdUST20wEvz18MYo293c78BvRFt2s5Is&kti=avZwqQAAAAI&s=640"
}
```

### 错误码

| 错误码 | 描述 | 排查建议 |
| --- | --- | --- |
| 40184001 | 无好友关系 | 用户与机器人无好友关系，无法查询/发送消息 |
