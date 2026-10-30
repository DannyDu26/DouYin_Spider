"""小红书六项只读业务接口，共用原项目的签名与 HTTP 链路。"""
from urllib.parse import parse_qs, urlparse

from xhs_apis.errors import XhsApiError, XhsAuthenticationError, XhsRiskControlError
from xhs_apis.request_policy import XhsRequestPolicy, retry_after_seconds
from xhs_utils.http_util import REQUEST_TIMEOUT
from xhs_utils.xhs_pc import XHSPcAuth
from xhs_utils.xhs_pc.params import (
    build_pc_business_headers, generate_request_params, generate_x_rap_param,
    generate_search_id, generate_search_session_id, splice_str,
)


class XHS_Apis:
    def __init__(self, auth: XHSPcAuth, request_policy=None):
        self.auth = auth
        self.http = auth.http_client
        self.base_url = auth.origin('api')
        self.so_base_url = auth.origin('search')
        self.request_policy = request_policy if request_policy is not None else XhsRequestPolicy.from_env()

    def bootstrap(self):
        """只接受正式登录用户，禁止将访客加入业务账号池。"""
        result = self.get_user_me()
        if result.get('guest') is not False or not result.get('user_id'):
            raise XhsAuthenticationError()
        self.auth.set_user_id(result['user_id'])
        self.auth.login_verified = True
        return self

    def _request_params(self, api, data='', method='POST', target_origin=None):
        # 签名、请求体序列化与头顺序沿用 PC 逆向实现。
        self.auth.validate()
        path = api.split('?', 1)[0]
        with_xy = path == '/api/sns/web/v1/feed'
        context = self.auth.next_sign_context(api)
        headers, cookies, body = generate_request_params(
            self.auth.cookies, api, data, method, user_id=self.auth.user_id,
            b1=self.auth.current_b1(context['now']), dsl_pair=self.auth.dsl_pair,
            doc_cookie=self.auth.sign_cookie, with_xy_direction=with_xy,
            tier=context['tier'], sign_context=context, include_client_hints=False,
        )
        headers.pop('x-mns', None)
        if path == '/api/sns/web/v2/user/me':
            headers.update({'cache-control': 'no-cache', 'pragma': 'no-cache'})
        if path in {'/api/sns/web/v1/feed', '/api/sns/web/v1/user_posted', '/api/sns/web/v2/search/notes'}:
            headers['x-rap-param'] = generate_x_rap_param(
                api, body or '', app_id=self.auth.profile.release['appId'],
                fingerprint_hex=self.auth.profile.rap_fingerprint_hex,
            )
            if not headers['x-rap-param']:
                raise XhsApiError('XHS_SIGN_ERROR', '小红书 RAP 签名生成失败')
        origin = target_origin or self.base_url
        cookies = self.auth.cookies_for_url(origin + api, cookies)
        headers = build_pc_business_headers(headers, cookies, api=api, method=method)
        headers['user-agent'] = context['userAgent']
        return headers, cookies, body

    @staticmethod
    def decode_response(response):
        """按稳定状态信号分类，不暴露上游正文和凭证。"""
        status = response.status_code
        if status == 401:
            raise XhsAuthenticationError()
        if status in {429, 461, 471}:
            raise XhsRiskControlError(f'http_{status}', retry_after_seconds(response))
        if status >= 400:
            raise XhsApiError('XHS_UPSTREAM_ERROR', '小红书上游请求失败', details={'http_status': status})
        try:
            result = response.json()
        except (ValueError, TypeError) as error:
            raise XhsApiError('XHS_INVALID_RESPONSE', '小红书上游未返回有效 JSON') from error
        if not isinstance(result, dict):
            raise XhsApiError('XHS_INVALID_RESPONSE', '小红书上游响应结构异常')
        if result.get('success') is not True:
            code = result.get('code')
            if code == -100:
                raise XhsAuthenticationError()
            # 业务错误保留数字码，不根据错误文案猜测账号失效。
            details = {'upstream_code': code} if isinstance(code, int) else None
            raise XhsApiError('XHS_UPSTREAM_ERROR', '小红书上游拒绝当前请求', details=details)
        data = result.get('data')
        if not isinstance(data, dict):
            raise XhsApiError('XHS_INVALID_RESPONSE', '小红书上游响应缺少 data 对象')
        return data

    def _request(self, api, data='', method='GET', origin=None):
        target = origin or self.base_url
        try:
            # 登录校验独立经过节流，避免在签名锁内递归请求。
            if api.split('?', 1)[0] == '/api/sns/web/v1/feed' and not self.auth.user_id:
                self.bootstrap()

            def send():
                headers, cookies, body = self._request_params(api, data, method, target)
                kwargs = dict(headers=headers, cookies=cookies, proxies=self.auth.proxies, timeout=REQUEST_TIMEOUT)
                if method != 'GET':
                    kwargs['data'] = body.encode('utf-8') if isinstance(body, str) else body
                response = self.http.request(method, target + api, **kwargs)
                self.auth.merge_response_cookies(response)
                return response

            # 六项业务接口均为只读，503 可在有限次数内安全重试。
            response = self.request_policy.execute(send)
            return self.decode_response(response)
        except XhsApiError:
            raise
        except Exception as error:
            raise XhsApiError('XHS_UPSTREAM_ERROR', '小红书请求或签名执行失败') from error

    def get_user_me(self):
        return self._request('/api/sns/web/v2/user/me')

    def get_user_info(self, user_id):
        return self._request(splice_str('/api/sns/web/v1/user/otherinfo', {'target_user_id': user_id}))

    def get_user_note_info(self, user_id, cursor='', xsec_token='', xsec_source='pc_search'):
        return self._request(splice_str('/api/sns/web/v1/user_posted', {
            'num': '30', 'cursor': cursor, 'user_id': user_id,
            'image_formats': 'jpg,webp,avif', 'xsec_token': xsec_token, 'xsec_source': xsec_source,
        }))

    def get_note_info(self, url):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        return self._request('/api/sns/web/v1/feed', {
            'source_note_id': parsed.path.rstrip('/').split('/')[-1],
            'image_formats': ['jpg', 'webp', 'avif'], 'extra': {'need_body_topic': '1'},
            'xsec_source': query.get('xsec_source', ['pc_search'])[-1],
            'xsec_token': query.get('xsec_token', [''])[-1],
        }, method='POST')

    def search_note(self, keyword, page=1, search_id=None, search_session_id=None,
                    sort='general', note_type=0, note_time=0, note_range=0, pos_distance=0, geo=''):
        # 兼容 string 类型的 page 参数并转换为整数
        if isinstance(page, str) and page.strip().isdigit():
            page = int(page.strip())
        # 同一次搜索的 ID 和会话标识由调用方在翻页时续传。
        data = {
            'keyword': keyword, 'page': page, 'page_size': 20,
            'search_id': search_id or generate_search_id(), 'sort': sort, 'note_type': note_type,
            'ext_flags': [], 'geo': geo, 'image_formats': ['jpg', 'webp', 'avif'],
            'session_id': search_session_id or generate_search_session_id(),
        }
        if note_time or note_range or pos_distance:
            data['filters'] = [
                {'tags': [sort], 'type': 'sort_type'},
                {'tags': [['不限', '视频笔记', '普通笔记'][note_type]], 'type': 'filter_note_type'},
                {'tags': [['不限', '一天内', '一周内', '半年内'][note_time]], 'type': 'filter_note_time'},
                {'tags': [['不限', '已看过', '未看过', '已关注'][note_range]], 'type': 'filter_note_range'},
                {'tags': [['不限', '同城', '附近'][pos_distance]], 'type': 'filter_pos_distance'},
            ]
        return self._request('/api/sns/web/v2/search/notes', data, 'POST', self.so_base_url)

    def get_note_out_comment(self, note_id, cursor, xsec_token):
        return self._request(splice_str('/api/sns/web/v2/comment/page', {
            'note_id': note_id, 'cursor': cursor, 'top_comment_id': '',
            'image_formats': 'jpg,webp,avif', 'xsec_token': xsec_token,
        }))

    def get_note_inner_comment(self, note_id, root_comment_id, cursor, xsec_token):
        return self._request(splice_str('/api/sns/web/v2/comment/sub/page', {
            'note_id': note_id, 'root_comment_id': root_comment_id, 'num': '10', 'cursor': cursor,
            'image_formats': 'jpg,webp,avif', 'top_comment_id': '', 'xsec_token': xsec_token,
        }))
