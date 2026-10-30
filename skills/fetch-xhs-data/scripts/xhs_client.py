#!/usr/bin/env python3
# coding=utf-8
"""通过项目 HTTP API 完成小红书扫码和短信登录，仅依赖标准库。"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import warnings


DEFAULT_BASE_URL = "https://crawler.xoyo.com/api/"
AUTH_PATH = "v1/xhs/auth"


class ClientError(RuntimeError):
    """只包含可展示信息的客户端错误。"""

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details


def _validate_base_url(value: str) -> str:
    """规范根地址，拒绝 URL 内嵌凭据。"""
    try:
        parsed = urlparse(value.strip())
        port = parsed.port
    except ValueError:
        raise argparse.ArgumentTypeError("base URL 格式无效") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise argparse.ArgumentTypeError("base URL 必须是有效的 http/https 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("base URL 不能包含认证信息、查询参数或片段")
    if any(char.isspace() or ord(char) < 32 for char in value) or port == 0:
        raise argparse.ArgumentTypeError("base URL 格式无效")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("超时必须是大于 0 的有限秒数")
    return seconds


def _decode_json(raw: bytes) -> dict[str, Any]:
    """拒绝非对象 JSON，避免回显代理错误页或原始响应。"""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    if not isinstance(parsed, dict):
        raise ClientError("服务返回了无效的 JSON 对象", code="INVALID_RESPONSE")
    return parsed


class XhsClient:
    """统一处理 HTTP、服务错误和请求超时。"""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = _validate_base_url(base_url)
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict | None = None,
                *, timeout: float | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": "fetch-xhs-data-skill/1.0"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self.base_url + path.lstrip("/"), data=body, headers=headers, method=method)
        http_status = None
        try:
            with urlopen(request, timeout=self.timeout if timeout is None else timeout) as response:
                raw = response.read()
        except HTTPError as error:
            http_status = error.code
            with error:
                raw = error.read()
        except (URLError, TimeoutError, OSError) as error:
            timed_out = isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
            raise ClientError(
                "请求超时；服务端登录任务可能仍在继续" if timed_out else "无法连接小红书 API 服务",
                code="REQUEST_TIMEOUT" if timed_out else "CONNECTION_ERROR",
            ) from None
        try:
            parsed = _decode_json(raw)
        except ClientError:
            if http_status is not None:
                raise ClientError(f"服务返回 HTTP {http_status}", code="HTTP_ERROR", http_status=http_status) from None
            raise
        if http_status is not None or parsed.get("success") is False:
            error = parsed.get("error")
            error = error if isinstance(error, dict) else {}
            raise ClientError(
                error.get("message") or "API 调用失败",
                code=error.get("code", "HTTP_ERROR" if http_status else "API_ERROR"),
                http_status=http_status, request_id=parsed.get("request_id"), details=error.get("details"),
            )
        return parsed


def _login_data(response: dict, expected_status: str) -> dict:
    """校验步骤结果，不能把 HTTP 200 直接当成登录完成。"""
    data = response.get("data")
    if (not isinstance(data, dict) or not isinstance(data.get("session_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", data["session_id"])
            or data.get("status") != expected_status):
        raise ClientError("登录响应缺少会话或状态不符合预期", code="INVALID_RESPONSE",
                          request_id=response.get("request_id"))
    return data


def _save_qrcode(data_url: str, output: str | None, session_id: str, force: bool) -> Path:
    """仅将合法 PNG 写入本地，默认放临时目录以免入库。"""
    match = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=]+)", data_url)
    try:
        png = base64.b64decode(match.group(1), validate=True) if match else b""
    except (ValueError, binascii.Error):
        png = b""
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ClientError("服务未返回有效的 PNG 二维码", code="INVALID_QR_CODE")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
        raise ClientError("服务返回的会话 ID 格式无效", code="INVALID_RESPONSE")
    path = Path(output or Path(tempfile.gettempdir()) / f"xhs-qr-{session_id}.png").expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb" if force else "xb") as file:
            file.write(png)
    except FileExistsError:
        raise ClientError("二维码文件已存在；请更换路径或使用 --force", code="OUTPUT_EXISTS",
                          qrcode_path=str(path)) from None
    except OSError:
        raise ClientError("无法保存二维码文件", code="OUTPUT_ERROR") from None
    return path


def _hidden_input(prompt: str, pattern: str, message: str) -> str:
    """无隐藏输入能力时停止，防止 getpass 退化为明文回显。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass(prompt).strip()
    except (getpass.GetPassWarning, EOFError, OSError):
        raise ClientError("请在支持隐藏输入的交互终端中执行此命令", code="INTERACTIVE_INPUT_REQUIRED") from None
    if not re.fullmatch(pattern, value):
        raise ClientError(message, code="INVALID_ARGUMENT")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过项目 API 登录小红书（扫码或短信）")
    parser.add_argument("--base-url", type=_validate_base_url,
                        default=os.getenv("XHS_API_BASE_URL", DEFAULT_BASE_URL), help="API 根地址，或设置 XHS_API_BASE_URL")
    parser.add_argument("--timeout", type=_positive_seconds, default=30.0, help="普通 HTTP 请求超时秒数")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="检查服务健康状态")
    commands.add_parser("accounts", help="列出小红书账号安全状态")
    start = commands.add_parser("login-start", help="创建扫码会话并保存二维码 PNG")
    start.add_argument("--account-id", required=True)
    start.add_argument("--output", help="二维码 PNG 输出路径，默认临时目录")
    start.add_argument("--force", action="store_true", help="覆盖已有二维码文件")
    wait = commands.add_parser("login-wait", help="单次请求等待扫码、手机确认及账号入池")
    wait.add_argument("--session-id", required=True)
    wait.add_argument("--wait-timeout", type=_positive_seconds, default=240.0, help="等待请求超时秒数，默认 240")
    sms_send = commands.add_parser("login-sms-send", help="隐藏输入手机号并发送验证码，或重发")
    sms_send.add_argument("--account-id", required=True)
    sms_send.add_argument("--zone", default="86", help="国家/地区区号，默认 86")
    sms_verify = commands.add_parser("login-sms-verify", help="隐藏输入短信验证码并登录")
    sms_verify.add_argument("--session-id", required=True)
    return parser


