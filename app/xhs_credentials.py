"""小红书 Cookie 与 remark JSON 的版本化保存和离线恢复。"""
import json
from dataclasses import asdict
from typing import Any

from app.account_store import CredentialFormatError
from xhs_utils.xhs_pc import XHSPcAuth
from xhs_utils.xhs_pc.http import PcHttpClient
from xhs_utils.xhs_pc.state import B1RuntimeState, MnsStageMaterial, PcSessionState, parse_cookie_kv


XHS_CREDENTIAL_TYPE = 'xhs_api_account_v1'


class XhsCredentialCodec:
    @staticmethod
    def metadata(remark: str | None) -> dict:
        if not remark:
            return {}
        try:
            data = json.loads(remark)
        except (TypeError, ValueError) as error:
            raise CredentialFormatError('小红书 remark 不是有效 JSON') from error
        if not isinstance(data, dict) or type(data.get('version')) is not int or data.get('version') != 1:
            raise CredentialFormatError('不支持的小红书状态版本')
        revision = data.get('revision', 0)
        if type(revision) is not int or revision < 0:
            raise CredentialFormatError('小红书凭证修订号无效')
        return data

    def revision(self, remark):
        return self.metadata(remark).get('revision', 0)

    @staticmethod
    def cookie(auth: XHSPcAuth) -> str:
        if not isinstance(auth, XHSPcAuth):
            raise CredentialFormatError('需要小红书 PC 认证对象')
        auth.validate()
        return auth.cookies

    def encode(self, auth, previous_remark=None):
        # 内置模板不重复保存，仅保存实际运行状态及覆盖值。
        local, tab = auth.profile.browser_storage_snapshot()
        data = {
            'version': 1, 'revision': self.revision(previous_remark) + 1,
            'user_id': auth.user_id, 'login_source': auth.login_source,
            'dsl': auth.dsl, 'web_build': auth.profile.web_build,
            'local_storage': local, 'session_storage': tab,
            'host_cookie_state': auth._cookie_store.export_state(),
            'session_state': asdict(auth.profile.session),
            'b1_state': asdict(auth.profile.b1_state), 'fixed_b1': auth.profile.fixed_b1,
            'mns_stages': {name: asdict(stage) for name, stage in auth.profile.mns_stages.items()},
            'rap_fingerprint_hex': auth.profile.rap_fingerprint_hex,
            'web_profile_fields': auth.profile.web_profile_fields,
            'web_profile_i12_seed': auth.profile.web_profile_i12_seed,
            'web_profile_fi': auth.profile.web_profile_fi,
        }
        value = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        if len(value.encode('utf-8')) > 65535:
            raise CredentialFormatError('小红书状态超过 remark TEXT 容量')
        return value

    def decode(self, cookie: str, remark: str | None) -> XHSPcAuth:
        cookies = parse_cookie_kv(cookie)
        if not cookies.get('a1') or not cookies.get('web_session'):
            raise CredentialFormatError('小红书 Cookie 缺少 a1 或 web_session')
        data = self.metadata(remark)
        auth = None
        http_client = PcHttpClient()
        try:
            auth = XHSPcAuth.from_saved_state(
                cookie, http_client=http_client, user_id=data.get('user_id', ''), login_source=data.get('login_source', 'cookie'),
                dsl=data.get('dsl', ''), web_build=data.get('web_build', ''),
                local_storage=data.get('local_storage', {}), session_storage=data.get('session_storage', {}),
                host_cookie_state=data.get('host_cookie_state', {}), b1=data.get('fixed_b1', ''),
                b1_state=B1RuntimeState(**data['b1_state']) if data.get('b1_state') else None,
                rap_fingerprint_hex=data.get('rap_fingerprint_hex', ''),
                web_profile_fields=data.get('web_profile_fields', {}),
                web_profile_i12_seed=data.get('web_profile_i12_seed'), web_profile_fi=data.get('web_profile_fi'),
            )
            if data.get('session_state'):
                auth.profile.session = PcSessionState(**data['session_state'])
                for key in ('loadts', 'dsllt', 'ets', 'mns_seq', 'last_tiga_update_time', 'profile_count', 'sign_count'):
                    value = getattr(auth.profile.session, key)
                    if type(value) is not int or value < 0:
                        raise ValueError('会话计数或时间无效')
            for name, stage in data.get('mns_stages', {}).items():
                auth.profile.mns_stages[name] = MnsStageMaterial.from_mapping({
                    'tier': stage['tier'], 'envConst': stage['env_const'], 'envFpTail': stage['env_fp_tail'],
                    'evidence': stage.get('evidence', ''),
                })
            auth._sync_storage_from_profile()
            auth.login_verified = False
            return auth
        except Exception as error:
            if auth is not None:
                auth.close()
            else:
                http_client.close()
            raise CredentialFormatError('小红书状态无法恢复') from error
