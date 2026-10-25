# V2 镜像完整性核查

核查时间：2026-10-25（UTC+8）。范围：`develop/api-v2/` 文档，不包括站外 SDK、Agent 接入或登录绑定接口。

## 覆盖与验证

- 官方当前部署为 `v1.42.0`；从路由资源提取 210 个 V2 路由，合并目录与 `index.html` 别名后为 **192 个正文页面**，全部抓取成功并列入 `README.md`。
- 每页均校验 HTTP 200、最终 URL 与请求 URL 一致、站点版本一致、标题及正文非空；192 份原始 HTML 的 SHA-256 全部通过复核。
- 离线比对原始 HTML 与 Markdown：462 个代码块及语言、517 张表、22 张图片、287 个页内本地链接均无错误；README 目录与快速入口链接另行全量解析。
- 自动生成的 API 页面仍为 47 个、事件页面仍为 22 个；素材管理是指南页，不能仍把 47 当作全部业务接口数。
- 逐页 URL、响应、更新时间、哈希及差异状态见 [MANIFEST.json](MANIFEST.json)。

## 与 2026-09-19 本地镜像的差异

页面由 191 增至 192：新增 1 页、删除 0 页；既有页面中 34 页正文有差异、157 页一致。34 页中，10 页含字段、端点或说明增删，2 页仅目录顺序或列表编号变化，22 页为空白、链接、强调或图片资源呈现差异。

主要正文变化：

1. **素材管理**：新增 `server-inter/message/material-management.md`，覆盖 prepare、预签名 PUT、complete、游标查询、删除及 8 个素材错误码；Markdown 页新增 `qqbot-media://` 引用说明，变更记录新增 `20261021`。
2. **群/C2C 富媒体上传**：新增 `file_data` 二进制直传及 5MB 上限说明，调整 `file_name` 要求；群上传页补充语音 `mp3/wav`，富媒体概述新增消息类型与格式表。
3. **群事件**：`GROUP_MEMBER_ADD` 移除事件体和示例中的 `user_openid`；全量群消息的 `content` 补充成员 `<@member_openid>`、全体 `<@all>` 与 `mentions` 的关系。
4. **访问凭证**：`dev-prepare/access-token.md` 与 `interface-framework/api-use.md` 补充 `POST https://api.bot.qq.com/app/getAppAccessToken` 基础请求信息。
5. **用户事件**：补充 `FRIEND_ADD`、`FRIEND_DEL`、`C2C_MSG_REJECT`、`C2C_MSG_RECEIVE` 的 Intent、触发场景、权限和推送方式表。

官方变更记录只新增 `20261021` 素材管理一项；上表其余内容是当前线上正文与旧本地镜像的差异，不全部代表 2026-10-21 当日发布，也不证明真实接口权限或调用已通过。

## 失效引用与本地校注

2026-10-25 复核以下原始目标仍跳转至 V2「启动接入」：`gateway/message/model.html`、`openapi/guild/model.html`、`openapi/guild/role_model.html#DefaultRoles`。保留 2026-09-19 的四处本地校注和有效链接，另保留角色对象页不存在 `#DefaultRoles` 锚点的修正；不修改协议字段。

Markdown 示例中的模板变量和 `qqbot-media://` 引用不是待抓取页面。目录索引只有标题时保留原样，不填造正文。
