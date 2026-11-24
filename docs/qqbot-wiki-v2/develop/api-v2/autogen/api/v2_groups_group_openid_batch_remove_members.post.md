<!-- 来源: https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_batch_remove_members.post.html · 抓取 2026-11-24 -->

# 群成员批量移除

批量移除群成员，单次最多 20 个openid。


## 请求

### 基础信息

| 字段 | 值 |
| --- | --- |
| HTTP URL | /v2/groups/{group_openid}/batch_remove_members |
| HTTP Method | POST |
| 接口频率限制 | 30 QPM |

## 路径参数

| 名称 | 类型 | 必填 | 描述 |
| --- | --- | --- | --- |
| group_openid | string | 是 | 群OpenID |

## 请求体

| 名称 | 类型 | 必填 | 描述 |
| --- | --- | --- | --- |
| member_openids | []string | 是 | 需要移除的成员 member_openid 列表，单次最多 20 个 |

### 请求示例

```text
POST /v2/groups/3E5D8A1F7B2C9E4D6A0F1B3C5D7E9F2A/batch_remove_members
{
    "member_openids": [
        "7A3B9C1D5E2F4A6B8C0D1E3F5A7B9C2D"
    ]
}
```

## 响应

### 响应体

| 名称 | 类型 | 描述 |
| --- | --- | --- |
| remove_members_result | string | 成功时返回 success |

## 响应示例

```json
{
    "remove_members_result": "success"
}
```

### 错误码

| 错误码 | 描述 | 排查建议 |
| --- | --- | --- |
| 40103012 | 操作失败, 群主已经关闭操作权限 | 需要群主给机器人打开“移除群成员”权限开关。 |
| 40103011 | 操作失败, 机器人已经关闭操作权限 | 需要到WEB管理端给机器人打开"移除群成员"开关。 |

> 本地校注（2026-11-24）：官方页 2026-11-16 移除 `add_to_member_blacklist` 入参、`add_to_member_blacklist_fail_openids` 响应字段和“内邀接入”提示，错误码表改为上述两行（原先记录的 11253 已不在页面上）。
