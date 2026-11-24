<!-- 来源: https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_add.html · 抓取 2026-11-24 -->

# 群成员加入

有新成员加入群聊时触发。

## 事件

| 字段 | 值 |
| --- | --- |
| 事件名 | GROUP_MEMBER_ADD |
| Intent | GROUP_MEMBER_EVENT (1<<24) |

### 事件体

| 名称 | 类型 | 描述 |
| --- | --- | --- |
| timestamp | integer | 事件时间戳（Unix 秒） |
| group_openid | string | 群 OpenID |
| union_group_openid | string | 跨应用关联群openid，当绑定了3方应用时有效 |
| member_openid | string | 新加入成员的 OpenID |

> 本地校注（changelog 20261116）：事件体已写入 `union_group_openid`。官方 autogen 页表示例尚未同步。该字段不作为群路由主键。

### 事件示例

**示例1**

```text
{
  "timestamp": 1784276757,
  "group_openid": "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5",
  "member_openid": "C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6"
}
```
