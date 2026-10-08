// BDMS 在 Service Worker 中使用项目提供的浏览器环境补丁和模拟 XHR。
importScripts("vendor/bdms-1.0.1.19.js");

const MSTOKEN_REPORT_URL = "https://mssdk.bytedance.com/web/common?ms_appid=6383";
const MSTOKEN_TTL_MS = 10 * 60 * 1000;
const MSTOKEN_HEADER_RULE_ID = 910001;
const USER_WORK_HEADER_RULE_ID = 910002;
let msTokenCache = { token: "", timestamp: 0 };
let msTokenRequest = null;
let userWorkQueue = Promise.resolve();

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});

chrome.runtime.onStartup.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});

async function setMsTokenRequestHeaders(ttwid, userAgent) {
  const requestHeaders = [
    { header: "Origin", operation: "set", value: "https://www.douyin.com" },
    { header: "Referer", operation: "set", value: "https://www.douyin.com/" },
    { header: "User-Agent", operation: "set", value: userAgent || navigator.userAgent },
  ];
  if (ttwid) {
    requestHeaders.push({
      header: "Cookie",
      operation: "set",
      value: `ttwid=${ttwid}`,
    });
  }

  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [MSTOKEN_HEADER_RULE_ID],
    addRules: [{
      id: MSTOKEN_HEADER_RULE_ID,
      priority: 1,
      action: {
        type: "modifyHeaders",
        requestHeaders,
      },
      condition: {
        urlFilter: "||mssdk.bytedance.com/web/common",
        initiatorDomains: [chrome.runtime.id],
        resourceTypes: ["xmlhttprequest"],
      },
    }],
  });
}

async function clearMsTokenRequestHeaders() {
  try {
    await chrome.declarativeNetRequest.updateSessionRules({
      removeRuleIds: [MSTOKEN_HEADER_RULE_ID],
    });
  } catch {
    // 临时规则可能已被 Service Worker 生命周期清理。
  }
}

function stripCookieMsToken(cookieHeader) {
  return String(cookieHeader || "")
    .split(";")
    .map((part) => part.trim())
    .filter((part) => part && part.split("=", 1)[0].toLowerCase() !== "mstoken")
    .join("; ");
}

async function setUserWorkRequestHeaders(message) {
  const requestHeaders = [
    { header: "Origin", operation: "remove" },
    { header: "Referer", operation: "set", value: message.referrer },
    { header: "User-Agent", operation: "set", value: message.userAgent || navigator.userAgent },
    { header: "Cookie", operation: "set", value: stripCookieMsToken(message.cookieHeader) },
    { header: "Sec-Fetch-Dest", operation: "set", value: "empty" },
    { header: "Sec-Fetch-Mode", operation: "set", value: "cors" },
    { header: "Sec-Fetch-Site", operation: "set", value: "same-origin" },
    { header: "Priority", operation: "set", value: "u=1, i" },
  ];
  if (message.secChUa) {
    requestHeaders.push(
      { header: "Sec-CH-UA", operation: "set", value: message.secChUa },
      { header: "Sec-CH-UA-Mobile", operation: "set", value: "?0" },
      {
        header: "Sec-CH-UA-Platform",
        operation: "set",
        value: `"${message.secChUaPlatform || "Windows"}"`,
      },
    );
  }

  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [USER_WORK_HEADER_RULE_ID],
    addRules: [{
      id: USER_WORK_HEADER_RULE_ID,
      priority: 1,
      action: {
        type: "modifyHeaders",
        requestHeaders,
      },
      condition: {
        urlFilter: "||www.douyin.com/aweme/v1/web/aweme/post/",
        initiatorDomains: [chrome.runtime.id],
        resourceTypes: ["xmlhttprequest"],
      },
    }],
  });
}

async function clearUserWorkRequestHeaders() {
  try {
    await chrome.declarativeNetRequest.updateSessionRules({
      removeRuleIds: [USER_WORK_HEADER_RULE_ID],
    });
  } catch {
    // 临时规则可能已被 Service Worker 生命周期清理。
  }
}