def _run(client: XhsClient, args: argparse.Namespace) -> dict:
    """将命令映射到实际存在的接口，两种登录共享响应校验。"""
    if hasattr(args, "account_id") and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.account_id):
        raise ClientError("account_id 必须是 1～64 位小写字母、数字、下划线或连字符，首位为字母或数字",
                          code="INVALID_ACCOUNT_ID")
    if args.command == "health":
        return client.request("GET", "health")
    if args.command == "accounts":
        return client.request("GET", f"{AUTH_PATH}/accounts")
    if args.command == "login-start":
        response = client.request("POST", f"{AUTH_PATH}/qr/create", {"account_id": args.account_id})
        data = _login_data(response, "waiting_scan")
        data_url = data.pop("qrcode_data_url", None)
        try:
            if not isinstance(data_url, str):
                raise ClientError("扫码会话缺少二维码", code="INVALID_QR_CODE")
            path = _save_qrcode(data_url, args.output, data["session_id"], args.force)
        except ClientError as error:
            error.details.update(session_id=data["session_id"], request_id=response.get("request_id"))
            raise
        return {**response, "data": {**data, "qrcode_path": str(path)}}
    if args.command == "login-wait":
        try:
            # 小红书由服务端等待扫码；重试同一会话可接回后台任务。
            response = client.request("POST", f"{AUTH_PATH}/qr/login", {"session_id": args.session_id},
                                      timeout=args.wait_timeout)
        except ClientError as error:
            error.details["session_id"] = args.session_id
            raise
        _login_data(response, "succeeded")
        return response
    if args.command == "login-sms-send":
        if not re.fullmatch(r"\d{1,4}", args.zone):
            raise ClientError("区号必须是 1～4 位数字", code="INVALID_ARGUMENT")
        phone = _hidden_input("请输入手机号（隐藏输入）：", r"\d{5,15}", "手机号必须是 5～15 位数字")
        response = client.request("POST", f"{AUTH_PATH}/sms/send",
                                  {"account_id": args.account_id, "phone": phone, "zone": args.zone})
        _login_data(response, "waiting_sms_code")
        return response
    if args.command == "login-sms-verify":
        code = _hidden_input("请输入短信验证码：", r"\d{4,8}", "短信验证码必须是 4～8 位数字")
        response = client.request("POST", f"{AUTH_PATH}/sms/login", {"session_id": args.session_id, "code": code})
        _login_data(response, "succeeded")
        return response
    raise ClientError("不支持的命令", code="UNKNOWN_COMMAND")


def main() -> int:
    # 输出固定为 UTF-8，避免 PowerShell 和 conda 吞掉中文提示。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _build_parser().parse_args()
    try:
        result = _run(XhsClient(args.base_url, args.timeout), args)
    except ClientError as error:
        result = {"success": False, "error": {"message": str(error), **error.details}}
        exit_code = 1
    except KeyboardInterrupt:
        result = {"success": False, "error": {"code": "INTERRUPTED", "message": "本地等待已中断；服务端会话可能仍在继续"}}
        exit_code = 130
    else:
        exit_code = 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
