"""使用原项目生成的固定向量验证迁移结果。"""
import base64
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from app.xhs_credentials import XhsCredentialCodec
from app.xhs_service import check_xhs_runtime
from xhs_utils.xhs_pc.runtime import run_signer, create_web_ssk_handshake
from test_xhs_api import make_auth


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/xhs_signing_golden.json').read_text(encoding='utf-8'))


def node_call(relative, method, payload):
    # JSON 通过标准输入传入，固定测试材料不包含真实凭证。
    program = "const fs=require('fs'); const m=require(process.argv[1]); process.stdout.write(JSON.stringify(m[process.argv[2]](JSON.parse(fs.readFileSync(0,'utf8')))))"
    result = subprocess.run(['node', '-e', program, str(ROOT / relative), method],
                            input=json.dumps(payload, ensure_ascii=False), text=True,
                            capture_output=True, encoding='utf-8', check=True, timeout=30)
    return json.loads(result.stdout)


def decode_xs(value):
    alphabet = 'ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5'
    standard = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    return json.loads(base64.b64decode(value[4:].translate(str.maketrans(alphabet, standard))))


@pytest.mark.parametrize('vector', FIXTURE['sign_vectors'], ids=lambda value: value['input']['tier'])
def test_signatures_match_original_project(vector):
    assert node_call('xhs_utils/xhs_core/js/sign.js', 'signFull', vector['input']) == vector['expected']


def test_b1_and_profile_match_original_project():
    b1 = node_call('xhs_utils/xhs_core/js/b1.js', 'generateB1', FIXTURE['b1']['options'])
    if isinstance(b1, dict):
        b1 = b1['b1']
    profile = node_call('xhs_utils/xhs_pc/js/profile.js', 'generateProfileData', FIXTURE['profile']['options'])
    assert hashlib.sha256(b1.encode()).hexdigest() == FIXTURE['b1']['sha256']
    assert hashlib.sha256(profile.encode()).hexdigest() == FIXTURE['profile']['sha256']


def test_all_js_and_templates_transferred_intact():
    for relative, expected in FIXTURE['asset_manifest'].items():
        assert hashlib.sha256((ROOT / 'xhs_utils' / relative).read_bytes()).hexdigest() == expected


def test_restored_ssk_is_used_by_python_signing_adapter():
    original = make_auth()
    original.profile.update_storage(local_storage={
        'webSsk': json.dumps({'xhs-pc-web': base64.b64encode(bytes(range(48))).decode()}),
    })
    codec = XhsCredentialCodec()
    restored = codec.decode(original.cookies, codec.encode(original))
    context = restored.next_sign_context('/api/sns/web/v1/feed')
    signed = run_signer('/api/sns/web/v1/feed', {'source_note_id': 'a' * 24},
                        a1=restored.a1, cookie=restored.sign_cookie,
                        b1=restored.current_b1(context['now']), dsl_pair=restored.dsl_pair,
                        tier=context['tier'], sign_context=context)
    envelope = decode_xs(signed['xs'])
    assert 'xhs-pc-web' in envelope['x6'] and 'xhs-pc-web' in envelope['x7']
    del context['webSsk']
    without = run_signer('/api/sns/web/v1/feed', {}, a1=restored.a1,
                         cookie=restored.sign_cookie, b1='', dsl_pair=restored.dsl_pair,
                         tier=context['tier'], sign_context=context)
    assert 'x6' not in decode_xs(without['xs']) and 'x7' not in decode_xs(without['xs'])
    original.close()
    restored.close()


def test_node_dependencies_and_ssk_handshake_available():
    check_xhs_runtime()
    result = create_web_ssk_handshake()
    assert result['private_key_base64'] and result['client_public_key_base64']
