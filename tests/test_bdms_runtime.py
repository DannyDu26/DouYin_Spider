import json
from pathlib import Path
import shutil
import subprocess

import pytest

from utils.dy_util import generate_a_bogus_bdms


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js")
SCRIPT = Path(__file__).resolve().parents[1] / "js" / "bdms_1.0.1.19_fix.js"


def test_bdms_loads_without_native_atob():
    """移除原生 atob，验证旧版 Node.js 加载和二进制解码。"""
    result = subprocess.run(
        [shutil.which("node"), "-e", r'''
const assert = require("assert");
delete globalThis.atob;
delete globalThis.performance;
const bdms = require(process.argv[1]);
assert.strictEqual(atob(" AP+A/w==\n"), "\x00\xff\x80\xff");
assert.strictEqual(atob("YQ"), "a");
assert.throws(() => atob("a"), { name: "InvalidCharacterError" });
assert.throws(() => atob("!!!!"), { name: "InvalidCharacterError" });
assert.strictEqual(window.atob, globalThis.atob);
const signature = bdms.generateABogus(
    "https://www.douyin.com/aweme/v1/web/general/search/single/?aid=6383&keyword=test",
    { method: "GET" }
);
process.stdout.write(JSON.stringify({ length: signature.length }));
''', str(SCRIPT)],
        capture_output=True, text=True, timeout=15, check=True,
    )
    assert json.loads(result.stdout)["length"] == 192


@pytest.mark.parametrize("tail_length, expected_length", [(None, 192), (87, 87)])
def test_python_bdms_entrypoint(tail_length, expected_length):
    """实际启动 Node.js，覆盖完整签名和末尾签名两条路径。"""
    signature = generate_a_bogus_bdms(
        "/aweme/v1/web/general/search/single/",
        "aid=6383&keyword=test",
        tail_length=tail_length,
    )
    assert len(signature) == expected_length
