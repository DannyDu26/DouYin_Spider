import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { COOKIE_SYNC_SERVER_URL, readSyncCookies, syncBrowserCookie, syncXhsCookie } from "../lib/cookie-sync.js";
import { readXhsPageCookies } from "../lib/xhs-cookies.js";

// 直接校验发布 HTML，防止脚本通过而浏览器表单的正则无法编译。
const panelHtml = readFileSync(new URL("../sidepanel.html", import.meta.url), "utf8");
const accountPattern = panelHtml.match(/name="accountId"[^>]*\bpattern="([^"]+)"/)?.[1];
assert.ok(accountPattern, "账号别名表单必须声明 pattern");
for (const flag of ["u", "v"]) {
  const validator = new RegExp(`^(?:${accountPattern})$`, flag);
  for (const value of ["xhs_01", "xhs-01", "_-", "a".repeat(64)]) assert.ok(validator.test(value), value);
  for (const value of ["", "XHS", "xhs 01", "xhs/01", "xhs\\01", "a".repeat(65)]) assert.ok(!validator.test(value), value);
}

// 只模拟扩展和网络边界，不接触真实浏览器 Cookie。
const calls = [];
const cookie = (name, value, extra = {}) => ({ name, value, domain: ".xiaohongshu.com", storeId: "current", ...extra });
globalThis.chrome = {
  permissions: { request: async (options) => { calls.push(["permission", options]); return true; } },
  tabs: { get: async () => ({ url: "https://www.xiaohongshu.com/explore" }) },
  cookies: {
    getAllCookieStores: async () => [{ id: "current", tabIds: [7] }, { id: "other", tabIds: [8] }],
    getAll: async (query) => {
      calls.push(["cookies", query]);
      return [cookie("a1", "current-device"), cookie("web_session", "current-session"),
        cookie("webBuild", "6.47.2"), cookie("foreign", "bad", { domain: "evilxiaohongshu.com" }),
        cookie("foreign-store", "bad", { storeId: "other" }),
        cookie("partition", "bad", { partitionKey: { topLevelSite: "https://elsewhere.example" } })];
    },
  },
};
globalThis.fetch = async (url, options) => {
  calls.push(["fetch", url, options]);
  return { ok: true, status: 200, json: async () => ({ success: true, data: { status: "succeeded", account_id: "xhs_01" } }) };
};
// 历史设置或调用方附加地址也不能改变固定接收方。
const config = { serverUrl: "https://evil.example/", accountId: "xhs_01", token: "test-secret", tabId: 7 };
assert.equal(COOKIE_SYNC_SERVER_URL, "https://crawler.xoyo.com");
assert.deepEqual(await syncXhsCookie(config), { accountId: "xhs_01", baseUrl: "https://crawler.xoyo.com" });
assert.equal(calls[0][0], "permission");
assert.deepEqual(calls[0][1], { origins: ["https://crawler.xoyo.com/*"] });
assert.ok(calls.filter(([type]) => type === "cookies").every(([, query]) => query.storeId === "current"));
const sent = calls.find(([type]) => type === "fetch");
assert.equal(sent[1], "https://crawler.xoyo.com/api/v1/xhs/auth/cookie/import");
assert.equal(sent[2].headers["X-XHS-Import-Token"], "test-secret");
assert.equal(sent[2].redirect, "error");
assert.equal(sent[2].credentials, "omit");
assert.deepEqual(JSON.parse(sent[2].body), { account_id: "xhs_01", cookie: "a1=current-device; web_session=current-session; webBuild=6.47.2" });

calls.length = 0;
chrome.permissions.request = async () => false;
await assert.rejects(syncXhsCookie(config), /未获得/);
assert.equal(calls.length, 0);
chrome.permissions.request = async () => true;
await assert.rejects(syncXhsCookie({ ...config, accountId: "bad alias" }), /账号别名/);
await assert.rejects(readSyncCookies(8), /a1|web_session/);
await assert.rejects(readSyncCookies(99), /Cookie Store/);
chrome.tabs.get = async () => ({ url: "https://evilxiaohongshu.com/" });
await assert.rejects(readSyncCookies(7), /小红书标签页/);
chrome.tabs.get = async () => ({ url: "https://www.xiaohongshu.com/" });
globalThis.fetch = async () => ({ ok: false, status: 401, json: async () => ({ error: { code: "XHS_COOKIE_IMPORT_UNAUTHORIZED", message: "secret-cookie-value" } }) });
await assert.rejects(syncXhsCookie(config), (error) => /同步密钥无效/.test(error.message) && !error.message.includes("secret-cookie-value"));
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ success: true, data: { status: "starting", account_id: "xhs_01" } }) });
await assert.rejects(syncXhsCookie(config), /未确认/);

// URL 查询漏读或部分接口失败时，主域查询仍能取到当前 Store 的登录凭证。
chrome.cookies.getAll = async (query) => {
  assert.equal(query.storeId, "current");
  if (query.url?.includes("edith.")) throw new Error("fixture-query-failure");
  if (query.domain) return [cookie("a1", "domain-device"), cookie("web_session", "http-only-session", { httpOnly: true })];
  return [];
};
assert.equal(await readSyncCookies(7), "a1=domain-device; web_session=http-only-session");

