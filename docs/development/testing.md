# 测试与 CI

## 环境

CI 使用 Ubuntu 24.04、Python 3.12、Node 24、bubblewrap，以及固定的 AstrBot host revision。测试禁止连接真实 QQ；所有上游请求必须映射到本地 fixture。

需要先准备 AstrBot host checkout 和虚拟环境，然后从仓库根目录运行隔离脚本：

```bash
ASTRBOT_SOURCE="$PWD/.host-source" \
PYTHON_ENV="$PWD/.host-source/.venv" \
bash scripts/test-isolated.sh -q
```

脚本会使用独立 user/PID/network namespace、只读挂载源码和临时数据。不要直接运行 `pytest tests`：`tests/conftest.py` 会拒绝不在隔离沙箱中的导入。

## 回归分组

| 主题 | 重点测试 |
| --- | --- |
| 装配与生命周期 | `test_assembly.py`, `test_lifecycle.py`, `test_connection_settings.py` |
| 网关与收件箱 | `test_gateway_backoff.py`, `test_gateway_shards.py`, `test_inbox_capacity.py`, `test_inbox_recovery.py` |
| 事件与 SDK | `test_sdk_event_shapes.py`, `test_sdk_events.py`, `test_sdk_ingress.py`, `test_sdk_public_contracts.py` |
| 消息与来源 | `test_messaging_delivery.py`, `test_messaging_faults.py`, `test_reply_fallback.py`, `test_streaming.py` |
| 媒体 | `test_media_contract.py`, `test_media_boundary.py`, `test_media_faults.py`, `test_media_upload.py` |
| 互动与管理 | `test_button_callbacks.py`, `test_extension_dispatch.py`, `test_management.py`, `test_sdk_group_tools.py` |
| 旧 OneBot 网络兼容 | `test_onebot_network.py`, `test_onebot_events_resources.py`, `test_onebot_config_instances.py` |
| Pages | `test_page.py`, `test_panel_config_routing.py`, `test_panel_resilience.py` |

## 编写测试时的约束

- 使用临时 SQLite 和本地 aiohttp upstream，不要读取操作者的 AstrBot 数据目录。
- 为每个测试建立独立 robot/platform identity；不要依赖数字 ID 或全局单例。
- 覆盖 unknown、partial、stale generation、配置冲突和持久化恢复路径。
- 发送测试必须断言 operation ledger，而不只断言 HTTP mock 被调用。
- 修改页面时运行 Node 页面测试，并在 PR 中附页面截图。
- 旧 OneBot 网络测试用于维护迁移期兼容行为；新增集成能力应在 SDK、事件和发送账本测试中覆盖。

## CI 变更

修改传输、协议、存储 schema 或公共 API 时，同时更新 `.github/workflows/tests.yml`、迁移说明和对应回归组。测试通过不代表 QQ 账号具备某项官方权限；需要把“代码支持”和“账号授权”分开记录。
