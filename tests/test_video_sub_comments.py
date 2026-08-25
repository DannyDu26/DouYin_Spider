#!/usr/bin/env python3
# coding=utf-8
"""直接调用 DouyinAPI 测试抖音视频二级评论。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import requests


# 直接运行 tests 下的文件时，将项目根目录加入模块搜索路径。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from builder.auth import DouyinAuth  # noqa: E402
from builder.params import Params  # noqa: E402
from dy_apis.douyin_api import (  # noqa: E402
    DouyinAPI,
    DouyinAuthenticationError,
    parse_douyin_response,
)
from utils.common_util import load_dy_auth  # noqa: E402
from utils.fingerprint import get_profile  # noqa: E402
from utils.http_util import get_douyin_http_timeout, get_douyin_tls_verify  # noqa: E402


# 仅用于复现浏览器中已验证成功的原始请求，请勿提交到 Git。
FIXED_CURL_URL = "https://www.douyin.com/aweme/v1/web/comment/list/reply/?device_platform=webapp&aid=6383&channel=channel_pc_web&item_id=7659971343718337828&comment_id=7659976593570169652&cut_version=1&cursor=0&count=3&item_type=0&update_version_code=170400&pc_client_type=1&cpu_core_num=16&version_code=170400&version_name=17.4.0&cookie_enabled=true&screen_width=1920&screen_height=1200&browser_language=zh-CN&browser_platform=Win32&browser_name=Chrome&browser_version=151.0.0.0&browser_online=true&engine_name=Blink&engine_version=151.0.0.0&os_name=Windows&os_version=10&device_memory=32&platform=PC&downlink=10&effective_type=4g&round_trip_time=0&webid=7594308563884328502&verifyFp=verify_mqucih9o_AvgcbyiG_iDLr_4vl9_8e6F_wMsV75D6Rhk5&fp=verify_mqucih9o_AvgcbyiG_iDLr_4vl9_8e6F_wMsV75D6Rhk5&msToken=ACacVuoIVPE0RtFU0Kax38waR73DxD0orC-D39X5mTURuXVZqJhUtKoXgVrcRR3ookLCVEIpy9DOoiiKkOJfh2u2GgqytAQjwoKtLpvuCxE7HY48toqNPnCxzvSyeEiNTRuoqzDa0B6ftuUfzA0wKaT2kPFT7QkR5eMBDWhboGlYhqxoyELGD3CQ&a_bogus=O7Ufhq6JxNWcKdFbmcBByfxlp0dlNTuysBi%2FSFHTyPu-aXUOFRNv%2FaC5cxo-UFjXLYpzkC-H6DsAYnnb8GXzZoakFmZDSTvWdtIIn8sL2qqsGzkQgqRTCzhOSJealYvwm5K6JAfflUdOIf%2F1k3rhUBlyCKarsmtpsNPWdaWaYIzg6F49MNq2uObdYwFCQb95rD%3D%3D"
FIXED_CURL_HEADERS = {
    "Cookie": "passport_csrf_token=c188ddc32b5b83747d2a9eef7deee875; passport_csrf_token_default=c188ddc32b5b83747d2a9eef7deee875; enter_pc_once=1; UIFID_TEMP=a3682da019905bd2868511de77147b86e5069f1da12659d787063f1c7805c06f78e069c97692a37e4354ea22be2f31e735ce3fc1b7417cce56309e944d24b58658b615f3d53b32b1ee9ad564a39ed74a; x-web-secsdk-uid=e8fda510-a5e2-41ba-97d4-9d3d1baa43ad; s_v_web_id=verify_mssu4ze7_OOqLDwIO_1DQY_4VCs_ARe3_p4Tb8ORVYY87; is_support_rtm_web_ts=1; dy_swidth=1920; dy_sheight=1200; fpk1=U2FsdGVkX19C8lJWmKYii2v9YAZE3HEs+ukfeKTRBO2jfTW+EXz316YCJdAOuvHU+ByGO0igevEz62EZJH/9jw==; fpk2=6967ec7261b3cbe6a91d798c6b951c60; is_dash_user=1; bd_ticket_guard_client_web_domain=2; UIFID=a3682da019905bd2868511de77147b86e5069f1da12659d787063f1c7805c06f78e069c97692a37e4354ea22be2f31e737ec45a20ce3658fae40113169c52788bdf60b435dfd725a77e0cda7a51a69bfe715b2afb174b4b993762f5f4c28acbe825070d36462feb59a3a5261ec260cd063509ca446d459b99d4b1332b1b6b2969a3f74fea9ace96508ccd3a1553c9fc6ff2915e13a64a66300f4839d1200ded2; passport_assist_user=CjxK_8ot-3h0nDGujY9RQQO1I24KjT4M7kmw5bqbITDoPznmz9XxhyN7HG-pQX3WIagQyrL-pZq2aOMjvDYaSgo8AAAAAAAAAAAAAFDHCB-tZ0J4kFhBTVBgNx8T_izxel6o09x0yYFG9p5ba_dT1OcN-wmtKog1cXBmLOIbENHDmQ4Yia_WVCABIgEDghSOFw%3D%3D; n_mh=YHX9L2gXpZbYkZAcvKxEE7weo9B4NIyKY85d4Yz9Zb8; sid_guard=5d294f42ee5666d00001b855d7167768%7C1786705197%7C5184000%7CTue%2C+13-Oct-2026+10%3A59%3A57+GMT; uid_tt=a36dc7f4aba362d8cd22d6f497b236b7; uid_tt_ss=a36dc7f4aba362d8cd22d6f497b236b7; sid_tt=5d294f42ee5666d00001b855d7167768; sessionid=5d294f42ee5666d00001b855d7167768; sessionid_ss=5d294f42ee5666d00001b855d7167768; session_tlb_tag=sttt%7C6%7CXSlPQu5WZtAAAbhV1xZ3aP________-yNZAmdXOLb--VpbcZ34qxx8ulLe9a-Ps1PwLeGxRduYI%3D; is_staff_user=false; has_biz_token=false; sid_ucp_v1=1.0.0-KDA3NDZiNzlmZjE4ZTBkMjViZThiY2VkMmQ0NzBhMzNmZjBmMDk0ZWIKHwivkea74wIQrer70wYY7zEgDDDH-KfVBTgHQPQHSAQaAmhsIiA1ZDI5NGY0MmVlNTY2NmQwMDAwMWI4NTVkNzE2Nzc2OA; ssid_ucp_v1=1.0.0-KDA3NDZiNzlmZjE4ZTBkMjViZThiY2VkMmQ0NzBhMzNmZjBmMDk0ZWIKHwivkea74wIQrer70wYY7zEgDDDH-KfVBTgHQPQHSAQaAmhsIiA1ZDI5NGY0MmVlNTY2NmQwMDAwMWI4NTVkNzE2Nzc2OA; bd_ticket_guard_generate_ticket_time=2026-08-14/18:59:57; bd_ticket_guard_ts_sign_id=ts.2.8a785ff89ee5ecd; _bd_ticket_crypt_cookie=518677194801ab72d18054a7534b25ee; __security_mc_1_s_sdk_sign_data_key_web_protect=8b032f02-4b7b-b1df; __security_mc_1_s_sdk_cert_key=c33f35ee-440b-b062; __security_mc_1_s_sdk_crypt_sdk=fb2e4089-4414-855d; __security_server_data_status=1; login_time=1786705198581; publish_badge_show_info=%220%2C0%2C0%2C1786705200986%22; SelfTabRedDotControl=%5B%7B%22id%22%3A%227340228573682206755%22%2C%22u%22%3A799%2C%22c%22%3A0%7D%5D; volume_info=%7B%22isUserMute%22%3Afalse%2C%22isMute%22%3Atrue%2C%22volume%22%3A0.5%7D; strategyABtestKey=%221786950237.211%22; ttwid=1%7C-ZRfwM3BThqc5lzp0AxP1dlZoXSuu0aFBbXS-myZogs%7C1786950237%7Cde2c5fabfe1ae6cec9ab141505a1c232c53d534e15d294decd4d631b88378af9; __ac_nonce=06a82bc5b009c4648d929; __ac_signature=_02B4Z6wo00f014BhcKAAAIDAcdv1nUDlGdeAQXQAAIqj43; FOLLOW_LIVE_POINT_INFO=%22MS4wLjABAAAA32pPkx5NmKg4G4UcHQyctMlVouZlqnC9oZeqTyyFRFo%2F1786982400000%2F0%2F0%2F1786953396996%22; download_guide=%223%2F20260817%2F0%22; douyin.com; device_web_cpu_core=16; device_web_memory_size=32; architecture=amd64; stream_recommend_feed_params=%22%7B%5C%22cookie_enabled%5C%22%3Atrue%2C%5C%22screen_width%5C%22%3A1920%2C%5C%22screen_height%5C%22%3A1200%2C%5C%22browser_online%5C%22%3Atrue%2C%5C%22cpu_core_num%5C%22%3A16%2C%5C%22device_memory%5C%22%3A32%2C%5C%22downlink%5C%22%3A10%2C%5C%22effective_type%5C%22%3A%5C%224g%5C%22%2C%5C%22round_trip_time%5C%22%3A50%7D%22; gulu_source_res=eyJwX2luIjoiMDM2YjRkNTIzYzVhMWVjYTYyMDNmZDZlNDdkMzc1OTc4NmE5MmU3ZmQ1ZTI1NjA4YzQyYzEzM2Q4ODIwOTg4ZCJ9; sdk_source_info=7e276470716a68645a606960273f276364697660272927676c715a6d6069756077273f276364697660272927666d776a68605a607d71606b766c6a6b5a7666776c7571273f275e58272927666a6b766a69605a696c6061273f27636469766027292762696a6764695a7364776c6467696076273f275e582729277672715a646971273f2763646976602729277f6b5a666475273f2763646976602729276d6a6e5a6b6a716c273f2763646976602729276c6b6f5a7f6367273f27636469766027292771273f27343c3637363c36303c333d3234272927676c715a75776a716a666a69273f2763646976602778; bit_env=2wwV3PelM_-SloVl8_ENbanamdBZPDkCkwN3JDSvJhvTUDDGTEyfJgrI_HC0RzPTblCcqy06SAKGonhgrvPQ14PV-S67FkMZpHsbPCutdhrcEy13iBs60vc4Lg4BNRqIpL48fOQgSaAo84oWX1xb-cZQHRAtqCcALmR410JYndtq9rNMr3meGEP7qeqd4RwS1zvGzSATrHYJ7HlvL7_dOKv-gtwV78fpjZharBBDauJmnqjFxYBg8OnPGWOPd1ZxdFUjFLwvQBl3ZU6cxbzfHb_sX-HVkh8FOTP726eXsdrGS0X8Wt6N28vI3TlDVh1BsLjbrG_qp_MCt_nOSYGiwEPX68o0eYUjLgiJLLzfJKOGjGcxVYdSFIJF52fECqDVV-H0DYto5jH9w5svwDK6jgzl1PdUGO_qT2kxfMI2NmzcxZ9Oi7mHK-eUUZ51-CHmj0Sxo_wdiwluoyR_7ZIz7XREvrf9PP7Jj_HIPKnfdZkhvjxUSvxq71n4DRok2Q13; passport_auth_mix_state=7vf46cyihrd6heb0e3oa89re2t2i3j2o7pm8a1ibgcg9hzkd; IsDouyinActive=true; bd_ticket_guard_client_data=eyJiZC10aWNrZXQtZ3VhcmQtdmVyc2lvbiI6MiwiYmQtdGlja2V0LWd1YXJkLWl0ZXJhdGlvbi12ZXJzaW9uIjoxLCJiZC10aWNrZXQtZ3VhcmQtcmVlLXB1YmxpYy1rZXkiOiJCQWJreVFCdzJxN01xeEJSYUgvbGYxS3FQbncrZ1pabVo0VWZ1Q2hpcm1hZFd4SURsMDN2aStUVmNhcmVMTFQxZnhXd2JSMnJkMW1UVURlWnRFMTE5TEk9IiwiYmQtdGlja2V0LWd1YXJkLXdlYi12ZXJzaW9uIjoyfQ%3D%3D; FOLLOW_NUMBER_YELLOW_POINT_INFO=%22MS4wLjABAAAA32pPkx5NmKg4G4UcHQyctMlVouZlqnC9oZeqTyyFRFo%2F1786982400000%2F0%2F0%2F1786955282038%22; home_can_add_dy_2_desktop=%221%22; odin_tt=ed68985ac8e6ef066dbc97ba3b167803f21e606ecae736c8e22a66811dae4584df960068af140de2c8aef9ec16b4eec95ff9e00ee20830a1f53fe4b2e73840bb; biz_trace_id=4f9255b9; bd_ticket_guard_client_data_v2=eyJyZWVfcHVibGljX2tleSI6IkJBYmt5UUJ3MnE3TXF4QlJhSC9sZjFLcVBudytnWlptWjRVZnVDaGlybWFkV3hJRGwwM3ZpK1RWY2FyZUxMVDFmeFd3YlIycmQxbVRVRGVadEUxMTlMST0iLCJ0c19zaWduIjoidHMuMi44YTc4NWZmODllZTVlY2Q5NzA1NjhiY2JhMWE1YTdjZWNhMWI1YWVjY2I5MDIwZjRkNzk1YzRjODU5Yjk1NTMzYzRmYmU4N2QyMzE5Y2YwNTMxODYyNGNlZGExNDkxMWNhNDA2ZGVkYmViZWRkYjJlMzBmY2U4ZDRmYTAyNTc1ZCIsInJlcV9jb250ZW50Ijoic2VjX3RzIiwicmVxX3NpZ24iOiJhSjN5Ti9yRUhNM0ZSNkc4cDFEQW9WMnoyTjZPL1JtdHdwSExVSFdvZTc0PSIsInNlY190cyI6IiNIVGFwZGMyQ0taL0MwVTFtNWR4U1R4RUlQV3k1eHhNSjdsS0U3NGRlZHZrcUsyMnh0eFNrN04rT0x6OG4ifQ%3D%3D",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0",
    "referer": "https://www.douyin.com/video/7672959123557731619",
}


class ScriptError(RuntimeError):
    """表示可安全输出的测试错误。"""


def _numeric_id(value: str) -> str:
    """校验作品 ID 或评论 ID。"""
    if not value.isdigit() or len(value) > 32:
        raise argparse.ArgumentTypeError("必须是长度不超过 32 位的数字 ID")
    return value


def _count(value: str) -> int:
    """校验单页数量。"""
    number = int(value)
    if not 1 <= number <= 50:
        raise argparse.ArgumentTypeError("count 必须在 1～50 之间")
    return number


def _non_negative_int(value: str) -> int:
    """校验非负整数。"""
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return number


def _positive_int(value: str) -> int:
    """校验正整数。"""
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def _normalize_page(response: Any) -> tuple[list[dict[str, Any]], int, bool]:
    """校验底层返回值并提取分页字段。"""
    if not isinstance(response, dict):
        raise ScriptError("底层未返回 JSON 对象")

    comments = response.get("comments")
    if comments is None:
        comments = []
    if not isinstance(comments, list) or any(not isinstance(item, dict) for item in comments):
        raise ScriptError("底层返回的 comments 格式错误")

    cursor = response.get("cursor")
    if isinstance(cursor, int) and not isinstance(cursor, bool):
        next_cursor = cursor
    elif isinstance(cursor, str) and cursor.isdigit():
        next_cursor = int(cursor)
    else:
        raise ScriptError("底层返回的 cursor 格式错误") from None
    if next_cursor < 0:
        raise ScriptError("底层返回的 cursor 不能为负数")

    has_more = response.get("has_more")
    if not isinstance(has_more, int) or isinstance(has_more, bool) or has_more not in (0, 1):
        raise ScriptError("底层返回的 has_more 格式错误")
    return comments, next_cursor, has_more == 1


def fetch_sub_comments(
        auth: Any,
        video_id: str,
        comment_id: str,
        cursor: int,
        count: int,
        all_pages: bool,
        max_pages: int,
) -> dict[str, Any]:
    """直接调用底层方法，获取一页或多页二级评论。"""
    current_cursor = cursor
    pages: list[dict[str, Any]] = []
    all_comments: list[dict[str, Any]] = []
    comment = {"aweme_id": video_id, "cid": comment_id}

    while True:
        # 这里直接请求抖音底层，不经过 FastAPI 和 SpiderService。
        response = DouyinAPI.get_work_inner_comment(
            auth,
            comment,
            str(current_cursor),
            str(count),
        )
        comments, next_cursor, has_more = _normalize_page(response)
        pages.append(response)
        all_comments.extend(comments)

        if not all_pages:
            return response
        if not has_more:
            break
        if len(pages) >= max_pages:
            raise ScriptError(f"已达到最大页数 {max_pages}，测试已停止")
        if next_cursor <= current_cursor:
            raise ScriptError("底层返回 has_more=1，但分页游标没有前进")
        current_cursor = next_cursor

    return {
        "video_id": video_id,
        "comment_id": comment_id,
        "start_cursor": cursor,
        "next_cursor": next_cursor,
        "has_more": False,
        "page_count": len(pages),
        "total": len(all_comments),
        "comments": all_comments,
    }


def get_video_sub_comments(video_id: str | int, comment_id: str | int) -> dict[str, Any]:
    """仅提供作品 ID 和一级评论 ID，直接获取首屏二级评论。"""
    normalized_video_id = _numeric_id(str(video_id))
    normalized_comment_id = _numeric_id(str(comment_id))
    # 自动读取 .env 中的 DY_COOKIES 并构造底层认证对象。
    auth = load_dy_auth()
    return fetch_sub_comments(
        auth=auth,
        video_id=normalized_video_id,
        comment_id=normalized_comment_id,
        cursor=0,
        count=20,
        all_pages=False,
        max_pages=1,
    )


def request_with_fixed_curl() -> dict[str, Any]:
    """使用写死的 cURL URL、Cookie 和请求头复现浏览器请求。"""
    response = requests.get(
        FIXED_CURL_URL,
        headers=FIXED_CURL_HEADERS,
        verify=get_douyin_tls_verify(),
        timeout=get_douyin_http_timeout(),
    )
    return parse_douyin_response(response)


def get_video_sub_comments_with_fixed_headers(
        video_id: str | int,
        comment_id: str | int,
        cursor: int = 0,
        count: int = 3,
) -> dict[str, Any]:
    """写死 cURL 的 Cookie 和请求头，其余参数仍由当前代码生成。"""
    normalized_video_id = _numeric_id(str(video_id))
    normalized_comment_id = _numeric_id(str(comment_id))
    auth = DouyinAuth()
    auth.perepare_auth(FIXED_CURL_HEADERS["Cookie"])
    referer = f"https://www.douyin.com/video/{normalized_video_id}"
    profile = get_profile()

    # 参数构造顺序与 DouyinAPI.get_work_inner_comment 保持一致。
    params = Params()
    for key, value in (
        ("device_platform", "webapp"),
        ("aid", "6383"),
        ("channel", "channel_pc_web"),
        ("item_id", normalized_video_id),
        ("comment_id", normalized_comment_id),
        ("cut_version", "1"),
        ("cursor", str(cursor)),
        ("count", str(count)),
        ("item_type", "0"),
        ("update_version_code", "170400"),
        ("pc_client_type", "1"),
        ("version_code", "170400"),
        ("version_name", "17.4.0"),
        ("cookie_enabled", "true"),
        ("screen_width", profile["screen_width"]),
        ("screen_height", profile["screen_height"]),
        ("browser_language", "zh-CN"),
        ("browser_platform", profile["platform"]),
        ("browser_name", profile["browser_name"]),
        ("browser_version", profile["browser_version"]),
        ("browser_online", "true"),
        ("engine_name", "Blink"),
        ("engine_version", profile["engine_version"]),
        ("os_name", "Windows"),
        ("os_version", profile["os_version"]),
        ("cpu_core_num", profile["cpu_core_num"]),
        ("device_memory", profile["device_memory"]),
        ("platform", "PC"),
        ("downlink", "10"),
        ("effective_type", "4g"),
        ("round_trip_time", "0"),
    ):
        params.add_param(key, value)
    params.with_web_id(auth, referer)
    params.add_param("verifyFp", auth.cookie["s_v_web_id"])
    params.add_param("fp", auth.cookie["s_v_web_id"])
    params.add_param("msToken", auth.msToken)
    params.with_a_bogus()

    headers = dict(FIXED_CURL_HEADERS)
    headers["referer"] = referer
    response = requests.get(
        f"{DouyinAPI.douyin_url}/aweme/v1/web/comment/list/reply/",
        headers=headers,
        params=params.get(),
        verify=get_douyin_tls_verify(),
        timeout=get_douyin_http_timeout(),
    )
    if not response.content:
        raise ScriptError("固定 Cookie 和请求头后，上游仍返回 200 空响应")
    return parse_douyin_response(response)


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="直接调用 DouyinAPI.get_work_inner_comment 测试二级评论。",
    )
    parser.add_argument("--video-id", type=_numeric_id, help="抖音作品 ID")
    parser.add_argument("--comment-id", type=_numeric_id, help="一级评论 ID")
    parser.add_argument("--cursor", type=_non_negative_int, default=0, help="起始游标，默认 0")
    parser.add_argument("--count", type=_count, default=20, help="每页数量，默认 20")
    parser.add_argument("--all-pages", action="store_true", help="持续请求直到没有下一页")
    fixed_group = parser.add_mutually_exclusive_group()
    fixed_group.add_argument(
        "--fixed-curl",
        action="store_true",
        help="使用测试文件中写死的完整 cURL 请求，忽略 ID 和分页参数",
    )
    fixed_group.add_argument(
        "--fixed-headers",
        action="store_true",
        help="写死 cURL 的 Cookie 和请求头，其他参数仍由代码动态生成",
    )
    parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=100,
        help="多页模式最大页数，默认 100",
    )
    return parser


def main() -> int:
    """加载本地登录态并运行底层测试。"""
    parser = build_parser()
    args = parser.parse_args()
    if not args.fixed_curl and (args.video_id is None or args.comment_id is None):
        parser.error("普通模式或 --fixed-headers 必须同时提供 --video-id 和 --comment-id")
    try:
        if args.fixed_curl:
            result = request_with_fixed_curl()
        elif args.fixed_headers:
            result = get_video_sub_comments_with_fixed_headers(
                args.video_id,
                args.comment_id,
                args.cursor,
                args.count,
            )
        else:
            # 从项目 .env 的 DY_COOKIES 等配置构造 DouyinAuth。
            auth = load_dy_auth()
            result = fetch_sub_comments(
                auth,
                args.video_id,
                args.comment_id,
                args.cursor,
                args.count,
                args.all_pages,
                args.max_pages,
            )
    except DouyinAuthenticationError:
        print("测试失败：抖音登录态已失效，请更新 DY_COOKIES", file=sys.stderr)
        return 1
    except ScriptError as error:
        print(f"测试失败：{error}", file=sys.stderr)
        return 1
    except RuntimeError as error:
        # load_dy_auth 的配置错误可安全展示，网络异常则不回显签名 URL。
        if str(error).startswith("缺少 DY_COOKIES"):
            print(f"测试失败：{error}", file=sys.stderr)
        else:
            print(f"测试失败：底层调用异常（{error.__class__.__name__}）", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"测试失败：底层调用异常（{error.__class__.__name__}）", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    comments = result.get("comments") or []
    print(f"测试成功：获取 {len(comments)} 条二级评论", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
