# 事件模型与订阅

## 宿主聊天事件

聊天消费者只把以下六类事件送入 AstrBot 标准消息管线：

| QQ 事件 | 场景 | 身份字段 |
| --- | --- | --- |
| `GROUP_AT_MESSAGE_CREATE` | `group` | `member_openid` |
| `GROUP_MESSAGE_CREATE` | `group` | `member_openid` |
| `C2C_MESSAGE_CREATE` | `c2c` | `user_openid` |
| `AT_MESSAGE_CREATE` | `channel` | channel user ID |
| `MESSAGE_CREATE` | `channel` | channel user ID |
| `DIRECT_MESSAGE_CREATE` | `dm` | channel user ID |

`V2MessageEvent` 绑定当前代次的 route、来源和 client。事件的 raw envelope 保留在事件对象中；引用只能根据官方提供的合法索引/引用元素还原，不能凭 ID 猜正文或作者。

入站附件只保留结构化元数据和 URL，按需消费：图片映射 `Image`，voice 与 `audio/wav`、`audio/x-wav`、`audio/mpeg` 映射 `Record`，`video/mp4` 映射 `Video`，未知类型映射 `File` 或 `Unknown`。

## NativeEvent

同一核心事件（包括聊天事件）也会通过 `client.qq.events` 暴露；其中六类聊天事件还会进入 AstrBot 宿主消息管线。`NativeEvent.payload`/`d` 是递归冻结视图；`raw()` 返回隔离的可写副本。`typed` 只有在事件形状校验通过时才返回 TypedDict 视图；缺字段、类型错误或未知事件仍保留 raw，但 `typed` 为 `None`。

事件目录由 `v2/sdk/catalog.py` 管理，当前登记 56 个业务事件以及 READY/RESUMED 等连接通知。SDK 订阅不会新增 QQ Intents，也不会为 Webhook 自动打开管理端监听。

## 订阅示例

```python
async def on_member(event):
    if event.typed is None:
        return
    member = event.typed["d"]
    print(member)

subscription = event.bot.qq.events.subscribe(
    {"GROUP_MEMBER_ADD", "GROUP_MEMBER_REMOVE"},
    callback=on_member,
    owner=self,
)

# 在插件 terminate 时
await subscription.close()
```

也可以使用 `events.stream(names, owner=self)` 获取同语义的异步流。回调必须是 async 函数。默认每个订阅 64 项容量、单项 5 秒超时；每个实例最多 32 个订阅，整个待消费事件预算 16 MiB。队列满或字节超限会关闭并诊断该订阅；回调超时/异常会被隔离并计数，订阅继续消费。任务取消可能产生 `subscription_gap`，但不会阻塞 QQ ACK 或宿主聊天投递。

`include_recovered=True` 只允许观察启动时仍未被核心处置的收件。它不提供进程崩溃后的 exactly-once、全局顺序或历史事件回放。

## 进度与所有权

```python
progress = event.bot.qq.events.progress(event.context.receipt)
```

receipt 只描述本实例收件箱的阶段，不代表 QQ 业务已成功。owner 卸载或平台重载后，旧订阅返回 `stale_owner`/`stale_generation`；其他实例和订阅不受影响。
