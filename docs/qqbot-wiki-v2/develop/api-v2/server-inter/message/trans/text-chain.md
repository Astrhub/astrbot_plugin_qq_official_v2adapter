<!-- 来源: https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html · 抓取 2026-11-04 -->

> 镜像校注（2026-11-04，非官方正文）：本插件出站 `At` 的 `id="all"` 当前只允许文字子频道；群聊会在联网前返回 `unsupported`，不能据此校注推断账号已获群 @全部成员权限。实际编码与发送调用链见[本地实现调用链](#本地实现调用链)。

## 本地实现调用链

> 镜像校注（2026-11-04，非官方正文）：以下链路按本插件当前源码整理。

```text
消息链 At / 原生 content 或 markdown.content
→ SendingCore.send → parse_message
→ build_body → check_at / MarkupCheck（场景、指令和属性校验）
→ ReplyDelivery.apply（被动来源与 msg_seq）
→ HTTPTransport.request → POST 对应 messages 路径
→ MessageStore.finish(sent)
```

`build_body` 对 `At.qq` 编码为 `<qqbot-at-user id="..." />`，文字子频道的 `At.qq="all"` 编码为 `<qqbot-at-everyone />`；Markdown 原文中的 QQ 指令由 `MarkupCheck` 校验，不转义合法标签。群聊 @用户可以使用统一消息链，群聊 @全部成员会被 `check_at` 在联网前拒绝；原生接口按其实际请求体发送，账号权限与频控仍由 QQ 服务端决定。

# 文本交互

> **说明**
>
> QQBot 提供文本消息的交互能力，当开发者使用指定的格式发送消息，用户即可在消息体上进行点击交互操作，例如@某人，跳转链接等。

## 使用 @ 能力

嵌入文本使用格式：

- @用户：`<qqbot-at-user id="" />`
- @全部成员：`<qqbot-at-everyone />`

协议：`<@userid>`、`@everyone` 即将弃用，请使用上述最新格式。

### 群聊

| 能力 | 嵌入文本使用格式 | 客户端展示 | 支持的消息类型 | 使用条件与限制 |
| --- | --- | --- | --- | --- |
| @用户 | `<qqbot-at-user id="" />` | @用户 标签 | markdown 消息 | `id` 填被 @ 群成员的 `member_openid` |
| @全部成员 | `<qqbot-at-everyone />` | @全部成员 标签 | 文本消息、markdown 消息 | 机器人需为该群**管理员**，且**每个群每天仅可发送 1 次**（自然日） |

### 文字子频道

| 能力 | 嵌入文本使用格式 | 客户端展示 | 支持的消息类型 | 使用条件与限制 |
| --- | --- | --- | --- | --- |
| @用户 | `<qqbot-at-user id="" />` | @用户 标签 | 文本消息、图文消息、markdown 消息 | `id` 填被 @ 用户的 `user_openid` |
| @全部成员 | `<qqbot-at-everyone />` | @全部成员 标签 | 文本消息、图文消息、markdown 消息 | 机器人需拥有发送 @全部成员 消息的权限 |

## 指令操作

目前仅在 markdown 支持。

**1. 回车指令格式（点击后，文本直接发送）**

嵌入文本使用格式：

`<qqbot-cmd-enter text="xxx" />`

客户端展示为： /回车指令 用户可点击的标签，群聊和文字子频道不支持该能力。

- `text` 用户点击后直接发送的文本，参数必填，最大限制 100 字符，传值时需要 urlencode。

**2. 参数指令格式（点击后，文本插入输入框，用户自行编辑发送）**

嵌入文本使用格式：

`<qqbot-cmd-input text="xxx" show="xxx" reference="false" />`

客户端展示为： /参数指令 用户可点击的标签

- `text` 用户点击后插入输入框的文本，参数必填，最大限制 100 字符，传值时需要 urlencode。
- `show` 用户在消息内看到的文本，参数选填，默认取 text 值，最大限制 100 字符，传值时需要 urlencode。
- `reference` 插入输入框时是否带消息原文回复引用，参数选填，默认为 `false`，填入 `true` 时则带引用回复到输入框中。

## 跳转子频道

仅频道可用。

嵌入文本使用格式：`<#channel_id>`

客户端展示为： #XXX文字子频道 标签，点击可以跳转至子频道，仅支持当前频道内的子频道。

## 表情

仅频道可用，解析为系统表情。 具体表情id参考 [Emoji 列表](../../../openapi/emoji/model.md#emoji%E5%88%97%E8%A1%A8)。

嵌入文本使用格式：`<emoji:id>`

- 仅支持 `type = 1` 的系统表情。
- `type = 2` 的 emoji 表情直接按字符串填写即可。
