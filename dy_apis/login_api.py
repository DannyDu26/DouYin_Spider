import time
import urllib.parse
import inspect
from contextlib import suppress

import aiohttp
import asyncio
import os
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
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


class QrCodeNotFoundError(RuntimeError):
    """登录入口已触发，但未在弹窗中找到二维码。"""


class SmsVerificationInteractionError(RuntimeError):
    """短信页面操作失败，仅携带可安全公开的阶段错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.safe_message = message


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
    async def _notify_verification(
            verification_callback,
            status: str,
            error_code: str | None = None,
            error_message: str | None = None,
    ):
        """通知服务身份验证状态，回调参数不得包含验证码。"""
        if verification_callback is None:
            return
        result = verification_callback(status, error_code, error_message)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    async def _page_body_text(page) -> str:
        """读取页面可见文本，页面跳转期间失败时按空文本处理。"""
        try:
            return await page.locator('body').inner_text(timeout=3000)
        except Exception:
            return ''

    @staticmethod
    async def _click_visible_text(page, texts: tuple[str, ...]) -> bool:
        """按给定顺序点击完全匹配的可见文本。"""
        return bool(await page.evaluate('''(texts) => {
            const nodes = Array.from(document.querySelectorAll(
                'button, [role="button"], a, div, span'
            ));
            for (const text of texts) {
                const hit = nodes.find(node => {
                    const value = (node.textContent || '').trim();
                    const rect = node.getBoundingClientRect();
                    return value === text && rect.width > 0 && rect.height > 0;
                });
                if (hit) {
                    hit.click();
                    return true;
                }
            }
            return false;
        }''', list(texts)))

    @staticmethod
    async def _click_enabled_button(
            page,
            texts: tuple[str, ...],
            timeout_seconds: float = 3.0,
    ) -> bool:
        """等待 React 将按钮启用后再点击，避免填充后立即点击被忽略。"""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for text in texts:
                locator = page.get_by_role('button', name=text, exact=True)
                for index in range(await locator.count()):
                    candidate = locator.nth(index)
                    if (
                            await candidate.is_visible()
                            and await candidate.is_enabled()
                            and await DYLoginApi._is_element_interactable(candidate)
                    ):
                        await candidate.click()
                        return True
            # 抖音部分版本使用 div/span 模拟按钮，需要检查 class/aria 后点击。
            clicked = await page.evaluate('''(texts) => {
                const nodes = Array.from(document.querySelectorAll(
                    'button, [role="button"], div, span'
                ));
                for (const text of texts) {
                    const node = nodes.find(item => {
                        const rect = item.getBoundingClientRect();
                        if ((item.textContent || '').trim() !== text
                                || rect.width <= 0 || rect.height <= 0) {
                            return false;
                        }
                        const top = document.elementFromPoint(
                            rect.left + rect.width / 2,
                            rect.top + rect.height / 2
                        );
                        return top === item
                            || item.contains(top)
                            || (top && top.contains(item));
                    });
                    if (!node) continue;
                    const target = node.closest('button, [role="button"]') || node;
                    const className = String(target.className || '').toLowerCase();
                    const disabled = target.disabled
                        || target.getAttribute('aria-disabled') === 'true'
                        || className.includes('disabled');
                    if (!disabled) {
                        target.click();
                        return true;
                    }
                }
                return false;
            }''', list(texts))
            if clicked:
                return True
            await page.wait_for_timeout(100)
        return False

    @classmethod
    async def _click_login_entry(cls, page) -> bool:
        """优先点击抖音头部原生登录按钮，再回退到通用定位。"""
        locator = page.locator(
            '#douyin-header-menuCt button[type="button"]'
        )
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if not await candidate.is_visible() or not await candidate.is_enabled():
                continue
            button_text = ''.join((await candidate.inner_text()).split())
            if button_text != '登录':
                continue
            aria_disabled = await candidate.get_attribute('aria-disabled')
            if aria_disabled == 'true' or not await cls._is_element_interactable(candidate):
                continue
            try:
                await candidate.click(timeout=3000)
                return True
            except Exception:
                # 页面重绘可能使精确按钮瞬间失效，继续使用通用定位重试。
                break
        return await cls._click_enabled_button(page, ('登录', '登 录'))

    @staticmethod
    async def _find_visible_sms_input(page):
        """查找可见短信输入框，不读取其中的验证码内容。"""
        selectors = (
            '#uc-second-verify input#button-input',
            '#uc-second-verify input[name="button-input"]',
            '#button-input',
            'input[name="button-input"]',
            'input[placeholder*="验证码"]',
            'input[inputmode="numeric"]',
            'input[type="tel"]',
            'input[maxlength="6"]',
        )
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if (
                        await candidate.is_visible()
                        and await DYLoginApi._is_element_interactable(candidate)
                ):
                    return candidate
        return None

    @staticmethod
    async def _is_element_interactable(locator) -> bool:
        """确认元素中心点未被另一层弹窗或遮罩覆盖。"""
        return bool(await locator.evaluate('''element => {
            const rect = element.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return false;
            const x = Math.max(0, Math.min(
                window.innerWidth - 1,
                rect.left + rect.width / 2
            ));
            const y = Math.max(0, Math.min(
                window.innerHeight - 1,
                rect.top + rect.height / 2
            ));
            const top = document.elementFromPoint(x, y);
            return top === element
                || element.contains(top)
                || (top && top.contains(element));
        }'''))

    @staticmethod
    async def _click_known_sms_submit(page) -> bool:
        """优先点击抖音登录组件中具有稳定 ID 的提交控件。"""
        locator = page.locator('#douyin_login_comp_btn_id')
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if not await candidate.is_visible():
                continue
            if not await DYLoginApi._is_element_interactable(candidate):
                continue
            button_text = (await candidate.inner_text()).strip()
            if button_text not in {'确认', '验证', '提交', '下一步', '完成'}:
                continue
            clickable = await candidate.evaluate('''element => {
                const className = String(element.className || '').toLowerCase();
                return element.getAttribute('aria-disabled') !== 'true'
                    && !className.includes('disabled');
            }''')
            if clickable:
                try:
                    await candidate.click(timeout=2000)
                    return True
                except Exception:
                    try:
                        # 遮罩层拦截鼠标事件时直接触发精确 ID 控件。
                        return bool(await candidate.evaluate('''element => {
                            element.click();
                            return true;
                        }'''))
                    except Exception:
                        # 精确 ID 点击失败后继续使用通用文本定位。
                        continue
        return False

    @staticmethod
    async def _click_second_verify_submit(page, timeout_seconds: float = 3.0) -> bool:
        """在稳定弹窗内按精确文案定位验证按钮并触发点击。"""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            panel = page.locator('#uc-second-verify')
            locator = panel.get_by_text('验证', exact=True)
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if not await candidate.is_visible():
                    continue
                if not await DYLoginApi._is_element_interactable(candidate):
                    continue
                if (await candidate.inner_text()).strip() != '验证':
                    continue
                enabled = await candidate.evaluate('''element => {
                    const className = String(element.className || '').toLowerCase();
                    return element.getAttribute('aria-disabled') !== 'true'
                        && !className.includes('disabled');
                }''')
                if not enabled:
                    continue

                # 探针挂在 document，避免 React 重绘替换按钮节点后监听丢失。
                probe_armed = await page.evaluate('''() => {
                    window.__douyinSmsVerifyClick = {
                        clicked: false,
                        trusted: false,
                    };
                    if (window.__douyinSmsVerifyClickHandler) {
                        document.removeEventListener(
                            'click',
                            window.__douyinSmsVerifyClickHandler,
                            true
                        );
                    }
                    if (!document.querySelector('#uc-second-verify')) return false;
                    window.__douyinSmsVerifyClickHandler = event => {
                        const matched = event.composedPath().some(node =>
                            node instanceof HTMLElement
                            && node.dataset.codexSmsSubmit === '1'
                        );
                        if (!matched) return;
                        window.__douyinSmsVerifyClick = {
                            clicked: true,
                            trusted: event.isTrusted,
                        };
                    };
                    document.addEventListener(
                        'click',
                        window.__douyinSmsVerifyClickHandler,
                        true
                    );
                    return true;
                }''')
                if not probe_armed:
                    continue

                try:
                    # 同步标记并点击，避免 Playwright click 静默无效。
                    await candidate.evaluate('''element => {
                        element.dataset.codexSmsSubmit = '1';
                        element.click();
                        return true;
                    }''')
                except Exception as error:
                    raise SmsVerificationInteractionError(
                        'QR_SMS_BUTTON_CLICK_FAILED',
                        '短信验证按钮点击事件执行失败，请重试',
                    ) from error

                await page.wait_for_timeout(50)
                probe = await page.evaluate(
                    '() => window.__douyinSmsVerifyClick || null'
                )
                if not probe or not probe.get('clicked'):
                    raise SmsVerificationInteractionError(
                        'QR_SMS_BUTTON_CLICK_NOT_RECEIVED',
                        '短信验证按钮未收到点击事件，请重试',
                    )
                logger.info(
                    '短信验证按钮已收到点击事件 trusted={}',
                    bool(probe.get('trusted')),
                )
                return True
            await page.wait_for_timeout(100)
        return False

    @staticmethod
    async def _set_input_dom_value(input_locator, value: str) -> None:
        """使用原生 setter 更新受控输入框，并补齐前端监听事件。"""
        await input_locator.evaluate('''(input, value) => {
            const descriptor = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype,
                'value'
            );
            descriptor.set.call(input, value);
            input.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                inputType: 'insertText',
                data: null,
            }));
            input.dispatchEvent(new Event('change', {bubbles: true}));
        }''', value)

    @classmethod
    async def _is_identity_verification(
            cls,
            page,
            body_text: str,
            verification_announced: bool,
    ) -> bool:
        """结合页面文本和 input 属性识别短信验证页。"""
        sms_input_visible = await cls._find_visible_sms_input(page) is not None
        choose_method_page = (
            '身份验证' in body_text
            and any(marker in body_text for marker in (
                '接收短信验证码',
                '验证码',
                '发送短信验证',
            ))
        )
        sms_code_page = (
            '接收短信验证码' in body_text
            and ('短信已发送' in body_text or sms_input_visible)
        )
        # 进入身份验证流程后，可见验证码框本身就是可靠信号。
        return choose_method_page or sms_code_page or (
            verification_announced and sms_input_visible
        )

    @classmethod
    async def request_sms_verification(cls, page) -> None:
        """在身份验证弹窗中选择接收短信验证码。"""
        if not await cls._click_visible_text(page, ('接收短信验证码',)):
            raise RuntimeError('未找到接收短信验证码入口')
        await page.wait_for_timeout(700)
        # 部分页面选择验证方式后还需再次点击获取验证码。
        await cls._click_visible_text(page, (
            '获取验证码',
            '发送验证码',
            '发送短信验证码',
            '重新发送',
        ))

    @classmethod
    async def resend_sms_verification(cls, page) -> None:
        """在验证码输入页重新发送短信。"""
        if not await cls._click_visible_text(page, (
            '重新发送',
            '重新获取',
            '获取验证码',
            '发送验证码',
        )):
            raise RuntimeError('未找到重新发送短信入口')

    @classmethod
    async def submit_sms_verification(cls, page, code: str) -> None:
        """填写短信验证码并提交，调用方负责保证验证码格式有效。"""
        try:
            input_locator = await cls._find_visible_sms_input(page)
        except Exception as error:
            raise SmsVerificationInteractionError(
                'QR_SMS_INPUT_LOOKUP_FAILED',
                '短信验证码输入框定位失败，请重试',
            ) from error
        if input_locator is None:
            raise SmsVerificationInteractionError(
                'QR_SMS_INPUT_NOT_FOUND',
                '未找到短信验证码输入框，请重试',
            )

        # 逐字符输入以触发 keydown/keyup，兼容依赖真实键盘事件的前端校验。
        try:
            try:
                await input_locator.click()
            except Exception:
                # 遮罩层阻止鼠标点击时直接聚焦输入框。
                await input_locator.evaluate('(input) => input.focus()')
            try:
                await input_locator.fill('')
            except Exception:
                await cls._set_input_dom_value(input_locator, '')
            press_sequentially = getattr(input_locator, 'press_sequentially', None)
            if press_sequentially is not None:
                try:
                    await press_sequentially(code, delay=80)
                except Exception:
                    try:
                        await input_locator.fill('')
                        await input_locator.type(code, delay=80)
                    except Exception:
                        await cls._set_input_dom_value(input_locator, code)
            else:
                try:
                    # 兼容较旧 Playwright 版本。
                    await input_locator.type(code, delay=80)
                except Exception:
                    await cls._set_input_dom_value(input_locator, code)
            # 再同步一次受控组件状态，并只校验长度，不读取或记录验证码。
            await cls._set_input_dom_value(input_locator, code)
            value_length = await input_locator.evaluate(
                'input => String(input.value || "").length'
            )
            if value_length != len(code):
                raise RuntimeError('验证码未写入输入框')
            await page.wait_for_timeout(150)
        except Exception as error:
            raise SmsVerificationInteractionError(
                'QR_SMS_INPUT_FAILED',
                '短信验证码输入失败，请重试',
            ) from error

        try:
            submitted = await cls._click_second_verify_submit(page)
            if not submitted:
                submitted = await cls._click_known_sms_submit(page)
            if not submitted:
                submitted = await cls._click_enabled_button(page, (
                    '确认',
                    '验证',
                    '提交',
                    '下一步',
                    '完成',
                ))
        except SmsVerificationInteractionError:
            raise
        except Exception as error:
            raise SmsVerificationInteractionError(
                'QR_SMS_BUTTON_CLICK_FAILED',
                '短信验证按钮点击失败，请重试',
            ) from error
        if not submitted:
            raise SmsVerificationInteractionError(
                'QR_SMS_BUTTON_NOT_READY',
                '短信验证按钮未启用或不可点击，请重试',
            )

    @staticmethod
    async def capture_login_qrcode(
            page,
            timeout_seconds: float = 60.0,
    ) -> bytes:
        """在给定时间内等待二维码渲染，并仅在内存中返回截图。"""
        timeout_ms = max(1, int(timeout_seconds * 1000))
        try:
            qrcode_handle = await page.wait_for_function('''() => {
                const images = Array.from(document.querySelectorAll('article img'));
                return images.find(image => {
                    const rect = image.getBoundingClientRect();
                    return rect.width >= 120 && rect.height >= 120
                        && Math.abs(rect.width - rect.height) <= 8;
                }) || false;
            }''', timeout=timeout_ms)
        except PlaywrightTimeoutError as error:
            raise QrCodeNotFoundError('登录弹窗未出现二维码') from error
        qrcode_image = qrcode_handle.as_element()
        if not qrcode_image:
            raise QrCodeNotFoundError('未找到登录二维码元素')

        return await qrcode_image.screenshot()

    @staticmethod
    async def capture_debug_screenshot(page, screenshot_path: str | None) -> bool:
        """覆盖保存登录页面调试截图，失败时不影响登录。"""
        if not screenshot_path or page.is_closed():
            return False
        try:
            screenshot_path = os.path.abspath(screenshot_path)
            os.makedirs(os.path.dirname(screenshot_path), mode=0o700, exist_ok=True)
            await page.screenshot(path=screenshot_path, full_page=True)
            with suppress(OSError):
                os.chmod(screenshot_path, 0o600)
            return True
        except Exception as error:
            # 不记录页面内容，避免账号信息进入日志。
            logger.warning('登录调试截图失败 error_type={}', error.__class__.__name__)
            return False

    @staticmethod
    async def wait_and_click_login(
            page,
            deadline,
            headless,
            poll_interval=1.0,
            debug_screenshot_path=None,
            debug_screenshot_interval=5.0,
    ):
        """可视模式等待人工通过验证，然后继续打开登录二维码。"""
        verification_logged = False
        next_debug_screenshot = 0.0
        while time.time() < deadline:
            if page.is_closed():
                raise RuntimeError('登录窗口已关闭')
            try:
                if (
                        debug_screenshot_path
                        and time.monotonic() >= next_debug_screenshot
                ):
                    await DYLoginApi.capture_debug_screenshot(
                        page,
                        debug_screenshot_path,
                    )
                    next_debug_screenshot = (
                        time.monotonic() + debug_screenshot_interval
                    )
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
                    # 优先使用服务器页面中稳定的头部原生按钮结构。
                    clicked = await DYLoginApi._click_login_entry(page)
                    if clicked:
                        logger.info('登录入口已点击，等待二维码弹窗')
                        return
            except BrowserVerificationRequiredError:
                raise
            except Exception:
                # 页面跳转期间上下文可能短暂销毁，继续等待即可。
                pass
            await asyncio.sleep(poll_interval)
        raise TimeoutError('登录超时：未找到登录入口或未完成浏览器验证')

    # 扫码登录并抓 ticket
    async def login_grab_ticket(self, headless=False, timeout=180,
                                qrcode_callback=None,
                                debug_screenshot_path=None,
                                verification_callback=None,
                                verification_command_queue=None,
                                verification_timeout=180):
        """扫码登录并返回完整认证，二维码可通过 bytes 回调获取。"""
        async with async_playwright() as p:
            browser = await self._launch_browser(p, headless)
            try:
                page = await browser.new_page()
                await page.goto(self.home_url)
                await page.wait_for_load_state("load")
                deadline = time.time() + timeout
                await self.wait_and_click_login(
                    page,
                    deadline,
                    headless,
                    debug_screenshot_path=debug_screenshot_path,
                )
                if await self.capture_debug_screenshot(page, debug_screenshot_path):
                    logger.info('登录调试截图已启用 path={}', debug_screenshot_path)
                if qrcode_callback:
                    try:
                        # 二维码慢加载时使用扫码会话的全部剩余时间。
                        qrcode_wait_seconds = max(1.0, deadline - time.time())
                        logger.info(
                            '等待二维码渲染 timeout_seconds={:.1f}',
                            qrcode_wait_seconds,
                        )
                        qrcode_bytes = await self.capture_login_qrcode(
                            page,
                            timeout_seconds=qrcode_wait_seconds,
                        )
                    except Exception:
                        # 二维码定位失败时保留当时页面，便于判断弹窗状态。
                        await self.capture_debug_screenshot(
                            page,
                            debug_screenshot_path,
                        )
                        raise
                    await self._notify_qrcode(qrcode_callback, qrcode_bytes)
                logger.info('扫码登录会话已就绪')
                keys_str = None
                web_protect_str = None
                cookies = {}
                login_cookie_names = ('sessionid', 'sessionid_ss', 'sid_guard', 'uid_tt', 'uid_tt_ss')
                is_logged_in = False
                verification_announced = False
                sms_submitted_at = None
                next_debug_screenshot = time.monotonic() + 5.0
                while time.time() < deadline:
                    await asyncio.sleep(2)
                    if page.is_closed():
                        raise RuntimeError("登录窗口已关闭")
                    if (
                            debug_screenshot_path
                            and time.monotonic() >= next_debug_screenshot
                    ):
                        await self.capture_debug_screenshot(
                            page,
                            debug_screenshot_path,
                        )
                        next_debug_screenshot = time.monotonic() + 5.0

                    body_text = await self._page_body_text(page)
                    identity_verification = await self._is_identity_verification(
                        page,
                        body_text,
                        verification_announced,
                    )
                    if identity_verification and not verification_announced:
                        verification_announced = True
                        # 短信交互开始后单独保留操作时间，不消耗剩余扫码时间。
                        deadline = max(deadline, time.time() + verification_timeout)
                        await self.capture_debug_screenshot(
                            page,
                            debug_screenshot_path,
                        )
                        await self._notify_verification(
                            verification_callback,
                            'verification_required',
                        )

                    command = None
                    if verification_announced and verification_command_queue is not None:
                        try:
                            command = verification_command_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    if command:
                        action = command.get('action')
                        if action in {'request_sms', 'resend_sms'}:
                            await self._notify_verification(
                                verification_callback,
                                'requesting_sms',
                            )
                            try:
                                if action == 'resend_sms':
                                    await self.resend_sms_verification(page)
                                else:
                                    await self.request_sms_verification(page)
                            except Exception:
                                await self._notify_verification(
                                    verification_callback,
                                    (
                                        'waiting_sms_code'
                                        if action == 'resend_sms'
                                        else 'verification_required'
                                    ),
                                    (
                                        'QR_SMS_RESEND_FAILED'
                                        if action == 'resend_sms'
                                        else 'QR_SMS_REQUEST_FAILED'
                                    ),
                                    (
                                        '重新发送短信验证码失败，请重试'
                                        if action == 'resend_sms'
                                        else '请求短信验证码失败，请重试'
                                    ),
                                )
                            else:
                                await self.capture_debug_screenshot(
                                    page,
                                    debug_screenshot_path,
                                )
                                await self._notify_verification(
                                    verification_callback,
                                    'waiting_sms_code',
                                )
                        elif action == 'submit_sms':
                            await self._notify_verification(
                                verification_callback,
                                'verifying_sms',
                            )
                            try:
                                await self.submit_sms_verification(
                                    page,
                                    command.get('code', ''),
                                )
                            except SmsVerificationInteractionError as error:
                                await self._notify_verification(
                                    verification_callback,
                                    'waiting_sms_code',
                                    error.code,
                                    error.safe_message,
                                )
                            except Exception:
                                await self._notify_verification(
                                    verification_callback,
                                    'waiting_sms_code',
                                    'QR_SMS_SUBMIT_FAILED',
                                    '短信验证码提交失败，请重试',
                                )
                            else:
                                sms_submitted_at = time.monotonic()

                    sms_rejected = any(marker in body_text for marker in (
                        '验证码错误',
                        '验证码不正确',
                        '验证码输入错误',
                        '验证码有误',
                        '验证码已过期',
                        '验证码失效',
                        '验证码已使用',
                        '请重新获取验证码',
                    ))
                    verification_stalled = (
                        sms_submitted_at is not None
                        and identity_verification
                        and time.monotonic() - sms_submitted_at >= 10
                    )
                    if sms_submitted_at is not None and (sms_rejected or verification_stalled):
                        sms_submitted_at = None
                        await self._notify_verification(
                            verification_callback,
                            'waiting_sms_code',
                            (
                                'QR_SMS_CODE_REJECTED'
                                if sms_rejected
                                else 'QR_SMS_VERIFICATION_STALLED'
                            ),
                            (
                                '短信验证码错误或已失效，请重新输入'
                                if sms_rejected
                                else '短信验证未完成，请确认验证码后重试'
                            ),
                        )
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
