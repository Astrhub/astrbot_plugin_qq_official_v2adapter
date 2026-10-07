# QQ V2 按钮与卡片

本文说明如何从 AstrBot 插件发送 QQ 原生 Json 卡片和受控按钮。按钮回调默认关闭，先在 Pages 本地设置中启用 `keyboard_enabled`。

## 发送一张卡片

```python
from astrbot.api.event import MessageChain
from astrbot.core.message.components import Json

card = {
    "msg_type": 2,
    "markdown": {"content": "## 功能菜单\n请选择操作"},
    "keyboard": {"content": {"rows": [{"buttons": [{
        "id": "help",
        "render_data": {"label": "查看帮助", "style": 1},
        "action": {"type": 2, "permission": {"type": 2},
                   "data": "/帮助", "enter": False}
    }]}]}}
}
yield event.chain_result([Json(card)])
```

一次发送只允许一条完整 Json 组件。`keyboard` 使用模板 ID 或自定义 rows 二选一；不接受伪造 `msg_id`、`event_id`、`file_info` 或未知字段。群聊不能自动替用户执行指令；C2C 可以按 QQ 规则使用 `enter=true`。

## 绑定插件回调

```python
from data.plugins.astrbot_plugin_qq_official_v2adapter.api import button_callback

class MyStar(Star):
    @button_callback("confirm")
    async def confirm(self, event):
        data = event.get_extra("qq_button_data")
        yield event.plain_result(f"已确认 {data['item_id']}")

@filter.command("菜单")
async def menu(self, event):
    button = event.qq.callback_button(
        self.confirm, label="确认", data={"item_id": "42"}
    )
```

票据绑定当前 route、generation、设置 revision 和 handler fingerprint，默认只允许原点击者使用；`audience="all"` 才允许所有人点击。票据单次使用、TTL 默认 120 秒，单机器人最多保留 4096 条未过期票据。互动先 ACK，再进入最多 8 并发的业务队列；队列满时不会自动重放。

媒体卡片可使用 AstrBot 的 `Image`、`Record`、`Video`、`File` 或 `media_card` 辅助。流式消息不能包含按钮；需要在流结束后另发卡片。发送 `unknown` 时先查询 operation ID，不要重新发布旧票据。
