# -*- coding: utf-8 -*-
"""msToken"""

import os
import re
import time

import requests

from utils.strdata_pure import build_report_body

from utils.fingerprint import get_profile
from utils.http_util import get_douyin_http_timeout, get_douyin_tls_verify
_REPORT_URL = "https://mssdk.bytedance.com/web/common?ms_appid=6383"

_cache = {"token": "", "ts": 0}
_TTL = 600


def _get_ttwid(ttwid: str = None) -> str:
    # 显式传入空字符串时不回退 legacy 的 DY_COOKIES。
    if ttwid is not None:
        return ttwid
    m = re.search(r"ttwid=([^;]+)", os.getenv("DY_COOKIES") or "")
    return m.group(1) if m else ""


def get_mstoken(ttwid: str = None, proxies: dict = None, use_cache: bool = True) -> str:
    if use_cache and _cache["token"] and (time.time() - _cache["ts"] < _TTL):
        return _cache["token"]

    envelope = build_report_body()
    tw = _get_ttwid(ttwid)
    headers = {
        "user-agent": get_profile()["ua"], "accept": "*/*", "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "text/plain;charset=UTF-8",
        "origin": "https://www.douyin.com", "referer": "https://www.douyin.com/",
        "cookie": f"ttwid={tw}" if tw else "",
    }
    # 配置错误必须直接暴露，不能被网络降级逻辑吞掉。
    timeout = get_douyin_http_timeout()
    tls_verify = get_douyin_tls_verify()
    try:
        resp = requests.post(_REPORT_URL, data=envelope.encode("utf-8"), headers=headers,
                             verify=tls_verify, timeout=timeout, proxies=proxies)
        token = resp.headers.get("x-ms-token", "")
        if not token:
            m = re.search(r"msToken=([^;]+)", resp.headers.get("set-cookie", ""))
            token = m.group(1) if m else ""
        if token:
            _cache["token"] = token
            _cache["ts"] = time.time()
        return token
    except Exception:
        return ""