// 首个空值不应遮住后续有效值，关键名称大小写与登录检测保持一致。
chrome.cookies.getAll = async (query) => query.domain
  ? [cookie("A1", "valid-device"), cookie("WEB_SESSION", "valid-session")]
  : [cookie("a1", ""), cookie("web_session", "")];
assert.equal(await readSyncCookies(7), "a1=valid-device; web_session=valid-session");

// 使用真实页面读取函数验证原始编码保留、a1 兜底及伪 Cookie 排除。
globalThis.location = new URL("https://www.xiaohongshu.com/explore");
globalThis.document = { cookie: "a1=page%2Bdevice; extra=raw%3Dvalue" };
globalThis.window = { _dsl: "1720000000000" };
chrome.scripting = { executeScript: async ({ target, func }) => {
  assert.equal(target.tabId, 7);
  return [{ result: func() }];
} };
const pageCookies = await readXhsPageCookies(7);
assert.equal(pageCookies.find(({ name }) => name === "extra").value, "raw%3Dvalue");
chrome.cookies.getAll = async () => [cookie("web_session", "api-only-session", { httpOnly: true })];
assert.equal(await readSyncCookies(7), "web_session=api-only-session; a1=page%2Bdevice");

// 页面可以补 a1，但页面的伪 web_session 不能冒充 HttpOnly 会话。
document.cookie = "a1=page-device; web_session=page-secret-session";
chrome.cookies.getAll = async () => [cookie("a1", "api-device")];
await assert.rejects(readSyncCookies(7), (error) => error.message.startsWith("缺少 web_session；")
  && error.message.includes("已读取 Cookie 名称：a1")
  && !error.message.includes("api-device") && !error.message.includes("page-secret-session"));

// 页面不可访问时不影响完整 API 凭证；全部 API 失败时提示权限而非误判未登录。
chrome.scripting.executeScript = async () => { throw new Error("fixture-script-denied"); };
chrome.cookies.getAll = async () => [cookie("web_session", "secret-session")];
await assert.rejects(readSyncCookies(7), (error) => error.message.startsWith("缺少 a1；") && !error.message.includes("secret-session"));
chrome.cookies.getAll = async () => { throw new Error("fixture-api-denied-secret"); };
await assert.rejects(readSyncCookies(7), (error) => error.message.includes("Cookie API 查询失败") && !error.message.includes("fixture-api-denied-secret"));
globalThis.location = new URL("https://unrelated.example/");
chrome.scripting.executeScript = async ({ func }) => [{ result: func() }];
assert.deepEqual(await readXhsPageCookies(7), []);

// 抖音沿用同一读取和上传流程，但只读取抖音域，使用独立路由与密钥头。
globalThis.location = new URL("https://www.douyin.com/");
chrome.tabs.get = async () => ({ url: location.href });
chrome.scripting.executeScript = async ({ func, args = [] }) => [{ result: func(...args) }];
chrome.cookies.getAll = async (query) => {
  assert.equal(query.storeId, "current");
  assert.ok(query.domain === "douyin.com" || new URL(query.url).hostname.endsWith("douyin.com"));
  return [cookie("sessionid", "douyin-secret-session", { domain: ".douyin.com", httpOnly: true }),
    cookie("s_v_web_id", "verify-real-fp", { domain: ".douyin.com" }),
    cookie("web_session", "xhs-secret-must-not-upload")];
};
let douyinRequest;
globalThis.fetch = async (url, options) => {
  douyinRequest = { url, ...options };
  return { ok: true, status: 200, json: async () => ({ success: true, data: { status: "succeeded", account_id: "douyin_01" } }) };
};
await syncBrowserCookie({ ...config, platform: "douyin", accountId: "douyin_01" });
assert.equal(douyinRequest.url, "https://crawler.xoyo.com/api/v1/douyin/auth/cookie/import");
assert.equal(douyinRequest.headers["X-Douyin-Import-Token"], "test-secret");
assert.equal(douyinRequest.headers["X-XHS-Import-Token"], undefined);
assert.deepEqual(JSON.parse(douyinRequest.body), { account_id: "douyin_01", cookie: "sessionid=douyin-secret-session; s_v_web_id=verify-real-fp" });
await assert.rejects(syncXhsCookie(config), /小红书标签页/);
await assert.rejects(syncBrowserCookie({ ...config, platform: "invalid" }), /不支持/);

// 页面可补充指纹；页面和 Cookie 都没有时，复用传入指纹或生成备用指纹。
chrome.cookies.getAll = async () => [cookie("sessionid_ss", "session-only", { domain: ".douyin.com" })];
document.cookie = "s_v_web_id=verify-page-fp";
assert.equal(await readSyncCookies(7, "douyin"), "sessionid_ss=session-only; s_v_web_id=verify-page-fp");
document.cookie = "";
assert.equal(await readSyncCookies(7, "douyin", "verify-existing-fp"), "sessionid_ss=session-only; s_v_web_id=verify-existing-fp");
assert.match(await readSyncCookies(7, "douyin"), /^sessionid_ss=session-only; s_v_web_id=verify_/);
chrome.cookies.getAll = async () => [];
await assert.rejects(readSyncCookies(7, "douyin"), /缺少 sessionid/);
console.log("Cookie sync tests passed");
