# 扩展、按钮、面板与群工具

## 按钮回调

稳定的第三方插件辅助位于 `api.py`：`button_callback(name)`、`media_card` 和 `MediaInput`。按钮回调必须是已加载 Star 的异步方法：

```python
from data.plugins.astrbot_plugin_qq_official_v2adapter.api import button_callback

class MyStar(Star):
    @button_callback("confirm")
    async def confirm(self, event):
        yield event.plain_result("已确认")
```

插件还需要在自己的卡片中使用 `event.qq.callback_button(...)` 产生票据。`keyboard_enabled` 默认关闭；票据绑定 handler fingerprint、route、generation、插件实例和设置 revision，默认 TTL 120 秒（可配置 30–300 秒），每个机器人最多保留 4096 条未过期票据。票据单次使用，换代、重载、设置变化、跨 route 或非本插件发布的票据都会失效。

互动 11/12 先独立 ACK，再运行业务回调；业务最多 8 并发。满载时仍 ACK 并记录 `not_executed`，不自动重放。QQ 管理员身份不等于 AstrBot 管理员身份。

卡片发送只接受一条完整 Json 组件；按钮、媒体和流式片段不能随意混合。发送 unknown 时先查询原操作，不要重新激活票据。

完整用户用法见 [按钮说明](../BUTTONS.md)。

## 托管面板

Pages 会从 AstrBot 当前命令注册表生成预览，检查命令绑定指纹、场景、权限和 QQ 菜单限制。设置编辑是本地 draft；远端同步需要 `remote_menu_sync=true`、管理员确认和 QQ 操作账本成功。

手动修改/删除托管面板前应暂停同步；unknown、远端漂移或账本不匹配时不自动接管，避免下一轮同步覆盖用户手工修改。

## 群工具

插件注册以下 13 个 `qq_v2_` 工具：

`get_group`、`get_member`、`list_members`、`find_known_members`、`list_mutes`、`mute_members`、`kick_members`、`list_blacklist`、`change_blacklist`、`list_join_requests`、`approve_join_request`、`list_join_strategies`、`get_operation_status`。

工具从触发消息绑定机器人、群和当前操作者，模型不能指定其他实例或群。禁言、踢人和黑名单变更每次最多 20 个显式 OpenID；分页结果必须继续使用官方 cursor。入群申请批准只能使用真实查询得到的有效 flag。unknown/partial 只允许查询原操作状态。
