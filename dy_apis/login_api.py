import time
import urllib.parse
import inspect
from contextlib import suppress

import aiohttp
import asyncio
import os
from playwright.async_api import async_playwright
import requests
from loguru import logger

from builder.auth import DouyinAuth
from builder.header import HeaderBuilder, HeaderType
from builder.params import Params
from utils.dy_util import generate_ree_key, generate_bd_ticket_client_data
import json
from threading import Thread
import qrcode


class BrowserVerificationRequiredError(RuntimeError):
    """无界面浏览器被抖音验证码中间页拦截。"""


class DYLoginApi:

    def __init__(self):
        self.base_url = "https://sso.douyin.com/"
        self.home_url = 'https://www.douyin.com/'

    @staticmethod
    async def _launch_browser(playwright, headless):
        """优先启动 Playwright Chromium，Windows 可回退到系统 Chrome。"""
        launch_options = {
            'headless': headless,
            'args': ['--disable-blink-features=AutomationControlled'],
        }
        browser_channel = os.getenv('PLAYWRIGHT_BROWSER_CHANNEL', '').strip()
        if browser_channel:
            launch_options['channel'] = browser_channel
            return await playwright.chromium.launch(**launch_options)

        try:
            return await playwright.chromium.launch(**launch_options)
        except Exception as error:
            if os.name == 'nt':
                logger.warning('未找到 Playwright Chromium，回退到系统 Chrome')
                launch_options['channel'] = 'chrome'
                return await playwright.chromium.launch(**launch_options)
            raise RuntimeError(
                '未安装 Chromium，请执行: python -m playwright install --with-deps chromium'
            ) from error

    # 生成初始cookies
    async def dyGenerateInitData(self, headless=True, cookie_str=""):
        async with async_playwright() as p:
            browser = await self._launch_browser(p, headless)
            try:
                context = await browser.new_context()
                if cookie_str:
                    await context.add_cookies([
                        {"name": part.strip().partition("=")[0],
                         "value": part.strip().partition("=")[2],
                         "domain": ".douyin.com", "path": "/"}
                        for part in cookie_str.split(";") if part.strip()
                    ])
                page = await context.new_page()
                await page.goto(self.home_url)
                await page.wait_for_load_state("load")
                keys_str = None
                web_protect_str = None
                for _ in range(6):
                    await asyncio.sleep(4)
                    await page.mouse.wheel(0, 600)
                    keys_str = await page.evaluate('localStorage["security-sdk/s_sdk_crypt_sdk"]')
                    web_protect_str = await page.evaluate('localStorage["security-sdk/s_sdk_sign_data_key/web_protect"]')
                    if keys_str and web_protect_str:
                        break
                cookies = {cookie['name']: cookie['value'] for cookie in await context.cookies()}
                # 使用完整 Cookie 初始化 ttwid 等认证状态
                complete_cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                auth = DouyinAuth()
                auth.perepare_auth(complete_cookie_str, web_protect_str, keys_str)
                return auth
            finally:
                with suppress(Exception):
                    await browser.close()

    @staticmethod
    async def _notify_qrcode(qrcode_callback, qrcode_bytes: bytes):
        """向调用方传递内存二维码，同时兼容同步和异步回调。"""
        if qrcode_callback is None:
            return
        result = qrcode_callback(qrcode_bytes)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    async def capture_login_qrcode(page, qrcode_path=None) -> bytes:
        """截取登录二维码；未传路径时全程仅使用内存。"""
        qrcode_handle = await page.wait_for_function('''() => {
            const images = Array.from(document.querySelectorAll('article img'));
            return images.find(image => {
                const rect = image.getBoundingClientRect();
                return rect.width >= 120 && rect.height >= 120
                    && Math.abs(rect.width - rect.height) <= 8;
            }) || false;
        }''', timeout=15000)
        qrcode_image = qrcode_handle.as_element()
        if not qrcode_image:
            raise RuntimeError('未找到登录二维码元素')

        screenshot_options = {}
        if qrcode_path:
            qrcode_path = os.path.abspath(qrcode_path)
            os.makedirs(os.path.dirname(qrcode_path), exist_ok=True)
            screenshot_options['path'] = qrcode_path
        qrcode_bytes = await qrcode_image.screenshot(**screenshot_options)
        if qrcode_path:
            logger.info('登录二维码已保存至 {}', qrcode_path)
        return qrcode_bytes

    @staticmethod
    async def wait_and_click_login(page, deadline, headless, poll_interval=1.0):
        """可视模式等待人工通过验证，然后继续打开登录二维码。"""
        verification_logged = False
        while time.time() < deadline:
            if page.is_closed():
                raise RuntimeError('登录窗口已关闭')
            try:
                title = await page.title()
                if '验证码中间页' in title:
                    if headless:
                        raise BrowserVerificationRequiredError(
                            '抖音要求浏览器验证，请使用可视模式手工完成'
                        )
                    if not verification_logged:
                        logger.warning('请在浏览器窗口中手工完成抖音验证码')
                        verification_logged = True
                else:
                    clicked = await page.evaluate('''() => {
                        const nodes = Array.from(document.querySelectorAll('button, span, div, a'));
                        const hit = nodes.find(
                            n => ['登录','登 录'].includes((n.textContent||'').trim())
                        );
                        if (!hit) return false;
                        hit.click();
                        return true;
                    }''')
                    if clicked:
                        return
            except BrowserVerificationRequiredError:
                raise
            except Exception:
                # 页面跳转期间上下文可能短暂销毁，继续等待即可。
                pass
            await asyncio.sleep(poll_interval)
        raise TimeoutError('登录超时：未找到登录入口或未完成浏览器验证')

    # 扫码登录并抓 ticket
    async def login_grab_ticket(self, headless=False, timeout=180, qrcode_path=None,
                                qrcode_callback=None):
        """扫码登录并返回完整认证，二维码可通过 bytes 回调获取。"""
        async with async_playwright() as p:
            browser = await self._launch_browser(p, headless)
            try:
                page = await browser.new_page()
                await page.goto(self.home_url)
                await page.wait_for_load_state("load")
                deadline = time.time() + timeout
                await self.wait_and_click_login(page, deadline, headless)
                if qrcode_path or qrcode_callback:
                    qrcode_bytes = await self.capture_login_qrcode(page, qrcode_path)
                    await self._notify_qrcode(qrcode_callback, qrcode_bytes)
                logger.info('扫码登录会话已就绪')
                keys_str = None
                web_protect_str = None
                cookies = {}
                login_cookie_names = ('sessionid', 'sessionid_ss', 'sid_guard', 'uid_tt', 'uid_tt_ss')
                is_logged_in = False
                while time.time() < deadline:
                    await asyncio.sleep(2)
                    if page.is_closed():
                        raise RuntimeError("登录窗口已关闭")
                    try:
                        keys_str = await page.evaluate('localStorage["security-sdk/s_sdk_crypt_sdk"]')
                        web_protect_str = await page.evaluate(
                            'localStorage["security-sdk/s_sdk_sign_data_key/web_protect"]'
                        )
                        cookies = {
                            cookie['name']: cookie['value']
                            for cookie in await page.context.cookies()
                        }
                    except Exception:
                        # 登录跳转瞬间执行上下文被销毁，下一轮重试
                        continue
                    # Cookie 和安全票据都就绪后才算成功
                    is_logged_in = any(cookies.get(name) for name in login_cookie_names)
                    if is_logged_in and keys_str and web_protect_str:
                        break
                if not (is_logged_in and keys_str and web_protect_str):
                    raise TimeoutError("登录超时：未检测到完整登录凭证")

                # 必须传入完整 Cookie，否则 ttwid 等派生状态会丢失
                complete_cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                auth = DouyinAuth()
                auth.perepare_auth(complete_cookie_str, web_protect_str, keys_str)
                return auth
            finally:
                with suppress(Exception):
                    await browser.close()

    # 登录凭证写入 .env
    ENV_FILE = ".env"
    TICKET_KEYS = ("DY_TICKET", "DY_TS_SIGN", "DY_CLIENT_CERT", "DY_PRIVATE_KEY")

    def save_credential(self, auth):
        cookie_str = "; ".join(f"{k}={v}" for k, v in auth.cookie.items())
        values = {
            "DY_COOKIES": cookie_str,
            "DY_TICKET": auth.ticket or "",
            "DY_TS_SIGN": auth.ts_sign or "",
            "DY_CLIENT_CERT": auth.client_cert or "",
            "DY_PRIVATE_KEY": auth.private_key or "",
        }
        set_values = {k: v for k, v in values.items() if v}
        from dotenv import set_key
        for key, value in set_values.items():
            set_key(self.ENV_FILE, key, value)
        return os.path.abspath(self.ENV_FILE)

    async def get_login_auth(self, headless=False):
        """优先从 .env 读 ticket，没有就扫码登录后写入 .env。"""
        from utils.common_util import load_env
        auth = load_env()
        if auth.ticket and auth.private_key:
            return auth
        auth = await self.login_grab_ticket(headless=headless)
        logger.info(f"登录凭证已存 {self.save_credential(auth)}")
        return auth

    # 获取二维码
    def dyGenerateQRcode(self, auth) -> dict:
        api = f"get_qrcode/"
        headers = HeaderBuilder().build(HeaderType.GET)
        headers.set_referer("https://www.douyin.com/")
        params = Params()
        params.add_param("service", 'https://www.douyin.com')
        params.add_param("need_logo", 'false')
        params.add_param("need_short_url", 'false')
        params.add_param("passport_jssdk_version", "1.0.26")
        params.add_param("passport_jssdk_type", "pro")
        params.add_param("aid", '6383')
        params.add_param("language", 'zh')
        params.add_param("account_sdk_source", 'sso')
        params.add_param("account_sdk_source_info", "7e276d64776172647760466a6b66707777606b667c273f3735292772606761776c736077273f63646976602927666d776a686061776c736077273f63646976602927766d60696961776c736077273f63646976602927756970626c6b76273f302927756077686c76766c6a6b76273f5e7e276b646860273f276b6a716c636c6664716c6a6b762729277671647160273f2775776a68757127785829276c6b6b60774d606c626d71273f3431313729276c6b6b6077526c61716d273f3436363129276a707160774d606c626d71273f3430303729276a70716077526c61716d273f37303335292776716a64776260567164717076273f7e276c6b61607d60614147273f7e276c6167273f276a676f6066712729276a75606b273f2763706b66716c6a6b2729276c6b61607d60614147273f276a676f6066712729274c41474e607c57646b6260273f2763706b66716c6a6b2729276a75606b4164716467647660273f27706b6160636c6b60612729276c7656646364776c273f636469766029276d6476436071666d273f6364697660782927696a66646956716a77646260273f7e276c76567075756a77714956716a77646260273f717770602927766c7f60273f3337313c32292772776c7160273f7177706078292776716a7764626054706a7164567164717076273f7e277076646260273f343031323236292774706a7164273f34373d3d313c33313030333d29276c7655776c73647160273f6364697660787829276b6a716c636c6664716c6a6b556077686c76766c6a6b273f2761606364706971272927756077636a7768646b6660273f7e27716c68604a776c626c6b273f3432373635343636303c3131372b362927707660614f564d606475566c7f60273f3437333c373c32343529276b64736c6264716c6a6b516c686c6b62273f7e276160666a616061476a617c566c7f60273f3035333434322927606b71777c517c7560273f276b64736c6264716c6a6b2729276c6b6c716c64716a77517c7560273f276b64736c6264716c6a6b2729276b646860273f276d717175763f2a2a7272722b616a707c6c6b2b666a682a707660772a48563172496f4447444444444075684d363131466e46723748303d513636543d5170437561734f764a7c645f6667527d444866334d3536724a534363344a72316855553c315141505631507627292777606b61607747696a666e6c6b62567164717076273f276b6a6b2867696a666e6c6b62272927766077736077516c686c6b62273f276c6b6b60772971715a6462722966616b286664666d602960616260296a776c626c6b272927627069605671647771273f343d3d3d2b3029276270696041707764716c6a6b273f34362b363c3c3c3c3c3c323334303d34313778782927776074706076715a6d6a7671273f277272722b616a707c6c6b2b666a68272927776074706076715a7564716d6b646860273f272a707660772a48563172496f4447444444444075684d363131466e46723748303d513636543d5170437561734f764a7c645f6667527d444866334d3536724a534363344a72316855553c31514150563150762778")
        params.add_param("passport_ztsdk", '3.0.20')
        params.add_param("passport_verify", '1.0.17')
        params.add_param("device_platform", 'web_app')
        params.add_param("msToken", auth.cookie['msToken'])
        params.with_a_bogus()
        resp = requests.get(self.base_url + api, headers=headers.get(), cookies=auth.cookie, params=params.get(), verify=False)
        return json.loads(resp.text)


    def dyCheckQrCodeLogin(self, auth, token):
        api = 'check_qrconnect/'
        headers = HeaderBuilder().build(HeaderType.GET)
        headers.set_referer("https://www.douyin.com/")
        params = Params()
        params.add_param("service", 'https://www.douyin.com')
        params.add_param("token", token)
        params.add_param("need_logo", 'false')
        params.add_param("is_frontier", 'false')
        params.add_param("need_short_url", 'false')
        params.add_param("passport_jssdk_version", "1.0.26")
        params.add_param("passport_jssdk_type", "pro")
        params.add_param("aid", '6383')
        params.add_param("language", 'zh')
        params.add_param("account_sdk_source", 'sso')
        params.add_param("account_sdk_source_info", "7e276d64776172647760466a6b66707777606b667c273f3735292772606761776c736077273f63646976602927666d776a686061776c736077273f63646976602927766d60696961776c736077273f63646976602927756970626c6b76273f302927756077686c76766c6a6b76273f5e7e276b646860273f276b6a716c636c6664716c6a6b762729277671647160273f2775776a68757127785829276c6b6b60774d606c626d71273f3431313729276c6b6b6077526c61716d273f3436363129276a707160774d606c626d71273f3430303729276a70716077526c61716d273f37303335292776716a64776260567164717076273f7e276c6b61607d60614147273f7e276c6167273f276a676f6066712729276a75606b273f2763706b66716c6a6b2729276c6b61607d60614147273f276a676f6066712729274c41474e607c57646b6260273f2763706b66716c6a6b2729276a75606b4164716467647660273f27706b6160636c6b60612729276c7656646364776c273f636469766029276d6476436071666d273f6364697660782927696a66646956716a77646260273f7e276c76567075756a77714956716a77646260273f717770602927766c7f60273f3337313c32292772776c7160273f7177706078292776716a7764626054706a7164567164717076273f7e277076646260273f343031323236292774706a7164273f34373d3d313c33313030333d29276c7655776c73647160273f6364697660787829276b6a716c636c6664716c6a6b556077686c76766c6a6b273f2761606364706971272927756077636a7768646b6660273f7e27716c68604a776c626c6b273f3432373635343636303c3131372b362927707660614f564d606475566c7f60273f3437333c373c32343529276b64736c6264716c6a6b516c686c6b62273f7e276160666a616061476a617c566c7f60273f3035333434322927606b71777c517c7560273f276b64736c6264716c6a6b2729276c6b6c716c64716a77517c7560273f276b64736c6264716c6a6b2729276b646860273f276d717175763f2a2a7272722b616a707c6c6b2b666a682a707660772a48563172496f4447444444444075684d363131466e46723748303d513636543d5170437561734f764a7c645f6667527d444866334d3536724a534363344a72316855553c315141505631507627292777606b61607747696a666e6c6b62567164717076273f276b6a6b2867696a666e6c6b62272927766077736077516c686c6b62273f276c6b6b60772971715a6462722966616b286664666d602960616260296a776c626c6b272927627069605671647771273f343d3d3d2b3029276270696041707764716c6a6b273f34362b363c3c3c3c3c3c323334303d34313778782927776074706076715a6d6a7671273f277272722b616a707c6c6b2b666a68272927776074706076715a7564716d6b646860273f272a707660772a48563172496f4447444444444075684d363131466e46723748303d513636543d5170437561734f764a7c645f6667527d444866334d3536724a534363344a72316855553c31514150563150762778")
        params.add_param("passport_ztsdk", '3.0.20')
        params.add_param("passport_verify", '1.0.17')
        params.add_param("biz_trace_id", auth.cookie['biz_trace_id'])
        params.add_param("device_platform", 'web_app')
        params.add_param("msToken", auth.cookie['msToken'])
        params.with_a_bogus()
        resp = requests.get(self.base_url + api, headers=headers.get(), cookies=auth.cookie, params=params.get(), verify=False)
        return json.loads(resp.text)

    # 手机验证码登录
    def dyGeneratePhoneVerificationCode(self, phone_num, auth):
        api = "send_activation_code/v2/"
        headers = {
            "accept": "application/json, text/javascript",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "cache-control": "no-cache",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.douyin.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": "https://www.douyin.com/",
            "sec-ch-ua": "\"Not)A;Brand\";v=\"99\", \"Microsoft Edge\";v=\"127\", \"Chromium\";v=\"127\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0",
            "x-tt-passport-csrf-token": auth.cookie['passport_csrf_token'],
            "x-tt-passport-trace-id": auth.cookie['biz_trace_id']
        }
        params = Params()
        params.add_param("passport_jssdk_version", "1.0.26")
        params.add_param("passport_jssdk_type", "pro")
        params.add_param("aid", "6383")
        params.add_param("language", "zh")
        params.add_param("account_sdk_source", "sso")
        params.add_param("account_sdk_source_info", "7e276d64776172647760466a6b66707777606b667c273f3735292772606761776c736077273f63646976602927666d776a686061776c736077273f63646976602927766d60696961776c736077273f63646976602927756970626c6b76273f302927756077686c76766c6a6b76273f5e7e276b646860273f276b6a716c636c6664716c6a6b762729277671647160273f2775776a68757127785829276c6b6b60774d606c626d71273f3431313729276c6b6b6077526c61716d273f3434313129276a707160774d606c626d71273f3430303729276a70716077526c61716d273f37303335292776716a64776260567164717076273f7e276c6b61607d60614147273f7e276c6167273f276a676f6066712729276a75606b273f2763706b66716c6a6b2729276c6b61607d60614147273f276a676f6066712729274c41474e607c57646b6260273f2763706b66716c6a6b2729276a75606b4164716467647660273f27706b6160636c6b60612729276c7656646364776c273f636469766029276d6476436071666d273f6364697660782927696a66646956716a77646260273f7e276c76567075756a77714956716a77646260273f717770602927766c7f60273f363d333433292772776c7160273f7177706078292776716a7764626054706a7164567164717076273f7e277076646260273f3135323c3d292774706a7164273f34373d3d313c33313030333d29276c7655776c73647160273f6364697660787829276b6a716c636c6664716c6a6b556077686c76766c6a6b273f2761606364706971272927756077636a7768646b6660273f7e27716c68604a776c626c6b273f34323736353734333d3d3c3d302b342927707660614f564d606475566c7f60273f34313331323c37303129276b64736c6264716c6a6b516c686c6b62273f7e276160666a616061476a617c566c7f60273f323537303c362927606b71777c517c7560273f276b64736c6264716c6a6b2729276c6b6c716c64716a77517c7560273f276b64736c6264716c6a6b2729276b646860273f276d717175763f2a2a7272722b616a707c6c6b2b666a682a3a7760666a6868606b61383427292777606b61607747696a666e6c6b62567164717076273f276b6a6b2867696a666e6c6b62272927766077736077516c686c6b62273f276c6b6b60772971715a6462722966616b286664666d602960616260296a776c626c6b272927627069605671647771273f343333362b3635353535353532343037303329276270696041707764716c6a6b273f276b6a6b602778782927776074706076715a6d6a7671273f277272722b616a707c6c6b2b666a68272927776074706076715a7564716d6b646860273f272a2778")
        params.add_param("passport_ztsdk", "3.0.20")
        params.add_param("passport_verify", "1.0.17")
        params.add_param("biz_trace_id", auth.cookie['biz_trace_id'])
        params.add_param("device_platform", "web_app")
        params.add_param("msToken", auth.cookie['msToken'])
        data = generateSecretPhoneNum(phone_num)
        params.with_a_bogus(data)
        response = requests.post(self.base_url + api, headers=headers, cookies=auth.cookie, params=params.get(), data=data, verify=False)
        res_json = json.loads(response.text)
        if res_json['error_code'] == 0:
            print("无需过滑块, 验证码发送成功")
            return res_json

        firstLoginRes = json.loads(response.text)
        iframeTemplate = self.generateIframe(auth.cookie, firstLoginRes)
        print(iframeTemplate)
        input('过滑块')
        # 过验证码后
        params.add_param("fp", auth.cookie['s_v_web_id'])
        params.add_param("verifyFp", auth.cookie['s_v_web_id'])
        response = requests.post(self.base_url + api, headers=headers, cookies=auth.cookie, params=params.get(), data=data, verify=False)
        return json.loads(response.text)

    def dyPhoneVerificationCodeLogin(self, auth, phone_num, code):
        headers = {
            "accept": "application/json, text/javascript",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "bd-ticket-guard-iteration-version": "1",
            "bd-ticket-guard-ree-public-key": generate_ree_key(auth.private_key),
            "bd-ticket-guard-version": "2",
            "bd-ticket-guard-web-version": "1",
            "cache-control": "no-cache",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.douyin.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": "https://www.douyin.com/",
            "sec-ch-ua": "\"Not)A;Brand\";v=\"99\", \"Microsoft Edge\";v=\"127\", \"Chromium\";v=\"127\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0",
            "x-tt-passport-csrf-token": auth.cookie['passport_csrf_token'],
            "x-tt-passport-trace-id": auth.cookie['biz_trace_id']
        }
        api = "quick_login/v2/"
        params = Params()
        params.add_param("passport_jssdk_version", "1.0.26")
        params.add_param("passport_jssdk_type", "pro")
        params.add_param("aid", "6383")
        params.add_param("language", "zh")
        params.add_param("account_sdk_source", "sso")
        params.add_param("account_sdk_source_info", "7e276d64776172647760466a6b66707777606b667c273f3735292772606761776c736077273f63646976602927666d776a686061776c736077273f63646976602927766d60696961776c736077273f63646976602927756970626c6b76273f302927756077686c76766c6a6b76273f5e7e276b646860273f276b6a716c636c6664716c6a6b762729277671647160273f2775776a68757127785829276c6b6b60774d606c626d71273f3431313729276c6b6b6077526c61716d273f3434313129276a707160774d606c626d71273f3430303729276a70716077526c61716d273f37303335292776716a64776260567164717076273f7e276c6b61607d60614147273f7e276c6167273f276a676f6066712729276a75606b273f2763706b66716c6a6b2729276c6b61607d60614147273f276a676f6066712729274c41474e607c57646b6260273f2763706b66716c6a6b2729276a75606b4164716467647660273f27706b6160636c6b60612729276c7656646364776c273f636469766029276d6476436071666d273f6364697660782927696a66646956716a77646260273f7e276c76567075756a77714956716a77646260273f717770602927766c7f60273f363d333433292772776c7160273f7177706078292776716a7764626054706a7164567164717076273f7e277076646260273f3135323c3d292774706a7164273f34373d3d313c33313030333d29276c7655776c73647160273f6364697660787829276b6a716c636c6664716c6a6b556077686c76766c6a6b273f2761606364706971272927756077636a7768646b6660273f7e27716c68604a776c626c6b273f34323736353734333d3d3c3d302b342927707660614f564d606475566c7f60273f34313331323c37303129276b64736c6264716c6a6b516c686c6b62273f7e276160666a616061476a617c566c7f60273f323537303c362927606b71777c517c7560273f276b64736c6264716c6a6b2729276c6b6c716c64716a77517c7560273f276b64736c6264716c6a6b2729276b646860273f276d717175763f2a2a7272722b616a707c6c6b2b666a682a3a7760666a6868606b61383427292777606b61607747696a666e6c6b62567164717076273f276b6a6b2867696a666e6c6b62272927766077736077516c686c6b62273f276c6b6b60772971715a6462722966616b286664666d602960616260296a776c626c6b272927627069605671647771273f343333362b3635353535353532343037303329276270696041707764716c6a6b273f276b6a6b602778782927776074706076715a6d6a7671273f277272722b616a707c6c6b2b666a68272927776074706076715a7564716d6b646860273f272a2778")
        params.add_param("passport_ztsdk", "3.0.20")
        params.add_param("passport_verify", "1.0.17")
        params.add_param("biz_trace_id", auth.cookie['biz_trace_id'])
        params.add_param("device_platform", "web_app")
        params.add_param("msToken", auth.cookie['msToken'])
        params.with_a_bogus()
        data = generateSecretCode(phone_num, code)
        response = requests.post(self.base_url + api, headers=headers, cookies=auth.cookie, params=params.get(), data=data, verify=False)
        responseCookies = response.cookies.get_dict()
        # 结合到cookies中
        auth.cookie.update(responseCookies)
        return json.loads(response.text), auth

    def generateIframe(self, cookies, firstLoginRes):
        verify_center_decision_conf = json.loads(firstLoginRes['verify_center_decision_conf'])
        url = r'https://rmc.bytedance.com/verifycenter/captcha/v2?from=iframe&fp=' + cookies["s_v_web_id"] + '&env={"screen":{"w":2560,"h":1600},"browser":{"w":2560,"h":1552},"page":{"w":1166,"h":1442},"document":{"width":1166},"product_host":"www.douyin.com","vc_version":"1.0.0.100","maskTime":' + str(int(time.time()) * 1000) + ',"h5_check_version":"3.8.6"}&aid=6383&repoId=579047&scene_level=p2&app_name=抖音 Web 站&host=https://verify.zijieapi.com&lang=zh&verify_data={"code":"10000","from":"shark_admin","type":"verify","version":"1","region":"cn","subtype":"slide","ui_type":"","detail":"' + verify_center_decision_conf["detail"] + '","verify_event":"tt_sso_send_code","fp":"' + cookies["s_v_web_id"] + '","server_sdk_env":"{\\"idc\\":\\"lq\\",\\"region\\":\\"CN\\",\\"server_type\\":\\"passport\\"}","log_id":"' + verify_center_decision_conf["log_id"] + '","is_assist_mobile":false,"is_complex_sms":false,"identity_action":"","identity_scene":"","verify_scene":"passport","login_status":0,"aid":0,"mfa_decision":""}'
        url = self.quoteUrl(url)
        iframeTemplate = f'<iframe src="{url}" style="z-index: 999;border: none;display: block;visibility: visible;border-radius: 6px;overflow: hidden;position: absolute;left: 50%;top: 50%;transform: translate(-50%, -50%);width: 380px;height: 384px;"></iframe>'
        return iframeTemplate

    def quoteUrl(self, url):
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        new_url = ''
        for k, v in params.items():
            i = f'{k}={v[0]}&'
            if k == 'host':
                new_url += requests.utils.quote(i, safe='?=&')
            else:
                new_url += requests.utils.quote(i, safe='/?=&*')
        return parsed.scheme + '://' + parsed.netloc + parsed.path + '?' + new_url[:-1]

    def persistenceLoginInfo(self, auth):
        url = "https://www.douyin.com/passport/user/web_record_status/set/"
        api = "passport/user/web_record_status/set/"
        headers = {
            "accept": "application/json, text/javascript",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            # "bd-ticket-guard-client-data": generate_bd_ticket_client_data(api, auth.ticket, auth.ts_sign, auth.private_key),
            "bd-ticket-guard-iteration-version": "1",
            "bd-ticket-guard-ree-public-key": generate_ree_key(auth.private_key),
            "bd-ticket-guard-version": "2",
            "bd-ticket-guard-web-version": "1",
            "cache-control": "no-cache",
            "content-type": "application/x-www-form-urlencoded",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": "https://www.douyin.com/video/7212619184386182435",
            "sec-ch-ua": "\"Not)A;Brand\";v=\"99\", \"Microsoft Edge\";v=\"127\", \"Chromium\";v=\"127\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0",
            "x-tt-passport-csrf-token": "07e71018d12ee15b8a50b086cd82d021",
            "x-tt-passport-trace-id": "5714b00b"
        }
        params = Params()
        params.add_param("user_web_record_status", "1")
        params.add_param("passport_jssdk_version", "1.0.26")
        params.add_param("passport_jssdk_type", "pro")
        params.add_param("aid", "6383")
        params.add_param("language", "zh")
        params.add_param("account_sdk_source", "web")
        params.add_param("account_sdk_source_info", "7e276d64776172647760466a6b66707777606b667c273f3735292772606761776c736077273f63646976602927666d776a686061776c736077273f63646976602927766d60696961776c736077273f63646976602927756970626c6b76273f302927756077686c76766c6a6b76273f5e7e276b646860273f276b6a716c636c6664716c6a6b762729277671647160273f2775776a68757127785829276c6b6b60774d606c626d71273f3431313729276c6b6b6077526c61716d273f3434313129276a707160774d606c626d71273f3430303729276a70716077526c61716d273f37303335292776716a64776260567164717076273f7e276c6b61607d60614147273f7e276c6167273f276a676f6066712729276a75606b273f2763706b66716c6a6b2729276c6b61607d60614147273f276a676f6066712729274c41474e607c57646b6260273f2763706b66716c6a6b2729276a75606b4164716467647660273f27706b6160636c6b60612729276c7656646364776c273f636469766029276d6476436071666d273f6364697660782927696a66646956716a77646260273f7e276c76567075756a77714956716a77646260273f717770602927766c7f60273f363d333433292772776c7160273f7177706078292776716a7764626054706a7164567164717076273f7e277076646260273f3135323c3d292774706a7164273f34373d3d313c33313030333d29276c7655776c73647160273f6364697660787829276b6a716c636c6664716c6a6b556077686c76766c6a6b273f2761606364706971272927756077636a7768646b6660273f7e27716c68604a776c626c6b273f34323736353734333d3d3c3d302b342927707660614f564d606475566c7f60273f34313331323c37303129276b64736c6264716c6a6b516c686c6b62273f7e276160666a616061476a617c566c7f60273f323537303c362927606b71777c517c7560273f276b64736c6264716c6a6b2729276c6b6c716c64716a77517c7560273f276b64736c6264716c6a6b2729276b646860273f276d717175763f2a2a7272722b616a707c6c6b2b666a682a3a7760666a6868606b61383427292777606b61607747696a666e6c6b62567164717076273f276b6a6b2867696a666e6c6b62272927766077736077516c686c6b62273f276c6b6b60772971715a6462722966616b286664666d602960616260296a776c626c6b272927627069605671647771273f343333362b3635353535353532343037303329276270696041707764716c6a6b273f276b6a6b602778782927776074706076715a6d6a7671273f277272722b616a707c6c6b2b666a68272927776074706076715a7564716d6b646860273f272a2778")
        params.add_param("passport_ztsdk", "3.0.20")
        params.add_param("passport_verify", "1.0.17")
        params.add_param("biz_trace_id", auth.cookie['biz_trace_id'])
        params.add_param("device_platform", "web_app")
        params.add_param("msToken", auth.cookie['msToken'])
        params.with_a_bogus()
        response = requests.get(url, headers=headers, cookies=auth.cookie, params=params.get(), verify=False)
        auth.cookie.update(response.cookies.get_dict())
        return json.loads(response.text)


    # ==========================
    def generateQrcode(self, verify_url):
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(verify_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.show()

    async def qrcodeMain(self):
        auth = await self.dyGenerateInitData()
        qrCodeDict = self.dyGenerateQRcode(auth)
        token = qrCodeDict['data']['token']
        verify_url = qrCodeDict['data']['qrcode_index_url']
        qrcode_thread = Thread(target=self.generateQrcode, args=(verify_url,))
        qrcode_thread.start()
        while True:
            checkLoginInfo = self.dyCheckQrCodeLogin(auth, token)
            print(checkLoginInfo)
            await asyncio.sleep(10)


    async def phoneMain(self):
        auth = await self.dyGenerateInitData()
        phone_num = "15251991681"
        sendCodeRes = self.dyGeneratePhoneVerificationCode(phone_num, auth)
        print(sendCodeRes)
        code = input("请输入验证码：")
        loginRes, auth = self.dyPhoneVerificationCodeLogin(auth, phone_num, code)
        print(loginRes)
        redirect_url = loginRes['redirect_url']
        print(redirect_url)
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": "https://www.douyin.com/?recommend=1",
            "sec-ch-ua": "\"Not)A;Brand\";v=\"99\", \"Microsoft Edge\";v=\"127\", \"Chromium\";v=\"127\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0",
        }
        response = requests.get(redirect_url, headers=headers, cookies=auth.cookie, verify=False)
        print(response.status_code)
        if response.status_code == 302:
            print(response.headers)
            location = response.headers['Location']
            response = requests.get(location, headers=headers, cookies=auth.cookie, verify=False)
            auth.cookie.update(response.cookies.get_dict())
            if response.status_code == 302:
                print(response.headers)
                location = response.headers['Location']
                response = requests.get(location, headers=headers, cookies=auth.cookie, verify=False)
                auth.cookie.update(response.cookies.get_dict())

        res = self.persistenceLoginInfo(auth)
        print(res)
        # 将cookie转为字符串
        cookie_str = ''
        for k, v in auth.cookie.items():
            cookie_str += k + '=' + v + '; '
        cookie_str = cookie_str[:-2]
        print(cookie_str)

if __name__ == '__main__':
    login_util = DYLoginApi()
    loop = asyncio.get_event_loop()
    # loop.run_until_complete(login_util.qrcodeMain())
    loop.run_until_complete(login_util.phoneMain())
