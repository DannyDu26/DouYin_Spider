---
name: fetch-douyin-data
description: Call the DouYin_Spider project's HTTP API to check service health, list authenticated accounts, start/show/poll/cancel Douyin QR-code login sessions, fetch video details and first-level or second-level comments, list a user's videos, or search videos. Use when Codex needs to log in to Douyin by QR code or retrieve Douyin data through this repository's FastAPI service.
---

# Fetch Douyin Data

Use the repository's FastAPI service instead of calling undocumented upstream endpoints directly. Never print, return, or persist account cookies and tickets outside the service's credential store.

## Locate the repository

Resolve the repository root from the current working directory. Confirm that `main.py`, `qr_login_service.py`, and `skills/fetch-douyin-data/scripts/douyin_client.py` exist. Run all commands from that root.

Set the API root with `--base-url` or `DOUYIN_API_BASE_URL`; default to `https://crawler.xoyo.com/api/`.

## Check prerequisites

Run:

```powershell
python skills/fetch-douyin-data/scripts/douyin_client.py health
```

If the service is unreachable, verify DNS, HTTPS, Nginx, and service health. For explicit local development, override the API root with `--base-url http://127.0.0.1:5000/api/` and start one worker with:

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 5000 --workers 1
```

Do not invent database credentials or modify environment files. Read [references/api.md](references/api.md) when setup, request fields, status values, or error handling details are needed.

## Perform QR-code login

1. Start a session with a stable account alias:

```powershell
python skills/fetch-douyin-data/scripts/douyin_client.py login-start --account-id marketing-01
```

2. Read `qrcode_path` and `session_id` from the JSON output. Display the PNG to the user with an absolute local image path. The QR image is returned only when the session is created.
3. Tell the user that the QR image is usually valid for only about one minute. Display it immediately, ask them to scan and confirm on the phone at once, and start polling without waiting for another user reply:

```powershell
python skills/fetch-douyin-data/scripts/douyin_client.py login-wait --session-id SESSION_ID --wait-timeout 70
```

Do not describe the QR image as valid until the API response's `expires_at`. That timestamp is the service session timeout; the QR image itself can expire earlier.

4. Treat only `succeeded` as successful. For `failed`, `expired`, or `cancelled`, report the safe server error and offer to create a new session. If the QR image has expired but the service still reports an active session, cancel that abandoned session before creating a replacement:

```powershell
python skills/fetch-douyin-data/scripts/douyin_client.py login-cancel --session-id SESSION_ID
```

Only one QR session may be active in a service process. Never expose credential database rows or browser storage.

## Fetch data

Use the narrowest command matching the request:

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

# 也可以从该用户任一作品 ID 解析作者并获取作品
python skills/fetch-douyin-data/scripts/douyin_client.py user-videos --video-id "123" --page-num 1

# 搜索作品
python skills/fetch-douyin-data/scripts/douyin_client.py search-videos --query "关键词" --limit 20
```

Return the API's JSON data faithfully. Mention `account_id`, `failover_count`, partial item errors, and pagination fields when present. The reply API only exposes second-level comments for a specified first-level comment. Do not claim that this API exposes private messages, live-room operations, media downloads, or arbitrary upstream endpoints.

## Handle failures

- On connection failure, verify the base URL and service process.
- On `NO_AVAILABLE_ACCOUNT`, run `accounts`; begin QR login only when the user asks to authenticate or authentication is required for their requested fetch.
- On `INVALID_REQUEST`, correct the request using [references/api.md](references/api.md).
- On `QR_VERIFICATION_REQUIRED`, explain that the service must run with `QR_LOGIN_HEADLESS=false` so the user can complete browser verification, then restart it.
- On upstream or internal failure, include the safe error code, message, and request ID. Never dump raw HTTP headers, cookies, tickets, database configuration, or tracebacks containing secrets.
