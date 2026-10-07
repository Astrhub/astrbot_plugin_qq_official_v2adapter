# 持久化、迁移与恢复

## 数据文件

owner 初始化以下自有资源：

| 资源 | 文件/目录 | 用途 |
| --- | --- | --- |
| Pages 设置 | `settings.sqlite3` | draft/applied、revision、版本历史和命令绑定 |
| 原始收件 | `transport.sqlite3` | receipt、pending、delivered、retained/quarantine |
| 消息账本 | `messaging.sqlite3` | identity、去重、reply source、sequence、operation、扩展状态 |
| 资料缓存 | `profiles.sqlite3` | 当前/历史成员资料和名单连续性 |
| 媒体临时区 | `media-spool/` | 受界限保护的本地媒体副本 |

凭据仍由 AstrBot 平台配置持有。按钮票据、QQ presigned URL 和正文不应写入日志；presigned URL 只在上传流程中短暂存在。

## 操作状态

消息和扩展账本把一次逻辑操作绑定到 robot、scene、target、source 和请求摘要。常见状态：

```text
reserved -> in_flight -> sent
                    -> rejected / not_sent / unknown / partial
```

初始化会把旧的 `in_flight` 标成 `unknown`、旧的 `reserved` 标成 `not_sent`，两者都不删除。unknown、partial 和历史淘汰的操作都不能通过删除记录或换 operation ID 自动重放。

## 升级与回退

升级前停机备份完整的插件数据目录和本体平台配置。不要只备份某一张 SQLite 表，也不要清理 `unknown`、pending 或 retained 收件。回退必须恢复与代码版本匹配的整套数据库和配置；不同版本的消息账本、transport schema 或扩展状态不能混用。

配置升级会规范自有字段、保留实例 ID/AppID/凭据，并清理旧 transport 别名或沙箱字段；读取/规范化本身不会连接 QQ。保存失败时保留旧内存配置。

## 恢复边界

- 原始收件箱恢复只补齐可确认的 checkpoint，不重复应用已完成的聊天转换或互动 ACK。
- 发送 unknown 只允许查询原账本；服务端是否已经产生副作用仍需由业务确认。
- 资料缓存带 `source`、`as_of`、`received` 和成员状态；历史资料不能直接恢复为当前管理员权限。
- 数据库损坏、容量耗尽或锁失败应让当前实例进入明确降级/失败状态，不应静默新建空库覆盖旧数据。
