# 小红书登录 HTTP API

本 skill 的接口契约来自本仓库 `app/xhs_router.py`、`app/xhs_schemas.py` 和 `app/xhs_login_service.py`。默认根地址为 `https://crawler.xoyo.com/api/`，所有下表路径相对该根地址。

| 命令 | 方法与路径 | JSON 请求 |
| --- | --- | --- |
| `health` | `GET health` | 无 |
| `accounts` | `GET v1/xhs/auth/accounts` | 无 |
| `login-start` | `POST v1/xhs/auth/qr/create` | `account_id` |
| `login-wait` | `POST v1/xhs/auth/qr/login` | `session_id` |
| `login-sms-send` | `POST v1/xhs/auth/sms/send` | `account_id`、隐藏输入的 `phone`、`zone`（默认 `86`） |
| `login-sms-verify` | `POST v1/xhs/auth/sms/login` | `session_id`、隐藏输入的 `code` |

`account_id` 匹配 `^[a-z0-9][a-z0-9_-]{0,63}$`，是自定义凭据别名，不是小红书用户 ID。`session_id` 是服务返回的本次会话 ID。手机号为 5～15 位数字，不含区号；区号为 1～4 位数字；验证码为 4～8 位数字字符串，保留开头的零。

## 响应与等待

成功信封包含 `success: true`、`request_id`、`data`。登录数据包括 `session_id`、`account_id`、`status`、`created_at`、`expires_at`。短信数据另有 `phone_masked`，完整手机号不会返回。

扫码创建仅在 `waiting_scan` 时返回 `qrcode_data_url`（Base64 PNG），客户端将其替换为绝对路径 `qrcode_path` 后输出，不打印二维码原始数据。扫码登录阻塞至成功或错误；重复提交相同会话可接回现有后台任务，成功结果在保留期内可重读，不重复保存凭据。本地断开不会取消后台任务。

内部扫码状态为 `starting`、`waiting_scan`、`waiting_confirm`、`committing`、`succeeded`，也可能进入 `verification_required` 或终态 `expired`、`failed`、`cancelled`。短信内部还使用 `ready`、`sending_sms`、`waiting_sms_code`、`verifying_sms`。这些内部状态不代表存在可调用的状态查询路由；公开接口是表中的两步流程。

账号列表是 `data.items`，每项包含 `account_id`、`credential_id`、`updated_at`、`status`、`cooldown_until`，不含 Cookie。`available` 表示当前可用；认证成功与可用状态分别报告。

## 服务配置

服务必须启用小红书模块并配置现有凭据存储和 Node.js 签名运行时；客户端不需要 Node.js 或 Playwright。复用已有部署，不自动更改配置。登录会话驻留内存，应使用单服务实例和一个 Uvicorn worker，服务重启会丢失会话。

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `XHS_QR_SESSION_TIMEOUT_SECONDS` | 180 | 扫码会话时限 |
| `XHS_SMS_SESSION_TIMEOUT_SECONDS` | 300 | 短信会话时限，最低 300 秒；更小的配置按 300 秒处理 |
| `XHS_SMS_SESSION_RETENTION_SECONDS` | 300 | 两种登录的终态保留时间 |
| `XHS_SMS_RESEND_INTERVAL_SECONDS` | 60 | 短信重发最小间隔 |
| `XHS_SMS_MAX_SESSIONS` | 10 | 两种登录共享活动会话上限 |
| `XHS_SMS_MAX_CODE_ATTEMPTS` | 5 | 短信验证码尝试上限 |

普通 HTTP 请求超时由顶层 `--timeout` 设置，默认 30 秒；扫码等待单独由 `login-wait --wait-timeout` 设置，默认 240 秒。反向代理的读取超时也应覆盖服务端等待时长；修改代理属于部署工作，不由 skill 自动执行。

## 错误处理

错误信封为 `success: false`、`request_id`、`error: {code, message, details?}`。客户端以 JSON 输出安全服务错误，退出码为 1；正常退出为 0，中断为 130。非 JSON 网关错误不回显原始页面。

| 错误码 | 处理 |
| --- | --- |
| `XHS_UNAVAILABLE` / `XHS_LOGIN_UNAVAILABLE` | 小红书数据或登录服务不可用，检查服务配置 |
| `XHS_SMS_SESSION_ACTIVE` | 同账号或手机号已有活动会话（扫码也可能返回该码），不要重复创建 |
| `XHS_SMS_SESSION_LIMIT` | 会话数量已满，等待已有会话结束 |
| `XHS_LOGIN_SESSION_NOT_FOUND` | 会话不存在、已清理或服务已重启，需要重新开始 |
| `XHS_QR_EXPIRED` / `XHS_LOGIN_SESSION_ENDED` | 会话结束，用户准备好后重新登录 |
| `XHS_QR_STATE_CONFLICT` / `XHS_SMS_STATE_CONFLICT` / `XHS_QR_SESSION_REQUIRED` | 检查会话类型和调用步骤 |
| `XHS_SMS_SEND_TOO_SOON` | 按 `details.retry_after_seconds` 等待，仅在用户要求时重发 |
| `XHS_SMS_INVALID_CODE` | 向用户索取正确验证码，在原会话重试；超尝试上限会结束会话 |
| `XHS_RISK_CONTROL` | 平台要求安全验证；停止自动尝试，报告限制 |
| `XHS_CREDENTIAL_SAVE_FAILED` / `XHS_LOGIN_NOT_AUTHENTICATED` | 没有完成正式凭据保存和入池，不报告成功 |
| `XHS_QR_CREATE_FAILED` / `XHS_QR_INVALID_INITIAL_STATE` / `XHS_QR_POLL_FAILED` | 报告安全错误及请求 ID，不暴露上游响应 |
| 客户端 `REQUEST_TIMEOUT` / `CONNECTION_ERROR` | 扫码时使用同一会话再次等待；不要直接创建替代会话 |
| 客户端 `INTERACTIVE_INPUT_REQUIRED` | 使用支持隐藏输入的交互终端，或与用户改用扫码 |

此 API 不公开登录状态查询、取消或验证挑战处理路由。不要调用抖音的 `/qr-sessions`，也不要调用旧 `Spider_XHS/save_login.py` 或把凭据写入本地 `.env`。
