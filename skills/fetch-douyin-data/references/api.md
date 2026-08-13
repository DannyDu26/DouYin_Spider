# DouYin Spider HTTP API

## Prerequisites

- Use `https://crawler.xoyo.com/api/` as the default API root. Override it with `--base-url` or `DOUYIN_API_BASE_URL` only when needed.
- Run Python 3.10+ and install `requirements.txt`.
- Install Chromium with `python -m playwright install chromium` for QR login.
- Configure the project's MySQL credential store through `.env.dev`, `.env.prod`, or process environment variables.
- Keep one Uvicorn worker and one service instance because the account cursor and QR sessions are process-local.
- Use `QR_LOGIN_HEADLESS=false` when a visible browser is needed to complete an intermediate verification page.

## Endpoints

| Command | Method and path | Request |
| --- | --- | --- |
| `health` | `GET /api/health` | None |
| `accounts` | `GET /api/v1/douyin/auth/accounts` | None |
| `login-start` | `POST /api/v1/douyin/auth/qr-sessions` | `account_id`: `^[a-z0-9][a-z0-9_-]{0,63}$` |
| `login-status` | `GET /api/v1/douyin/auth/qr-sessions/{id}` | None |
| `login-cancel` | `DELETE /api/v1/douyin/auth/qr-sessions/{id}` | None |
| `video-info` | `POST /api/v1/douyin/video_info` | `video_id` or `urls`: 1–20 HTTPS Douyin video URLs |
| `video-comments` | `POST /api/v1/douyin/video_comments` | `url` or `video_id`, `cursor` ≥ 0, `count` 1–50 |
| `video-sub-comments` | `POST /api/v1/douyin/video_sub_comments` | `video_id`, `comment_id`, `cursor` ≥ 0, `count` 1–50 |
| `user-videos` | `POST /api/v1/douyin/user_videos` | `/user/{id}` URL or `video_id`, `page_num` 1–10 |
| `search-videos` | `POST /api/v1/douyin/search_videos` | See search fields below |

QR states are `starting`, `waiting_scan`, `committing`, `succeeded`, `expired`, `failed`, and `cancelled`. The creation response is the only response containing `qrcode_data_url`.

Search fields:

| Field | Values |
| --- | --- |
| `limit` | 1–100 |
| `sort_type` | `0` comprehensive, `1` most liked, `2` newest |
| `publish_time` | `0` any, `1` one day, `7` one week, `180` half year |
| `filter_duration` | empty, `0-1`, `1-5`, `5-10000` |
| `search_range` | `0` any, `1` viewed, `2` unseen, `3` followed users |
| `content_type` | `0` any, `1` video, `2` image/text |

## Response and errors

Successful envelopes contain `success`, `request_id`, and `data`. Failed envelopes contain `success`, `request_id`, and `error` with a safe `code` and `message`. Preserve the request ID when reporting a failure.

Common errors include `INVALID_REQUEST`, `UPSTREAM_ERROR`, `NO_AVAILABLE_ACCOUNT`, `QR_SESSION_ACTIVE`, `QR_SESSION_NOT_FOUND`, `QR_VERIFICATION_REQUIRED`, `QR_LOGIN_FAILED`, and `INTERNAL_ERROR`.

The service never returns cookies or tickets from the account-list endpoint. Do not inspect or expose its credential storage to work around API errors.
