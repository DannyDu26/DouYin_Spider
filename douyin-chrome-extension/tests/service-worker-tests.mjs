import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { signWebUrl } from "../lib/web-sign.js";

const rules = [];
const requests = [];
let active = 0;
let peak = 0;
let failNext = false;
const context = vm.createContext({
  URL, AbortController, setTimeout, clearTimeout,
  navigator: { userAgent: "test-agent" },
  importScripts() {},
  chrome: {
    runtime: {
      id: "test-extension",
      onInstalled: { addListener() {} },
      onStartup: { addListener() {} },
      onMessage: { addListener() {} },
    },
    declarativeNetRequest: {
      async updateSessionRules(rule) { rules.push(rule); },
    },
  },
  async fetch(url, options) {
    requests.push({ url, options });
    active += 1;
    peak = Math.max(peak, active);
    try {
      await new Promise((resolve) => setTimeout(resolve, 5));
      if (failNext) { failNext = false; throw new Error("network failure"); }
      return { status: 200, url, headers: { get() { return ""; } }, async text() { return "{}"; } };
    } finally { active -= 1; }
  },
});
vm.runInContext(await readFile(new URL("../service-worker.js", import.meta.url), "utf8"), context);
const message = {
  type: "FETCH_USER_WORKS",
  url: signWebUrl("https://www.douyin.com/aweme/v1/web/aweme/post/?a_bogus=A%2BB%3D&uifid=test-uifid&verifyFp=fp"),
  referrer: "https://www.douyin.com/user/sec-user?from_tab_name=main",
  cookieHeader: "sessionid=test-session; msToken=old-token; UIFID=test-uifid",
};
await context.handleMessage(message);
assert.equal(requests[0].url, message.url); // 发送原始签名 URL，不重排参数。
assert.equal(requests[0].options.headers.uifid, "test-uifid");
assert.equal(requests[0].options.headers["cache-control"], undefined);
assert.equal(requests[0].options.credentials, "omit");
const installed = rules.find((rule) => rule.addRules)?.addRules[0];
assert.equal(installed.condition.initiatorDomains[0], "test-extension");
assert.equal(installed.action.requestHeaders.find((item) => item.header === "Cookie").value,
  "sessionid=test-session; UIFID=test-uifid");
assert.equal(rules.at(-1).removeRuleIds[0], 910002);

await assert.rejects(context.handleMessage({ ...message,
  url: "https://www.douyin.com/aweme/v1/web/aweme/post/?a_bogus=unsigned",
}), /缺少有效签名/);
await assert.rejects(context.handleMessage({ ...message,
  url: message.url.replace("www.douyin.com", "example.com"),
}), /请求地址不合法/);
assert.equal(requests.length, 1);

// 多侧栏并发时，前一次清理规则后才安装下一次的 Cookie 和 Referer。
await Promise.all([context.handleMessage(message), context.handleMessage(message)]);
assert.equal(peak, 1);
failNext = true;
await assert.rejects(context.handleMessage(message), /network failure/);
assert.equal(rules.at(-1).removeRuleIds[0], 910002);
assert.equal((await context.handleMessage(message)).ok, true);
console.log("service-worker tests: PASS");
