// 手动同步登录态：仅在点击时读取当前 Cookie Store，不保存凭证。
import { readPlatformPageCookies } from "./xhs-cookies.js";
import { generateVerifyFingerprint } from "./core.js";

export const COOKIE_SYNC_SERVER_URL = "https://crawler.xoyo.com";
// 固定接收方，不读取表单或历史设置中的服务地址。
const SYNC_PLATFORMS = {
  xhs: {
    name: "小红书", domain: "xiaohongshu.com", route: "xhs", header: "X-XHS-Import-Token",
    api: "https://edith.xiaohongshu.com/api/sns/web/v2/user/me", fingerprint: "a1", sessions: ["web_session"],
  },
  douyin: {
    name: "抖音", domain: "douyin.com", route: "douyin", header: "X-Douyin-Import-Token",
    api: "https://www.douyin.com/aweme/v1/web/query/user/", fingerprint: "s_v_web_id",
    sessions: ["sessionid", "sessionid_ss", "sid_guard", "uid_tt", "uid_tt_ss"],
  },
};

function syncPlatform(platform) {
  if (!Object.hasOwn(SYNC_PLATFORMS, platform)) throw new Error("不支持的同步平台");
  return SYNC_PLATFORMS[platform];
}

export async function readSyncCookies(tabId, platform = "xhs", fingerprint = "") {
  const config = syncPlatform(platform);
  const tab = await chrome.tabs.get(tabId);
  const url = new URL(tab.url);
  if (url.protocol !== "https:"
    || !(url.hostname === config.domain || url.hostname.endsWith(`.${config.domain}`))) {
    throw new Error(`请先打开已登录的${config.name}标签页`);
  }
  const stores = await chrome.cookies.getAllCookieStores();
  const storeId = stores.find((store) => store.tabIds.includes(tabId))?.id;
  if (!storeId) throw new Error("无法识别当前标签页的 Cookie Store，请刷新页面后重试");
  // 保留接口优先级，同时覆盖当前路径、主域和子域；始终限定当前 Store。
  const queries = [
    { url: config.api },
    { url: tab.url },
    { url: `https://www.${config.domain}/` },
    { url: `https://${config.domain}/` },
    { domain: config.domain },
  ];
  const results = await Promise.allSettled(queries.map((query) => chrome.cookies.getAll({ ...query, storeId })));
  if (results.every((result) => result.status === "rejected")) {
    throw new Error(`Cookie API 查询失败，请在扩展管理中允许访问${config.name}网站，重新加载插件后重试`);
  }
  const groups = results.filter((result) => result.status === "fulfilled").map((result) => result.value);
  const values = new Map();
  const detectedNames = new Set();
  for (const cookie of groups.flat()) {
    const domain = String(cookie.domain || "").replace(/^\./, "");
    if (cookie.storeId !== storeId || cookie.partitionKey
      || !(domain === config.domain || domain.endsWith(`.${config.domain}`))) continue;
    if (!/^[!#$%&'*+.^_`|~0-9a-zA-Z-]+$/.test(cookie.name)
      || /[;\x00-\x1f\x7f]/.test(cookie.value)) throw new Error(`Cookie 格式无效，请重新登录${config.name}`);
    detectedNames.add(cookie.name);
    // 登录检测兼容大小写；空条目不能挡住同名的有效 Cookie。
    const name = [config.fingerprint, ...config.sessions].includes(cookie.name.toLowerCase()) ? cookie.name.toLowerCase() : cookie.name;
    if (cookie.value && !values.has(name)) values.set(name, cookie.value);
  }
  if (!values.get(config.fingerprint)) {
    // 只从当前页面补充非 HttpOnly 的指纹，不拼接其他账号的会话。
    const pageCookies = await readPlatformPageCookies(tabId, platform);
    const pageFingerprint = pageCookies.find((cookie) => cookie.name?.toLowerCase() === config.fingerprint && cookie.value
      && !/[;\x00-\x1f\x7f]/.test(cookie.value)
      && (cookie.domain === url.hostname));
    if (pageFingerprint) { values.set(config.fingerprint, pageFingerprint.value); detectedNames.add(config.fingerprint); }
  }
  if (platform === "douyin" && !values.get(config.fingerprint)) {
    // 抖音可能不再设置指纹 Cookie，沿用采集客户端的备用指纹机制。
    values.set(config.fingerprint, /^[A-Za-z0-9_-]{1,256}$/.test(fingerprint) ? fingerprint : generateVerifyFingerprint());
  }
  const missing = values.get(config.fingerprint) ? [] : [config.fingerprint];
  if (!config.sessions.some((name) => values.get(name))) missing.push(config.sessions.join(" / "));
  if (missing.length) {
    // 诊断只展示名称，绝不回显 Cookie 值。
    const names = [...detectedNames].sort().slice(0, 16).join("、") || "无";
    throw new Error(`缺少 ${missing.join("、")}；已读取 Cookie 名称：${names}。请确认扩展已获${config.name}网站访问权限，并刷新当前已登录的${config.name}页面后重试`);
  }
  return [...values].map(([name, value]) => `${name}=${value}`).join("; ");
}

const syncErrors = {
  DOUYIN_COOKIE_IMPORT_DISABLED: "服务端未启用抖音 Cookie 导入，请配置 DOUYIN_COOKIE_IMPORT_TOKEN 并重启服务",
  DOUYIN_COOKIE_IMPORT_UNAUTHORIZED: "抖音同步密钥无效，请检查服务端配置",
  DOUYIN_AUTH_EXPIRED: "抖音登录已过期，请重新登录后同步",
  DOUYIN_VERIFICATION_REQUIRED: "抖音要求安全验证，请在平台完成验证后重试",
  DOUYIN_COOKIE_IMPORT_ACTIVE: "此抖音账号正在同步，请稍后再试",
  DOUYIN_CREDENTIAL_SAVE_FAILED: "服务端保存抖音凭证失败，请检查数据库",
  XHS_COOKIE_IMPORT_DISABLED: "服务端未启用 Cookie 导入，请配置 XHS_COOKIE_IMPORT_TOKEN 并重启服务",
  XHS_COOKIE_IMPORT_UNAUTHORIZED: "同步密钥无效，请检查服务端配置",
  XHS_AUTH_EXPIRED: "登录已过期，请重新登录小红书后同步",
  XHS_SMS_SESSION_ACTIVE: "此账号正在登录或同步，请稍后再试",
  XHS_CREDENTIAL_SAVE_FAILED: "服务端保存凭证失败，请检查数据库",
  INVALID_REQUEST: "上传参数校验失败，请检查账号别名和登录状态",
};

export async function syncBrowserCookie({ platform = "xhs", accountId, token, tabId, fingerprint = "" }) {
  const config = syncPlatform(platform);
  if (!/^[a-z0-9_-]{1,64}$/.test(accountId)) throw new Error("账号别名仅支持 1～64 位小写字母、数字、下划线或连字符");
  if (!token || /[^\x21-\x7e]/.test(token)) throw new Error("请输入有效同步密钥（非空可见 ASCII 字符）");
  // 在点击事件的首个异步操作中申请固定服务站点，保留 Chrome 用户手势。
  if (!await chrome.permissions.request({ origins: [`${COOKIE_SYNC_SERVER_URL}/*`] })) throw new Error("未获得服务地址的访问权限");
  const cookie = await readSyncCookies(tabId, platform, fingerprint);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 120_000);
  try {
    const response = await fetch(`${COOKIE_SYNC_SERVER_URL}/api/v1/${config.route}/auth/cookie/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json", [config.header]: token },
      body: JSON.stringify({ account_id: accountId, cookie }),
      credentials: "omit", redirect: "error", cache: "no-store", signal: controller.signal,
    });
    const body = await response.json().catch(() => null);
    if (!response.ok || body?.success !== true) {
      // 不展示服务端原始正文，避免代理错误页或异常响应泄漏凭证。
      throw new Error(syncErrors[body?.error?.code] || `同步失败（HTTP ${response.status}），请检查服务端状态；如触发安全验证，请在${config.name}完成验证`);
    }
    if (body.data?.status !== "succeeded" || body.data?.account_id !== accountId) throw new Error("服务端未确认凭证保存成功");
    return { accountId, baseUrl: COOKIE_SYNC_SERVER_URL };
  } catch (error) {
    if (error.name === "AbortError") throw new Error("同步超时，请先检查服务端账号池状态，再决定是否重试");
    if (error instanceof TypeError) throw new Error("无法连接服务端，请检查地址、访问权限和 HTTPS 证书");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

// 保留小红书调用入口，两个平台共享上传和错误处理。
export function syncXhsCookie(options) {
  return syncBrowserCookie({ ...options, platform: "xhs" });
}
