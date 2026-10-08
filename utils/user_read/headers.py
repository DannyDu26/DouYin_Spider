"""与成功项目一致的浏览器请求头。"""
from enum import Enum
from .fingerprint import get_profile

class HeaderType(Enum):
    GET = 'GET'
    DOC = 'DOC'
    POST = 'POST'
    FORM = 'FORM'
    PROTOBUF = 'PROTOBUF'

class Header:
    def __init__(self):
        self.headers = {}

    def set_header(self, key, value):
        self.headers[key] = value
        return self

    def set_referer(self, url):
        return self.set_header('referer', url)

    def with_uifid(self, auth):
        # UIFID 同时出现在请求头和查询参数中。
        value = auth.cookie.get('UIFID', '')
        if value:
            self.set_header('uifid', value)
        return self

    def get(self):
        return self.headers

class HeaderBuilder:
    @staticmethod
    def build(header_type):
        """XHR 请求头，对齐浏览器实录。

        `sec-ch-ua` / `-mobile` / `-platform` **必须发**：这三个一度被删掉，
        依据是 CDP `Network.requestWillBeSent` 里看不到它们 —— 但那个事件只给
        页面 JS 设的头，浏览器网络层后加的要看 `requestWillBeSentExtraInfo`。
        补上 extraInfo 重抓后确认，主站 aweme XHR 是**带**这三个的。
        同理 `accept-language` / `priority` / `sec-fetch-*` 也确实要发。

        不发的是 `cache-control` / `pragma`（实录里确实没有）。
        `accept-encoding` / `cookie` / `content-length` 由 HTTP 层自己补。
        """
        profile = get_profile()
        header = Header()
        header.set_header('user-agent', profile['ua'])
        if header_type == HeaderType.POST:
            header.set_header('accept', '*/*')
            header.set_header('content-type', 'application/json; charset=UTF-8')
        elif header_type == HeaderType.FORM:
            header.set_header('accept', 'application/json, text/plain, */*')
            header.set_header('content-type', 'application/x-www-form-urlencoded; charset=UTF-8')
        elif header_type == HeaderType.PROTOBUF:
            header.set_header('accept', 'application/x-protobuf')
            header.set_header('content-type', 'application/x-protobuf')
        elif header_type == HeaderType.GET:
            header.set_header('accept', 'application/json, text/plain, */*')
        elif header_type == HeaderType.DOC:
            header = Header()
            h = {
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'accept-language': profile['accept_language'],
                'cache-control': 'no-cache',
                'cookie': '',
                'pragma': 'no-cache',
                'priority': 'u=0, i',
                'sec-ch-ua': profile['sec_ch_ua'],
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': profile['sec_ch_ua_platform'],
                'sec-fetch-dest': 'document',
                'sec-fetch-mode': 'navigate',
                'sec-fetch-site': 'none',
                'sec-fetch-user': '?1',
                'upgrade-insecure-requests': '1',
                'user-agent': profile['ua']
            }
            header.headers.update(h)
            return header
        # 非 DOC 的 XHR 统一补上这几个，顺序照浏览器
        header.set_header('sec-ch-ua', profile['sec_ch_ua'])
        header.set_header('sec-ch-ua-mobile', '?0')
        header.set_header('sec-ch-ua-platform', profile['sec_ch_ua_platform'])
        header.set_header('accept-language', profile['accept_language'])
        header.set_header('priority', 'u=1, i')
        header.set_header('sec-fetch-dest', 'empty')
        header.set_header('sec-fetch-mode', 'cors')
        header.set_header('sec-fetch-site', 'same-origin')
        return header
