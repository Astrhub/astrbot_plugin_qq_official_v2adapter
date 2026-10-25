<!-- 来源: https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/url_get.html · 抓取 2026-10-25 -->

# 获取通用 WSS 接入点
### 接口
```http
GET /gateway
```
### 功能描述
用于获取 WSS 接入地址，通过该地址可建立 `websocket` 长连接。
### Content-Type
```http
application/json
```
### 返回
返回一个用于连接 `websocket` 的地址。
### 错误码
详见[错误码](../error/error.md)。
### 示例
响应数据包
```json
{
  "url": "wss://api.bot.qq.com/websocket/"
}
```
