# QQ V2 插件按钮

仅群聊/C2C，需 AstrBot 4.28.1+。单个原生 `Json` 表示一条完整卡片；发送共用路由、回复来源、额度和防重账本。

```python
from astrbot.api.event import MessageChain
from astrbot.core.message.components import Json

card = {
    "msg_type": 2,
    "markdown": {"content": "## 功能菜单\n请选择操作"},
    "keyboard": {"content": {"rows": [{"buttons": [{
        "id": "help", "render_data": {"label": "查看帮助", "style": 1},
        "action": {"type": 2, "permission": {"type": 2}, "data": "/帮助", "enter": False},
    }]}]}},
}
yield event.chain_result([Json(card)])
# 或：await event.send(MessageChain([Json(card)]))
# 主动发送：await self.context.send_message(umo, MessageChain([Json(card)]))
```

`keyboard: {"id": "模板ID"}` 与 `content.rows` 二选一。自定义按钮支持跳转、受控回调和指令预填；指令由用户实际发送后再走普通命令管线。群聊不能自动发送指令，C2C 可设 `enter=True`；权限与最终发送许可以 QQ 响应为准。仅接受一个 Json 组件；混合 Plain/多 Json、伪造 `msg_id`、`event_id`、`file_info` 或未知字段直接失败。

高级回调 API 的导入路径取决于实际安装目录；以下假定目录名为 `astrbot_plugin_qq_official_v2adapter`：

```python
from copy import deepcopy
from astrbot.api.event import filter
from astrbot.api.star import Star
from astrbot.core.message.components import Json
from data.plugins.astrbot_plugin_qq_official_v2adapter.api import button_callback

class MyStar(Star):
    @button_callback("confirm")
    async def confirm(self, event):
        item = event.get_extra("qq_button_data")
        yield event.plain_result(f"已确认 {item['item_id']}")

    @filter.command("功能")
    async def menu(self, event):
        button = event.qq.callback_button(self.confirm, label="确认", data={"item_id": "42"})
        payload = deepcopy(card)
        payload["keyboard"]["content"]["rows"][0]["buttons"] = [button]
        yield event.chain_result([Json(payload)])
```

`callback_button(function, *, label, data=None, audience="actor")` 只接受已装载 Star 的绑定方法。默认票据仅当前用户可点；显式 `audience="all"` 对应 QQ 的所有人权限，业务侧仍须自行校验。QQ 群管理员不等于 AstrBot 管理员，`permission.type=1` 不授予本地管理权限。`keyboard_enabled` 控制卡片回调；旧 `keyboard_execute` 只控制旧菜单无参命令票据。票据单次使用、过期/换代/禁用/热重载失效；互动 11/12 在原事件起 3 秒内先独立 ACK，再只调用注册的处理函数及其过滤器，不广播聊天监听器，也不触发 LLM。业务执行结果只能记录为已调度/未确认，不承诺外部副作用成功。

宿主会话白名单、会话整体启停和会话插件禁用在 ACK 后、业务前按当前配置检查。最多 8 个业务回调并发；满载时仍 ACK 新互动并持久记录 `callback_business_capacity`/`not_executed`，不自动重放。

媒体卡片优先传 AstrBot 原生 `Image`、`Record`、`Video`、`File`；高级接口仍支持本插件 `MediaInput`：

```python
from astrbot.core.message.components import Image
from data.plugins.astrbot_plugin_qq_official_v2adapter.api import media_card

image = media_card(Image.fromURL("https://example.com/image.png"), card["keyboard"])
await event.send(MessageChain([image]))
```

本地文件可使用 `Image.fromFileSystem(path)` 或 `File(name, file=file_uri)`；`MediaInput("image", source)` 仍可作为高级输入。
媒体按现有进程文件权限、Base64 大小限制或外链交 QQ 转存；上传后用本次目标自己的 `file_info` 发 `msg_type=7` + keyboard。Markdown 中的公网图片 URL 由 QQ 转存，不会把本地图片伪装成公网 URL。群没有原生流式；C2C `stream_messages` 也没有 keyboard 字段。流式片段含 Json/按钮时明确失败；结束流后按需另发卡片，不自动重发全文。

确定 `not_sent`/`rejected` 后调用方可另选纯文本；`result_unknown` 时先用 `event.bot.qq.send_status(operation_id)` 核对，不能自动重试或降级。真实 QQ 的媒体与按钮组合、客户端权限和 mini-program scheme 尚需独立平台验证。
