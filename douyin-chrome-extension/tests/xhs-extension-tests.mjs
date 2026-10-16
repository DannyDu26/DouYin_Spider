import assert from "node:assert/strict";
import { inspectXhsLoginCookies, parseXhsPage, normalizeXhsId } from "../lib/xhs-api.js";
import { signXhs, XhsSignError } from "../lib/xhs-sign.js";
import { buildXhsRapParam } from "../lib/xhs-rap.js";

// 验证小红书页面识别、Cookie ID 校验和本地反爬签名输出。
assert.deepEqual(
  parseXhsPage("https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa?xsec_token=t"),
  { kind: "note", noteId: "aaaaaaaaaaaaaaaaaaaaaaaa", url: "https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa?xsec_token=t" },
);
assert.equal(normalizeXhsId("bbbbbbbbbbbbbbbbbbbbbbbb", "用户 ID"), "bbbbbbbbbbbbbbbbbbbbbbbb");
assert.throws(() => normalizeXhsId("bad", "笔记 ID"), /24 位十六进制/);
const login = inspectXhsLoginCookies([
  { name: "a1", value: "a1-value", domain: ".xiaohongshu.com" },
  { name: "web_session", value: "session-value", domain: ".xiaohongshu.com" },
  { name: "webBuild", value: "6.57.6", domain: ".xiaohongshu.com" },
]);
assert.equal(login.ready, true);
assert.equal(login.cookieMap.webBuild, "6.57.6");
const normalizedLogin = inspectXhsLoginCookies([
  { name: "A1", value: "a1-value", domain: ".xiaohongshu.com" },
  { name: "WEB_SESSION", value: "session-value", domain: ".xiaohongshu.com" },
]);
assert.equal(normalizedLogin.cookieMap.a1, "a1-value");
assert.equal(normalizedLogin.ready, true);
assert.equal(inspectXhsLoginCookies([{ name: "web_session", value: "session-value" }]).ready, false);

const headers = signXhs({
  api: "/api/sns/web/v2/search/notes",
  data: { keyword: "猫", page: 1 },
  cookieMap: { a1: "a".repeat(64), loadts: "1780000000000", _dsl: "1780000000001" },
});
assert.match(headers["x-s"], /^XYS_/);
assert.match(headers["x-t"], /^\d+$/);
assert.ok(headers["x-s-common"].length > 100);
assert.throws(
  () => signXhs({ api: "/api/test", cookieMap: {} }),
  (error) => error instanceof XhsSignError && error.code === "XHS_COOKIE_MISSING",
);
const rap = await buildXhsRapParam("/api/sns/web/v2/search/notes", { keyword: "猫", page: 1 });
assert.match(rap, /^[A-Za-z0-9+/=]{100,}$/);
console.log("xhs extension tests: PASS");
