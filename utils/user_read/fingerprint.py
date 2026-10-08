# -*- coding: utf-8 -*-
"""浏览器指纹档案。

必须与 `.env` 里 cookie 所属的那台浏览器一致：cookie 里的
`stream_recommend_feed_params` 已经写死了屏幕尺寸 / CPU 核数 / 内存，
query 里再报另一套值就会自相矛盾。默认值取自 2026-08-16 的 Chrome 实录抓包，
换设备时用环境变量覆盖即可（见 DY_FP_* ）。
"""

import os


# 2026-08-16 Chrome 实录：create_v2 抓包所用设备
_DEFAULTS = {
    "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
    "browser_version": "151.0.0.0",
    "engine_version": "151.0.0.0",
    "screen_width": "2560",
    "screen_height": "1440",
    "cpu_core_num": "20",
    "device_memory": "32",
    "webgl_vendor": "Google Inc. (NVIDIA)",
    # 2026-08-22 更正：原值写的是 RTX 4070，但本机实际是 RTX 5060 Ti
    # （浏览器实录的 account_sdk_source_info 里就是这串）。这条必须与
    # DY_DTRAIT_BLOB 里的 webGL 特征同源——那个 blob 是从这台机器抓的，
    # 显卡型号对不上等于自曝。
    "webgl_renderer": ("ANGLE (NVIDIA, NVIDIA GeForce RTX 5060 Ti (0x00002D04) "
                       "Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    # 浏览器实录的 accept-language，与 browser_language=zh-CN 配套
    "accept_language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7,ja;q=0.6",
}

_profile = None
def _env(key, default):
    return os.getenv("DY_FP_" + key.upper()) or default


def _int_env(key, default):
    try:
        return int(_env(key, str(default)))
    except (TypeError, ValueError):
        return int(default)


def get_profile():
    """进程级指纹档案（UA/几何/硬件统一，进程内稳定）。"""
    global _profile
    if _profile is None:
        ua = _env("ua", _DEFAULTS["ua"])
        major = _env("browser_version", _DEFAULTS["browser_version"]).split(".")[0]
        _profile = {
            "ua": ua,
            # Chrome 151 truth from the live login capture / user's curl.
            # Keep the brand order and GREASE token exactly as emitted by the
            # browser; this header is present on both passport and mssdk XHRs.
            "sec_ch_ua": (f'"Not=A?Brand";v="99", "Google Chrome";v="{major}", '
                          f'"Chromium";v="{major}"'),
            "sec_ch_ua_platform": '"Windows"',
            "browser_name": "Chrome",
            "browser_version": _env("browser_version", _DEFAULTS["browser_version"]),
            "engine_name": "Blink",
            "engine_version": _env("engine_version", _DEFAULTS["engine_version"]),
            "os_name": "Windows",
            "os_version": "10",
            "platform": "Win32",
            "cpu_core_num": _env("cpu_core_num", _DEFAULTS["cpu_core_num"]),
            "device_memory": _env("device_memory", _DEFAULTS["device_memory"]),
            "webgl_vendor": _env("webgl_vendor", _DEFAULTS["webgl_vendor"]),
            "webgl_renderer": _env("webgl_renderer", _DEFAULTS["webgl_renderer"]),
            "screen_width": _env("screen_width", _DEFAULTS["screen_width"]),
            "screen_height": _env("screen_height", _DEFAULTS["screen_height"]),
            "screen_x": _int_env("screen_x", 0),
            "screen_y": _int_env("screen_y", 0),
            "accept_language": _env("accept_language", _DEFAULTS["accept_language"]),
        }
        w, h = int(_profile["screen_width"]), int(_profile["screen_height"])
        # a_bogus 里拼成 "w|innerH|w|outerH|w|availH|w|h|Win32"。
        # 偏移取自 2026-08-16 页面 SDK 真值（2560x1440 → 1215/1392/1392）：
        # 任务栏占 48px，窗口最大化时 outerHeight == availHeight。
        # Current Chrome truth (isolated parity context):
        # inner 2560x1215, outer 2560x1392, avail 2560x1392, screen 2560x1440.
        # Keep screen dimensions separate and allow per-machine overrides.
        inner_w = _int_env("inner_width", w)
        inner_h = _int_env("inner_height", h - 225)
        outer_w = _int_env("outer_width", w)
        outer_h = _int_env("outer_height", h - 48)
        avail_w = _int_env("avail_width", w)
        avail_h = _int_env("avail_height", h - 48)
        _profile["geo"] = (inner_w, inner_h, outer_w, outer_h,
                            avail_w, avail_h, w, h)
    return _profile
