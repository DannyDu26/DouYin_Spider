"""保留成功项目的参数顺序及双签名流程。"""
import threading
from .fingerprint import get_profile
from .ab_pure import ABogusPureSigner
from utils.dy_util import generate_msToken, generate_fake_webid, splice_url

_signer = None
_signer_lock = threading.Lock()

def generate_a_bogus(query, data="", host="www.douyin.com"):
    # 签名计数器由锁保护，多账号并发时仍保持一致。
    global _signer
    with _signer_lock:
        if _signer is None:
            _signer = ABogusPureSigner(fixed=False)
        return _signer.sign_query(query, data, host=host)

class Params:
    def __init__(self):
        self.params = {}

    def with_platform(self, round_trip_time='0', auth=None, url="",
                      uifid=True, verify_fp=True,
                      version_code='170400', version_name='17.4.0'):
        """www.douyin.com 的公共 query 组。

        字段与顺序按浏览器实测（2026-08-15）对齐：`pc_client_type` 后面紧跟
        `pc_libra_divert` / `support_h265` / `support_dash` / `cpu_core_num`，
        `device_memory` 挪到 `os_version` 之后。缺这几个会被部分接口静默拒绝。

        传了 `auth` 就顺带把 `webid` / `uifid` / `verifyFp` / `fp` 也补上 ——
        2026-08-16 抓 44 个主站接口统计，这四个的出现率分别是
        44/44、41/44、43/44、43/44，属于公共参数；以前要各接口自己记得调
        `with_web_id()` / `with_uifid()`，漏掉的就少字段。顺序照实录：
        `round_trip_time` 之后依次 webid、uifid、verifyFp、fp。

        个别接口确实不带其中某项（如 comment/list/reply 不带 uifid），
        用 `uifid=False` / `verify_fp=False` 关掉，**依据必须是该接口的抓包**。

        `round_trip_time` 默认 0：44 个接口里的主流取值，以前默认 50 是错的。
        """
        params = {
            'device_platform': 'webapp',
            'aid': '6383',
            'channel': 'channel_pc_web',
            'update_version_code': '170400',
            'pc_client_type': '1',
            'pc_libra_divert': 'Windows',
            'support_h265': '1',
            'support_dash': '1',
            'cpu_core_num': get_profile()["cpu_core_num"],
            'version_code': version_code,
            'version_name': version_name,
            'cookie_enabled': 'true',
            'screen_width': get_profile()["screen_width"],
            'screen_height': get_profile()["screen_height"],
            'browser_language': 'zh-CN',
            'browser_platform': 'Win32',
            'browser_name': get_profile()["browser_name"],
            'browser_version': get_profile()["browser_version"],
            'browser_online': 'true',
            'engine_name': 'Blink',
            'engine_version': get_profile()["engine_version"],
            'os_name': 'Windows',
            'os_version': '10',
            'device_memory': get_profile()["device_memory"],
            'platform': 'PC',
            'downlink': '10',
            'effective_type': '4g',
            'round_trip_time': round_trip_time,
        }
        self.params.update(params)
        if auth is not None:
            self.with_web_id(auth, url)
            if uifid:
                self.with_uifid(auth)
            if verify_fp:
                self.with_verify_fp(auth)
        return self

    def with_uifid(self, auth):
        """uifid 取自 UIFID Cookie，位置紧跟 webid。"""
        uifid = (auth.cookie or {}).get('UIFID', '') if auth else ''
        if uifid:
            self.params['uifid'] = uifid
        return self

    def with_verify_fp(self, auth):
        """verifyFp / fp 都取 s_v_web_id。注意不同接口相对 a_bogus 的位置不同。"""
        fp = (auth.cookie or {}).get('s_v_web_id', '')
        if fp:
            self.params['verifyFp'] = fp
            self.params['fp'] = fp
        return self

    def with_web_id(self, auth=None, url="", fake=False):
        # 走 auth.webid 而不是按 url 现抓：调用方传进来的页面常常是客户端渲染的，
        # 抓不到 user_unique_id 就会退化成随机数，被接口静默拒绝
        if fake or auth is None:
            webid = generate_fake_webid()
        else:
            webid = auth.webid
        self.params['webid'] = webid
        return self

    def with_a_bogus(self, data=None, host='www.douyin.com'):
        """算 a_bogus。host 必须是本次请求的子域：签名里内嵌 (aid, page_id)，
        www / live / creator 三套值不同，用错了强校验接口会判人机验证。
        """
        query = splice_url(self.get())
        if data is not None:
            data = splice_url(data)
        else:
            data = ''
        abogus = generate_a_bogus(query, data, host=host)
        self.add_param('a_bogus', abogus)
        return self

    def with_ms_token(self):
        msToken = generate_msToken()
        self.params['msToken'] = msToken
        return self

    def signed_url(self, base_url, auth=None, ts=None):
        """把参数拼成带 `timestamp` + `x-secsdk-web-signature` 的完整 URL。

        只有 secsdk 的 webSign 策略覆盖的接口才需要（见
        `utils.user_read.web_sign.PROTECTED_PATHS_GET`）。签名是对**规范化后的
        query** 算的，服务端也按收到的 query 校验，所以必须发这里返回的 URL，
        不能再把 `self.get()` 当 params 传给 requests——那样 query 会被重新
        编码一遍，与签名输入对不上。

        用法：
            url = params.signed_url(f'{DouyinAPI.douyin_url}{api}', auth)
            resp = requests.get(url, headers=..., cookies=...)   # 不传 params
        """
        from .web_sign import sign_url
        uifid = (auth.cookie or {}).get('UIFID', '') if auth else ''
        return sign_url(base_url + '?' + self.toString(), ts=ts, uifid=uifid)

    def add_param(self, key, value):
        self.params[key] = value
        return self

    def get(self):
        return self.params

    def toString(self):
        # 按url参数格式拼接参数
        return "&".join([f"{k}={v}" for k, v in self.params.items()])
