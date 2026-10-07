# QQ V2 SDK 覆盖范围

这份台账用于判断“代码中有具名入口”与“账号一定有权限”之间的区别。所有 API 都经过身份、代次和参数校验；写入或可能产生副作用的 API 还经过统一的 operation ledger。

## 当前规模

- 目录登记 98 个官方 HTTP target，其中 96 个已实现/可用、2 个明确 excluded；
- 64 个旧 SDK 风格的具名方法；
- 56 个业务事件，加上 READY/RESUMED 等连接通知（事件目录共 58 个名称）；
- 主要实现位于 `v2/sdk/api/{reads,messages,media,group_admin,guild_admin,menus}.py`。

## 方法分组

| 分组 | 代表入口 |
| --- | --- |
| 读取 | `get_gateway`、`me`、`me_guilds`、`get_group_info`、`get_group_member_info`、`get_group_member_list`、`get_guild`、`get_channels`、`get_message`、`get_panels` |
| 消息 | `post_group_message`、`post_c2c_message`、`post_message`、`post_dms`、`recall_*`、`patch_guild_message`、`on_interaction_result` |
| 媒体 | `post_group_file`、`post_c2c_file`、`post_*_upload_prepare`、`post_*_upload_part_finish`、`begin_upload` |
| 群管理 | `set_group_restrict_chat_setting`、`batch_remove_group_members`、`set_group_member_blacklist`、`approve_group_join_request`、入群策略 |
| 频道管理 | 频道/成员/角色/权限、公告、reaction、日程、语音和帖子相关方法 |
| 菜单面板 | `get_menu`、`put_menu`、`get_panel`、`create_panel`、`update_panel`、`delete_panel`、`set_panel_target` |

`iter_*` 便利方法有 200 页、5000 项、4 MiB、120 秒上限；单页方法只请求一次。SDK 1.2.1 兼容的少数路径会在源码注释和方法 docstring 中标注，不能据此推断现行账号权限。

## 不在覆盖范围内

OneBot 网络 action 表更小，且不会因为 SDK 新增具名方法而自动扩大。当前明确不提供完整好友列表、完整历史 `get_msg`、反向 WebSocket、HTTP POST 事件、quick operations、跨实例路由、数字 ID 语义和业务重放。

需要逐条核对路径、参数或事件形状时，直接查看 `v2/sdk/http_catalog.py`、`v2/sdk/event_types.py` 和对应测试；不要从方法名猜测 QQ API 行为。
