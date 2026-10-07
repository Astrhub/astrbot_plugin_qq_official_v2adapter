# 发送、媒体与幂等

## 统一发送核心

`SendingCore` 负责 Plain/Markdown/At/Reply/CQ/Json/媒体的组合校验、路由、来源、序号和写账本。一条消息链无法完整映射到 QQ 时直接失败，不静默丢媒体、不伪造引用、不自动拆多条。

发送结果包含 `operation_id` 和 `delivery` 摘要。`MessageStore` 持久化来源、去重键、消息序号、请求阶段和结果。相同 operation ID 的目标、来源或内容改变会报冲突。

## 被动与主动

群来源通常有效 5 分钟，C2C 使用保守的 60 分钟窗口。被动请求仅在 QQ 明确拒绝来源且错误属于白名单时，最多转换为一次主动发送。宿主 `send_by_session` 另有一个针对 `40034105` 的一次性主动→被动候选转换。

以下结果禁止自动重放：超时、断线、5xx、`unknown`/`result_unknown`、`partial`、已开始发送的流或已确认的部分媒体。业务应先调用 `send_status()`/`extension_status()`，根据真实状态决定后续动作。

## 流式与 typing

```python
mode = event.bot.qq.streaming_mode("c2c", openid)
result = await event.bot.qq.send_streaming(
    "c2c", openid, chunks, input_mode="append"
)
```

- C2C 使用官方 `stream_messages`，首片获得 `stream_msg_id`，后续片递增 index 并复用同一 ID/来源序号。
- 群、频道和 DM 没有原生流；`stream_fallback=aggregate` 时按界限聚合，设置为 `reject` 时明确拒绝。
- 每条流最多 4096 字符、默认 120 秒，最多 8 条待处理流；媒体、引用、按钮不会被悄悄塞进文本流。
- typing 只对 C2C 生效，需要 `typing_enabled=true` 和真实被动来源；不续期，也不在不支持的场景伪造状态。

## 媒体

外链 URL 交 QQ 转存，本机不 GET/HEAD。支持的本地输入由 AstrBot MediaResolver 读取；路径、Base64、bytes 和 stream 都受边界检查。Base64 单项上限 8 MiB，原始媒体最大 200 MiB，BlobPool 默认最多 384 MB/32 个 blob。

群/C2C 的大文件流程是：

```python
async with await client.qq.begin_upload(
    "group", group_openid, path, kind="image", name="photo.png"
) as task:
    for part in task.parts:
        await task.put_part(part["index"])
        await task.finish_part(part["index"])
    receipt = await task.complete(srv_send_msg=False)
```

服务器返回的 part index 可能从 0 或 1 开始，必须原样使用。预签名 PUT 不携带 QQ token、Cookie，也不跟随跳转；取消、断线或未知结果不会重传已确认的字节。`srv_send_msg=True` 会把合并后的发送写入消息账本。

频道只支持图片 URL 或本地 multipart 图片；DM 只支持图片 URL。媒体软上限超过时默认允许按文件发送，可通过 `allow_file_fallback=false` 禁止降级。

## 原生响应与生命周期

`NativeUploadHandle`、`with_options(owner=...)` 和流任务都绑定平台代次。插件卸载、配置重载或 route 改变后，句柄必须关闭并返回 stale 错误。调用方创建的输入流仍由调用方管理；句柄只关闭适配器创建的临时副本。
