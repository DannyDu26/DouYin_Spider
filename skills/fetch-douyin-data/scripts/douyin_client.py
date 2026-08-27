#!/usr/bin/env python3
# coding=utf-8
"""调用项目抖音 HTTP API，并管理扫码登录会话。"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://crawler.xoyo.com/api/"
TERMINAL_QR_STATES = {"succeeded", "expired", "failed", "cancelled"}
ACTION_REQUIRED_QR_STATES = {"verification_required", "waiting_sms_code"}


class ClientError(RuntimeError):
    """可安全展示的客户端错误。"""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


def _validate_base_url(value: str) -> str:
    """限制服务地址格式，拒绝隐式认证信息。"""
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise argparse.ArgumentTypeError("base URL 必须是有效的 http/https 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("base URL 不能包含认证信息、查询参数或片段")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}/"


class DouyinClient:
    """项目 FastAPI 服务的轻量客户端。"""

    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json", "User-Agent": "fetch-douyin-data-skill/1.0"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        # base_url 已包含 /api/，接口路径统一使用相对路径，避免重复前缀。
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as error:
            raw = error.read()
            parsed = _decode_json(raw)
            safe_error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            raise ClientError(
                safe_error.get("message") or f"服务返回 HTTP {error.code}",
                details={
                    "http_status": error.code,
                    "code": safe_error.get("code", "HTTP_ERROR"),
                    "request_id": parsed.get("request_id") if isinstance(parsed, dict) else None,
                    "details": safe_error.get("details"),
                },
            ) from None
        except (URLError, TimeoutError, OSError) as error:
            raise ClientError(
                "无法连接抖音 API 服务",
                details={"code": "CONNECTION_ERROR", "reason": error.__class__.__name__},
            ) from None

        parsed = _decode_json(raw)
        if not isinstance(parsed, dict):
            raise ClientError("服务返回了非 JSON 对象", details={"code": "INVALID_RESPONSE"})
        if parsed.get("success") is False:
            safe_error = parsed.get("error") or {}
            raise ClientError(
                safe_error.get("message") or "API 调用失败",
                details={
                    "code": safe_error.get("code", "API_ERROR"),
                    "request_id": parsed.get("request_id"),
                    "details": safe_error.get("details"),
                },
            )
        return parsed


def _decode_json(raw: bytes) -> Any:
    """解析 UTF-8 JSON，不回显无法解析的原文。"""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _save_qrcode(data_url: str, output: str | None, session_id: str, force: bool) -> Path:
    """校验并保存服务返回的 PNG 二维码。"""
    match = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=]+)", data_url)
    if not match:
        raise ClientError("服务未返回有效的 PNG 二维码", details={"code": "INVALID_QR_CODE"})
    try:
        png = base64.b64decode(match.group(1), validate=True)
    except (ValueError, binascii.Error) as error:
        raise ClientError("二维码 Base64 数据无效", details={"code": "INVALID_QR_CODE"}) from error
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ClientError("二维码内容不是 PNG 图片", details={"code": "INVALID_QR_CODE"})

    path = Path(output or f"douyin-qr-{session_id}.png").expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "wb" if force else "xb"
        with path.open(mode) as file:
            file.write(png)
    except FileExistsError:
        raise ClientError(
            "二维码输出文件已存在；请更换路径或使用 --force",
            details={"code": "OUTPUT_EXISTS", "qrcode_path": str(path)},
        ) from None
    except OSError as error:
        raise ClientError(
            "无法保存二维码文件",
            details={"code": "OUTPUT_ERROR", "reason": error.__class__.__name__},
        ) from None
    return path


def _data(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise ClientError("响应缺少 data 对象", details={"code": "INVALID_RESPONSE"})
    return data


def _login_start(client: DouyinClient, args: argparse.Namespace) -> dict[str, Any]:
    response = client.request("POST", "v1/douyin/auth/qr-sessions", {"account_id": args.account_id})
    data = _data(response)
    session_id = str(data.get("session_id") or "")
    data_url = data.pop("qrcode_data_url", None)
    if not session_id or not isinstance(data_url, str):
        raise ClientError("扫码会话响应不完整", details={"code": "INVALID_RESPONSE"})
    qrcode_path = _save_qrcode(data_url, args.output, session_id, args.force)
    return {
        "success": True,
        "request_id": response.get("request_id"),
        "data": {**data, "qrcode_path": str(qrcode_path)},
    }


def _login_status(client: DouyinClient, session_id: str) -> dict[str, Any]:
    encoded_id = quote(session_id, safe="")
    return client.request("GET", f"v1/douyin/auth/qr-sessions/{encoded_id}")


def _login_wait(client: DouyinClient, args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + args.wait_timeout
    while True:
        response = _login_status(client, args.session_id)
        data = _data(response)
        if data.get("status") in TERMINAL_QR_STATES | ACTION_REQUIRED_QR_STATES:
            return response
        if time.monotonic() >= deadline:
            raise ClientError(
                "等待扫码结果超时；会话可能仍然有效",
                details={"code": "LOCAL_WAIT_TIMEOUT", "session_id": args.session_id},
            )
        time.sleep(args.interval)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="调用 DouYin_Spider HTTP API")
    parser.add_argument(
        "--base-url",
        type=_validate_base_url,
        default=os.getenv("DOUYIN_API_BASE_URL", DEFAULT_BASE_URL),
        help="服务根地址，默认读取 DOUYIN_API_BASE_URL",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="单次 HTTP 请求超时秒数")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="检查服务健康状态")
    subparsers.add_parser("accounts", help="列出账号安全状态")

    login_start = subparsers.add_parser("login-start", help="创建扫码登录会话并保存二维码")
    login_start.add_argument("--account-id", required=True)
    login_start.add_argument("--output", help="二维码 PNG 输出路径")
    login_start.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")

    login_status = subparsers.add_parser("login-status", help="查询扫码登录状态")
    login_status.add_argument("--session-id", required=True)

    login_sms_request = subparsers.add_parser("login-sms-request", help="请求身份验证短信验证码")
    login_sms_request.add_argument("--session-id", required=True)

    login_sms_verify = subparsers.add_parser("login-sms-verify", help="安全输入并提交短信验证码")
    login_sms_verify.add_argument("--session-id", required=True)

    login_wait = subparsers.add_parser("login-wait", help="等待扫码登录结束")
    login_wait.add_argument("--session-id", required=True)
    login_wait.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数")
    # 二维码通常约一分钟失效，默认等待略留登录提交时间。
    login_wait.add_argument("--wait-timeout", type=float, default=70.0, help="本地等待超时秒数")

    login_cancel = subparsers.add_parser("login-cancel", help="取消扫码登录会话")
    login_cancel.add_argument("--session-id", required=True)

    video_info = subparsers.add_parser("video-info", help="获取作品详情")
    # 批量链接和单个作品 ID 只能选择一种定位方式。
    video_info_locator = video_info.add_mutually_exclusive_group(required=True)
    video_info_locator.add_argument("--url", action="append", dest="urls")
    video_info_locator.add_argument("--video-id")

    comments = subparsers.add_parser("video-comments", help="获取视频一级评论")
    # 链接和作品 ID 只能选择一种定位方式。
    comments_locator = comments.add_mutually_exclusive_group(required=True)
    comments_locator.add_argument("--url")
    comments_locator.add_argument("--video-id")
    comments.add_argument("--cursor", type=int, default=0)
    comments.add_argument("--count", type=int, default=20)

    sub_comments = subparsers.add_parser("video-sub-comments", help="获取视频二级评论")
    sub_comments.add_argument("--video-id", required=True)
    sub_comments.add_argument("--comment-id", required=True)
    sub_comments.add_argument("--cursor", type=int, default=0)
    sub_comments.add_argument("--count", type=int, default=20)

    user_videos = subparsers.add_parser("user-videos", help="获取用户作品")
    user_locator = user_videos.add_mutually_exclusive_group(required=True)
    user_locator.add_argument("--user-url")
    user_locator.add_argument("--user-id")
    user_videos.add_argument("--page-num", type=int, default=1)

    search = subparsers.add_parser("search-videos", help="搜索作品")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=25)
    search.add_argument("--sort-type", choices=["0", "1", "2"], default="0")
    search.add_argument("--publish-time", choices=["0", "1", "7", "180"], default="0")
    search.add_argument("--filter-duration", choices=["", "0-1", "1-5", "5-10000"], default="")
    search.add_argument("--search-range", choices=["0", "1", "2", "3"], default="0")
    search.add_argument("--content-type", choices=["0", "1", "2"], default="0")
    return parser


def _run(client: DouyinClient, args: argparse.Namespace) -> dict[str, Any]:
    """把受支持的命令映射到固定 API。"""
    if args.command == "health":
        return client.request("GET", "health")
    if args.command == "accounts":
        return client.request("GET", "v1/douyin/auth/accounts")
    if args.command == "login-start":
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.account_id):
            raise ClientError("account_id 格式无效", details={"code": "INVALID_ACCOUNT_ID"})
        return _login_start(client, args)
    if args.command == "login-status":
        return _login_status(client, args.session_id)
    if args.command == "login-sms-request":
        encoded_id = quote(args.session_id, safe="")
        return client.request(
            "POST",
            f"v1/douyin/auth/qr-sessions/{encoded_id}/sms/request",
        )
    if args.command == "login-sms-verify":
        # 使用隐藏输入，避免验证码进入命令历史和正常输出。
        code = getpass.getpass("请输入短信验证码：").strip()
        if not re.fullmatch(r"\d{4,8}", code):
            raise ClientError(
                "短信验证码必须是 4～8 位数字",
                details={"code": "INVALID_SMS_CODE"},
            )
        encoded_id = quote(args.session_id, safe="")
        return client.request(
            "POST",
            f"v1/douyin/auth/qr-sessions/{encoded_id}/sms/verify",
            {"code": code},
        )
    if args.command == "login-wait":
        if args.interval <= 0 or args.wait_timeout <= 0:
            raise ClientError("轮询时间参数必须大于 0", details={"code": "INVALID_ARGUMENT"})
        return _login_wait(client, args)
    if args.command == "login-cancel":
        encoded_id = quote(args.session_id, safe="")
        return client.request("DELETE", f"v1/douyin/auth/qr-sessions/{encoded_id}")
    if args.command == "video-info":
        payload = {"urls": args.urls} if args.urls else {"video_id": args.video_id}
        return client.request("POST", "v1/douyin/video_info", payload)
    if args.command == "video-comments":
        locator = {"url": args.url} if args.url else {"video_id": args.video_id}
        payload = {**locator, "cursor": args.cursor, "count": args.count}
        return client.request("POST", "v1/douyin/video_comments", payload)
    if args.command == "video-sub-comments":
        payload = {
            "video_id": args.video_id,
            "comment_id": args.comment_id,
            "cursor": args.cursor,
            "count": args.count,
        }
        return client.request("POST", "v1/douyin/video_sub_comments", payload)
    if args.command == "user-videos":
        # 用户 ID 可直接定位主页，无需先查询任一作品。
        locator = {"user_url": args.user_url} if args.user_url else {"user_id": args.user_id}
        payload = {**locator, "page_num": args.page_num}
        return client.request("POST", "v1/douyin/user_videos", payload)
    if args.command == "search-videos":
        payload = {
            "query": args.query,
            "limit": args.limit,
            "sort_type": args.sort_type,
            "publish_time": args.publish_time,
            "filter_duration": args.filter_duration,
            "search_range": args.search_range,
            "content_type": args.content_type,
        }
        return client.request("POST", "v1/douyin/search_videos", payload)
    raise ClientError("不支持的命令", details={"code": "UNKNOWN_COMMAND"})


def main() -> int:
    # 统一工具输出编码，便于 Codex 和跨平台终端读取。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = _build_parser()
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    client = DouyinClient(args.base_url, args.timeout)
    try:
        result = _run(client, args)
    except ClientError as error:
        print(json.dumps({"success": False, "error": {"message": str(error), **error.details}}, ensure_ascii=False, indent=2))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"success": False, "error": {"code": "INTERRUPTED", "message": "操作已中断"}}, ensure_ascii=False, indent=2))
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
