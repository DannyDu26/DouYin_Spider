"""用户资料、作品及设备号的请求参数，与成功项目保持相同顺序。"""
from urllib.parse import urlsplit
from .params import Params
from .fingerprint import get_profile
from .headers import HeaderBuilder, HeaderType

class DouyinAPI:
    douyin_url = 'https://www.douyin.com'

    @staticmethod
    def get_user_work_info(auth, user_url: str, max_cursor, **kwargs):
        """
        获取用户作品信息.
        :param auth: DouyinAuth object.
        :param user_url:  用户主页URL.
        :param max_cursor:  上一次请求的max_cursor.
        :return:
        """
        api = f"/aweme/v1/web/aweme/post/"
        user_id = urlsplit(user_url).path.rstrip("/").rsplit("/", 1)[-1]
        headers = HeaderBuilder().build(HeaderType.GET)
        headers.set_referer(user_url)
        headers.with_uifid(auth)
        # 字段与顺序照 2026-08-16 抓包（46 项）。注意几处只能看抓包才知道的细节：
        #   - version_code 是 290100 / 29.1.0，**这个接口特有**，不是主站通用的 170400
        #   - whale_cut_token 是**空值字段**，浏览器确实发（`whale_cut_token=`）。
        #     早先误判成"浏览器不发"并删掉了，根因是 parse_qsl 默认 keep_blank_values=False
        #     会把空值字段整个丢掉，对账脚本因此看不见它 —— 比对 query 一定要开 keep_blank_values
        #   - verifyFp / fp 在 **a_bogus 之后**（aweme/detail 那边却在之前，逐接口不同）
        params = Params()
        params.add_param("device_platform", 'webapp')
        params.add_param("aid", '6383')
        params.add_param("channel", 'channel_pc_web')
        params.add_param("sec_user_id", user_id)
        params.add_param("max_cursor", max_cursor)
        params.add_param("locate_query", 'false')
        params.add_param("show_live_replay_strategy", '1')
        params.add_param("need_time_list", '1' if max_cursor == '0' else '0')
        params.add_param("time_list_query", '0')
        params.add_param("whale_cut_token", '')
        params.add_param("cut_version", '1')
        params.add_param("count", '18')
        params.add_param("publish_video_strategy_type", '2')
        # 实录：自己主页发 0、他人主页发 1。写死 0 去爬别人的作品与浏览器不一致，
        # 这里按 sec_user_id 是否是登录者本人来取（拿不到自己的 sec_uid 就按他人算）。
        _own = getattr(auth, 'sec_uid', None) or getattr(auth, '_sec_uid', None)
        params.add_param("from_user_page", '0' if (_own and _own == user_id) else '1')
        params.add_param("update_version_code", '170400')
        params.add_param("pc_client_type", '1')
        params.add_param("pc_libra_divert", 'Windows')
        params.add_param("support_h265", '1')
        params.add_param("support_dash", '1')
        params.add_param("cpu_core_num", get_profile()["cpu_core_num"])
        params.add_param("version_code", '290100')
        params.add_param("version_name", '29.1.0')
        params.add_param("cookie_enabled", 'true')
        params.add_param("screen_width", get_profile()["screen_width"])
        params.add_param("screen_height", get_profile()["screen_height"])
        params.add_param("browser_language", 'zh-CN')
        params.add_param("browser_platform", 'Win32')
        params.add_param("browser_name", get_profile()["browser_name"])
        params.add_param("browser_version", get_profile()["browser_version"])
        params.add_param("browser_online", 'true')
        params.add_param("engine_name", 'Blink')
        params.add_param("engine_version", get_profile()["engine_version"])
        params.add_param("os_name", 'Windows')
        params.add_param("os_version", '10')
        params.add_param("device_memory", get_profile()["device_memory"])
        params.add_param("platform", 'PC')
        params.add_param("downlink", '10')
        params.add_param("effective_type", '4g')
        params.add_param("round_trip_time", '0')
        params.with_web_id(auth, user_url)
        params.with_uifid(auth)
        params.add_param("msToken", auth.msToken)
        params.with_a_bogus()
        params.with_verify_fp(auth)
        # 这个接口在 secsdk 的 webSign 策略表里，末尾还要带 timestamp + 签名
        return auth.request(params.signed_url(f'{DouyinAPI.douyin_url}{api}', auth), headers.get())


    @staticmethod
    def get_user_info(auth, user_url: str, **kwargs):
        """
        获取用户信息.
        :param auth: DouyinAuth object.
        :param user_url: 用户主页URL.
        :return: 用户信息.
        """
        api = f"/aweme/v1/web/user/profile/other/"
        user_id = urlsplit(user_url).path.rstrip("/").rsplit("/", 1)[-1]
        headers = HeaderBuilder().build(HeaderType.GET)
        headers.set_referer(user_url)
        headers.with_uifid(auth)
        params = Params()
        params.add_param("device_platform", 'webapp')
        params.add_param("aid", '6383')
        params.add_param("channel", 'channel_pc_web')
        params.add_param("publish_video_strategy_type", '2')
        params.add_param("source", 'channel_pc_web')
        params.add_param("sec_user_id", user_id)
        params.add_param("personal_center_strategy", '1')
        # 2026-08-16 用户真实 Chrome 实录：personal_center_strategy 之后还有这两个，以前全漏了
        params.add_param("profile_other_record_enable", '1')
        params.add_param("land_to", '1')
        # 公共组换 with_platform()：手写那版缺 pc_libra_divert / support_h265 / support_dash，
        # 且 round_trip_time 写死 100（实录是 0）
        params.with_platform(round_trip_time='0')
        params.with_web_id(auth, user_url)
        params.with_uifid(auth)
        params.add_param("msToken", auth.msToken)
        params.with_a_bogus()
        # 实录里 verifyFp / fp 在 a_bogus **之后**
        params.with_verify_fp(auth)
        return auth.request(f'{DouyinAPI.douyin_url}{api}', headers.get(), params=params.get())


    @staticmethod
    def get_device_id(auth, **kwargs):
        """
        获取设备ID.
        :param auth: DouyinAuth object.
        :return: 设备ID.
        """
        url = "https://www.douyin.com/aweme/v1/web/query/user"
        headers = HeaderBuilder().build(HeaderType.GET)
        refer = "https://www.douyin.com/discover"
        headers.set_header("referer", refer)
        headers.with_uifid(auth)
        params = Params()
        (params
         .with_platform()
         .add_param("publish_video_strategy_type", "2")
         .with_web_id(auth, refer)
         .with_ms_token()
         .add_param('verifyFp', auth.cookie['s_v_web_id'])
         .add_param('fp', auth.cookie['s_v_web_id'])
         .with_a_bogus()
         )
        return auth.request(url, headers.get(), params=params.get())