async function fetchUserWorks(message) {
  const url = new URL(message.url);
  if (
    url.origin !== "https://www.douyin.com"
    || url.pathname !== "/aweme/v1/web/aweme/post/"
  ) {
    throw new Error("用户作品请求地址不合法");
  }
  // 与后端一致，作品请求必须已完成双签名；直接发送，不能再次编码。
  if (!url.searchParams.get("a_bogus")
    || !/^\d+$/.test(url.searchParams.get("timestamp") || "")
    || !/^[a-f0-9]{32}$/.test(url.searchParams.get("x-secsdk-web-signature") || "")) {
    throw new Error("用户作品请求缺少有效签名");
  }
  const referrer = new URL(message.referrer);
  if (referrer.protocol !== "https:"
    || !(referrer.hostname === "douyin.com" || referrer.hostname.endsWith(".douyin.com"))
    || !/^\/user\/[^/]+\/?$/.test(referrer.pathname)
    || referrer.username || referrer.password) throw new Error("用户主页地址不合法");

  await setUserWorkRequestHeaders(message);
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await fetch(message.url, {
      method: "GET",
      headers: {
        accept: "application/json, text/plain, */*",
        "accept-language": message.acceptLanguage || "zh-CN,zh;q=0.9,en;q=0.8",
        ...(url.searchParams.get("uifid") ? { uifid: url.searchParams.get("uifid") } : {}),
      },
      credentials: "omit",
      signal: controller.signal,
    });
    return {
      status: response.status,
      finalUrl: response.url,
      pageUrl: "",
      requestReferrer: message.referrer,
      headers: {
        xTtLogId: response.headers.get("x-tt-logid") || "",
      },
      text: await response.text(),
    };
  } finally {
    clearTimeout(timeoutId);
    await clearUserWorkRequestHeaders();
  }
}

async function requestDynamicMsToken({ reportBody, ttwid, userAgent }) {
  if (typeof reportBody !== "string" || !reportBody) {
    throw new Error("缺少 msToken 上报数据");
  }

  await setMsTokenRequestHeaders(ttwid, userAgent);
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 10_000);
  try {
    const response = await fetch(MSTOKEN_REPORT_URL, {
      method: "POST",
      headers: {
        accept: "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "text/plain;charset=UTF-8",
      },
      body: reportBody,
      cache: "no-store",
      credentials: "include",
      signal: controller.signal,
    });
    const token = response.headers.get("x-ms-token") || "";
    if (!token) {
      throw new Error(`mssdk 未返回 x-ms-token（HTTP ${response.status}）`);
    }
    return token;
  } finally {
    clearTimeout(timeoutId);
    await clearMsTokenRequestHeaders();
  }
}

async function getDynamicMsToken(message) {
  const now = Date.now();
  if (
    !message.forceRefresh
    && msTokenCache.token
    && now - msTokenCache.timestamp < MSTOKEN_TTL_MS
  ) {
    return msTokenCache.token;
  }
  if (msTokenRequest) {
    return msTokenRequest;
  }

  msTokenRequest = requestDynamicMsToken(message)
    .then((token) => {
      msTokenCache = { token, timestamp: Date.now() };
      return token;
    })
    .finally(() => {
      msTokenRequest = null;
    });
  return msTokenRequest;
}

async function handleMessage(message) {
  if (message?.type === "SIGN_BDMS") {
    const { apiPath, query, method = "GET", body, tailLength = null } = message;
    const options = { method };
    if (typeof body === "string") options.body = body;
    const fullSignature = bdmsNode.generateABogus(
      `https://www.douyin.com${apiPath}?${query}`,
      options,
    );
    return {
      ok: true,
      signature: Number.isInteger(tailLength)
        ? fullSignature.slice(-tailLength)
        : fullSignature,
    };
  }

  if (message?.type === "GET_DYNAMIC_MSTOKEN") {
    const token = await getDynamicMsToken(message);
    return { ok: true, token };
  }

  if (message?.type === "FETCH_USER_WORKS") {
    // 临时请求头规则是共享资源，多侧栏请求必须顺序执行。
    const task = userWorkQueue.then(() => fetchUserWorks(message));
    userWorkQueue = task.catch(() => {});
    return { ok: true, response: await task };
  }
  throw new Error("不支持的插件消息");
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!["SIGN_BDMS", "GET_DYNAMIC_MSTOKEN", "FETCH_USER_WORKS"].includes(message?.type)) {
    return false;
  }
  handleMessage(message).then(sendResponse).catch((error) => {
    sendResponse({
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    });
  });
  return true;
});
