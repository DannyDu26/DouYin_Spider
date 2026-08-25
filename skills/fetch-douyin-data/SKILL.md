---
name: fetch-douyin-data
description: 调用 DouYin_Spider 项目的 HTTP API，检查服务健康状态、列出已认证账号、启动/查看/轮询/取消抖音扫码登录会话、获取作品详情和一级或二级评论、列出用户作品或搜索作品。当 Codex 需要通过二维码登录抖音，或通过本仓库的 FastAPI 服务获取抖音数据时使用。
---

# 获取抖音数据

使用本仓库的 FastAPI 服务，不要直接调用未公开的上游接口。绝不能在服务的凭据存储之外打印、返回或持久化账号 Cookie 和票据。

通过 `--base-url` 或 `DOUYIN_API_BASE_URL` 设置 API 根地址；默认使用 `https://crawler.xoyo.com/api/`。

## 检查前置条件

运行：

```powershell
python skills/fetch-douyin-data/scripts/douyin_client.py health
```

如果无法连接服务，请检查 DNS、HTTPS、Nginx 和服务健康状态。仅在明确进行本地开发时，使用 `--base-url http://127.0.0.1:5000/api/` 覆盖 API 根地址

不要虚构数据库凭据或修改环境文件。需要了解配置、请求字段、状态值或错误处理细节时，请阅读 [references/api.md](references/api.md)。

## 执行二维码登录

1. 使用稳定的账号别名启动会话：

```powershell
python skills/fetch-douyin-data/scripts/douyin_client.py login-start --account-id test-01
```

2. 从 JSON 输出中读取 `qrcode_path` 和 `session_id`。使用绝对本地图片路径向用户展示 PNG。二维码图片仅在创建会话时返回。
3. 告知用户二维码通常只有约一分钟的有效期。立即展示二维码，请用户马上扫码并在手机上确认，同时无需等待用户再次回复就开始轮询：

```powershell
python skills/fetch-douyin-data/scripts/douyin_client.py login-wait --session-id SESSION_ID --wait-timeout 70
```

不要声称二维码在 API 响应的 `expires_at` 时间之前始终有效。该时间戳表示服务会话的超时时间，二维码图片本身可能更早失效。

4. 如果轮询返回 `verification_required`，请在同一个浏览器会话中请求短信验证码。当状态已经是 `waiting_sms_code` 时，同一命令会重新发送验证码：

```powershell
python skills/fetch-douyin-data/scripts/douyin_client.py login-sms-request --session-id SESSION_ID
```

持续轮询，直到状态变为 `waiting_sms_code`，然后向用户询问验证码。通过客户端的隐藏输入提示提交验证码，避免验证码出现在命令行或普通输出中：

```powershell
python skills/fetch-douyin-data/scripts/douyin_client.py login-sms-verify --session-id SESSION_ID
```

仅在出现 `请输入短信验证码：` 提示时输入验证码，然后再次运行 `login-wait`。绝不能回显验证码或在最终回复中包含验证码。

5. 仅将 `succeeded` 视为登录成功。如果状态为 `failed`、`expired` 或 `cancelled`，请报告安全的服务端错误，并提议创建新会话。如果二维码图片已失效，但服务仍报告会话处于活动状态，请先取消该废弃会话，再创建替代会话：

```powershell
python skills/fetch-douyin-data/scripts/douyin_client.py login-cancel --session-id SESSION_ID
```

一个服务进程中只能存在一个活动的二维码会话。绝不能暴露凭据数据库记录或浏览器存储内容。

## 获取数据

使用与请求最匹配、范围最小的命令：

```powershell
# 获取一个或多个作品详情
python skills/fetch-douyin-data/scripts/douyin_client.py video-info --url "https://www.douyin.com/video/123"

# 也可以直接使用单个作品 ID
python skills/fetch-douyin-data/scripts/douyin_client.py video-info --video-id "123"

# 获取一级评论
python skills/fetch-douyin-data/scripts/douyin_client.py video-comments --url "https://www.douyin.com/video/123" --cursor 0 --count 20

# 也可以直接使用作品 ID 获取一级评论
python skills/fetch-douyin-data/scripts/douyin_client.py video-comments --video-id "123" --cursor 0 --count 20

# 获取指定一级评论下的二级评论
python skills/fetch-douyin-data/scripts/douyin_client.py video-sub-comments --video-id "123" --comment-id "456" --cursor 0 --count 20

# 获取用户作品
python skills/fetch-douyin-data/scripts/douyin_client.py user-videos --user-url "https://www.douyin.com/user/USER_ID" --page-num 1

# 也可以直接传用户主页路径中的用户 ID（sec_user_id）
python skills/fetch-douyin-data/scripts/douyin_client.py user-videos --user-id "USER_ID" --page-num 1

# 搜索作品
python skills/fetch-douyin-data/scripts/douyin_client.py search-videos --query "关键词" --limit 20
```

如实返回 API 的 JSON 数据。如果响应中包含 `account_id`、`failover_count`、部分条目错误和分页字段，请在回复中说明。回复接口仅提供指定一级评论下的二级评论。不要声称该 API 支持私信、直播间操作、媒体下载或任意上游接口。

## 处理故障

- 连接失败时，检查基础 URL 和服务进程。
- 遇到 `NO_AVAILABLE_ACCOUNT` 时，运行 `accounts`；仅在用户要求认证，或用户请求的数据获取操作必须认证时，才开始二维码登录。
- 遇到 `INVALID_REQUEST` 时，根据 [references/api.md](references/api.md) 修正请求。
- 遇到 `QR_VERIFICATION_REQUIRED` 时，说明服务必须以 `QR_LOGIN_HEADLESS=false` 运行，以便用户完成浏览器验证，然后重启服务。
- 遇到上游或内部故障时，提供安全的错误代码、消息和请求 ID。绝不能输出原始 HTTP 标头、Cookie、票据、数据库配置或含有敏感信息的堆栈跟踪。
