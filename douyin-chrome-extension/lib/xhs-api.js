// 小红书浏览器直连客户端：请求在当前登录页面上下文中发起，Cookie 不离开浏览器。

export class XhsRequestError extends Error {
  constructor(message, code = "XHS_REQUEST_FAILED", details = null, status = 0) {
    super(message); this.name = "XhsRequestError"; this.code = code; this.details = details; this.status = status;
  }
}

export function parseXhsPage(url = "") {
  try {
    const parsed = new URL(url);
    if (!/^https?:$/.test(parsed.protocol) || !/(^|\.)xiaohongshu\.com$/i.test(parsed.hostname)) return { kind: "external", url };
    const note = parsed.pathname.match(/^\/(?:explore|discovery\/item)\/([0-9a-f]{24})\/?$/i);
    if (note) return { kind: "note", noteId: note[1], url };
    const user = parsed.pathname.match(/^\/user\/profile\/([0-9a-f]{24})\/?$/i);
    if (user) return { kind: "user", userId: user[1], url };
    if (/^\/search_result\/?$/i.test(parsed.pathname)) return { kind: "search", keyword: parsed.searchParams.get("keyword") || "", url };
    return { kind: "xhs", url };
  } catch { return { kind: "external", url }; }
}

export function normalizeXhsId(value, kind = "ID") {
  const match = String(value || "").trim().match(/([0-9a-f]{24})/i);
  if (!match) throw new XhsRequestError(`请输入有效的小红书${kind}（24 位十六进制）`, "INVALID_XHS_ID");
  return match[1];
}

export function normalizeXhsUrl(value, kind = "笔记") {
  let parsed;
  try { parsed = new URL(String(value || "").trim()); } catch { throw new XhsRequestError(`请输入完整的小红书${kind}链接`, "INVALID_XHS_URL"); }
  if (parsed.protocol !== "https:" || !/(^|\.)xiaohongshu\.com$/i.test(parsed.hostname)) throw new XhsRequestError(`请输入完整的 HTTPS 小红书${kind}链接`, "INVALID_XHS_URL");
  return parsed.href;
}

// 兼容大小写和历史别名，避免 Cookie API 返回值差异导致误判未登录。
export function inspectXhsLoginCookies(cookies, pageAuthenticated = false) {
  const cookieMap = {};
  const detectedNames = [];
  for (const cookie of cookies || []) {
    if (!cookie?.name) continue;
    detectedNames.push(cookie.name);
    const normalizedName = cookie.name.trim().toLowerCase();
    const value = String(cookie.value ?? "");
    if (value && !cookieMap[normalizedName]) cookieMap[normalizedName] = value;
  }
  cookieMap.a1 ||= cookieMap.a_1 || "";
  cookieMap.web_session ||= cookieMap.websession || cookieMap.web_session_v2 || "";
  cookieMap.webBuild ||= cookieMap.webbuild || "";
  const loginCookieNames = ["a1", "web_session", "webid", "gid", "secure_session"].filter((name) => cookieMap[name]);
  const hasSession = Boolean(cookieMap.web_session || cookieMap.secure_session || cookieMap.id_token || cookieMap.login_token);
  // 小红书签名必须使用真实 a1，只有页面登录状态不能替代该 Cookie。
  const ready = Boolean(cookieMap.a1 && (hasSession || pageAuthenticated));
  return { ready, pageAuthenticated, cookieMap, detectedNames: [...new Set(detectedNames)], loginCookieNames, hasLoginCookie: hasSession, hasFingerprint: Boolean(cookieMap.a1) };
}

// 在 MAIN world 中调用小红书页面已经加载的 seccore/mnsv2 签名器。
export async function executeXhsRequest(tabId, request) {
  // 页面脚本或网络请求异常时不能让侧边栏永久等待。
  const execution = chrome.scripting.executeScript({
    target: { tabId }, world: "MAIN", args: [request], func: async (input) => {
      const apiUrl = new URL(input.api, location.origin);
      const body = input.method === "GET" ? undefined : JSON.stringify(input.data ?? {});
      const candidates = [window.mnsv2, window.seccore_signv2, window.seccoreSignV2, window.__mnsv2__, window.__xhs_sign__, window.xhsSign].filter((item) => typeof item === "function");
      const localSigned = input.signedHeaders || {};
      let signed = { ...localSigned };
      let lastError = null;
      const calls = [
        (fn) => fn({ url: input.signApi || input.api, api: input.signApi || input.api, data: input.data ?? "", method: input.method || "GET" }),
        (fn) => fn(input.signApi || input.api, input.data ?? ""),
        (fn) => fn({ url: input.signApi || input.api, data: body || "", method: input.method || "GET" }),
      ];
      const timedCall = (work, timeoutMs) => new Promise((resolve, reject) => {
        let settled = false;
        const timer = setTimeout(() => {
          if (!settled) { settled = true; reject(new Error("页面签名器响应超时")); }
        }, timeoutMs);
        Promise.resolve().then(work).then((value) => {
          if (settled) return;
          settled = true; clearTimeout(timer); resolve(value);
        }, (error) => {
          if (settled) return;
          settled = true; clearTimeout(timer); reject(error);
        });
      });
      for (const fn of candidates) {
        for (const call of calls) {
          try {
            const value = await timedCall(() => call(fn), 1500);
            if (value && typeof value === "object") {
              // 页面签名器包含当前版本的 RAP/MNS 参数，优先使用页面结果，本地结果只做回退。
              signed = { ...signed, ...(value.headers || value) };
              if (signed["x-rap-param"] || signed["X-RAP-Param"]) break;
            }
          } catch (error) { lastError = error; }
        }
        if (signed["x-rap-param"] || signed["X-RAP-Param"]) break;
      }
      const headers = {
        accept: "application/json, text/plain, */*",
        origin: "https://www.xiaohongshu.com",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "x-b3-traceid": `${Date.now().toString(16)}${Math.random().toString(16).slice(2, 10)}`.slice(-16),
        "x-xray-traceid": `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}${Math.random().toString(16).slice(2)}`.slice(-32),
        ...(body ? { "content-type": "application/json;charset=UTF-8" } : {}),
        ...(input.referer ? { referer: input.referer } : { referer: location.href }),
      };
      const aliases = {
        "x-s": ["x-s", "X-S", "xs", "x_s"], "x-t": ["x-t", "X-T", "xt", "x_t"],
        "x-s-common": ["x-s-common", "X-S-Common", "xs_common", "x_s_common"],
        "x-rap-param": ["x-rap-param", "X-RAP-Param", "x_rap_param", "xRapParam", "rapParam"],
      };
      for (const [header, keys] of Object.entries(aliases)) {
        const key = keys.find((name) => signed[name] != null);
        if (key) headers[header] = String(signed[key]);
      }
      if (!headers["x-s"]) {
        const message = candidates.length ? "未能调用小红书页面签名器，请刷新小红书页面后重试" : "当前小红书页面未加载签名器，请先打开并刷新小红书页面";
        return { ok: false, error: message, detail: lastError?.message || "" };
      }
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 20000);
        let response;
        try {
          response = await fetch(apiUrl.href, { method: input.method || "GET", credentials: "include", cache: "no-store", headers, body, referrer: input.referer || location.href, referrerPolicy: "unsafe-url", signal: controller.signal });
        } finally {
          clearTimeout(timer);
        }
        return { ok: true, status: response.status, text: await response.text(), finalUrl: response.url };
      } catch (error) { return { ok: false, error: error instanceof Error ? error.message : String(error) }; }
    },
  });
  const timeout = new Promise((_, reject) => setTimeout(() => reject(new XhsRequestError("小红书页面签名或请求超时，请刷新页面后重试", "XHS_PAGE_TIMEOUT")), 25000));
  const [{ result }] = await Promise.race([execution, timeout]);
  if (!result?.ok) throw new XhsRequestError(result?.error || "小红书页面请求失败", "XHS_PAGE_REQUEST_FAILED");
  let envelope;
  try { envelope = JSON.parse(result.text); } catch { throw new XhsRequestError(`小红书返回无效 JSON（HTTP ${result.status}）`, "INVALID_RESPONSE", null, result.status); }
  if (result.status < 200 || result.status >= 300 || envelope?.success === false || envelope?.code < 0) {
    const message = envelope?.msg || envelope?.message || `小红书请求失败（HTTP ${result.status}）`;
    const detail = envelope?.code != null ? `；上游错误码：${envelope.code}` : "";
    throw new XhsRequestError(`${message}${detail}`, `HTTP_${result.status}`, envelope, result.status);
  }
  return envelope?.data ?? envelope;
}

function serializeXhsQuery(data = {}) {
  const query = new URLSearchParams();
  Object.entries(data || {}).forEach(([key, value]) => {
    if (value == null) return;
    // 小红书 GET 接口将图片格式等数组参数编码为逗号分隔字符串。
    query.set(key, Array.isArray(value) ? value.join(",") : String(value));
  });
  return query.toString();
}

function noteRequest(url, noteId, xsecToken, xsecSource) {
  if (url) return { url: normalizeXhsUrl(url), fromUrl: true };
  return { noteId: normalizeXhsId(noteId, "笔记 ID"), xsecToken: String(xsecToken || ""), xsecSource: xsecSource || "pc_search" };
}

export class XhsBrowserClient {
  constructor({ tabId, cookieMap = {}, onProgress = () => {}, shouldCancel = () => false, sleepFn = (ms) => new Promise((resolve) => setTimeout(resolve, ms)) } = {}) { this.tabId = tabId; this.cookieMap = cookieMap; this.sequence = 0; this.onProgress = onProgress; this.shouldCancel = shouldCancel; this.sleepFn = sleepFn; }
  async reportProgress(message) {
    this.onProgress(message);
    // 让侧边栏先绘制状态，再进入签名和网络等待。
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  async request(api, data, method = "POST", origin = "https://edith.xiaohongshu.com") {
    if (this.shouldCancel()) throw new XhsRequestError("任务已停止", "CANCELLED");
    const normalizedMethod = String(method || "POST").toUpperCase();
    const query = normalizedMethod === "GET" ? serializeXhsQuery(data) : "";
    const requestApi = query ? `${api}?${query}` : api;
    const requestData = normalizedMethod === "GET" ? "" : data;
    const fullApi = `${origin}${requestApi}`;
    const { signXhs } = await import("./xhs-sign.js");
    const signedHeaders = signXhs({ api: requestApi, data: requestData, cookieMap: this.cookieMap, seq: ++this.sequence });
    // 搜索、详情和用户笔记接口还校验 RAP 业务签名，浏览器本地生成后随请求发送。
    if (["/api/sns/web/v2/search/notes", "/api/sns/web/v1/feed", "/api/sns/web/v1/user_posted"].includes(api)) {
      const { buildXhsRapParam } = await import("./xhs-rap.js");
      await this.reportProgress("正在生成小红书请求签名");
      const timeout = new Promise((_, reject) => setTimeout(() => reject(new XhsRequestError("小红书 RAP 签名超时，请刷新页面后重试", "XHS_RAP_TIMEOUT")), 8000));
      signedHeaders["x-rap-param"] = await Promise.race([buildXhsRapParam(requestApi, requestData), timeout]);
    }
    return executeXhsRequest(this.tabId, { api: fullApi, signApi: requestApi, data: requestData, method: normalizedMethod, signedHeaders });
  }
  async searchNotes({ keyword, limit = 20, sort = "general", noteType = 0, noteTime = 0, noteRange = 0, interval = 1000 }) {
    const items = []; let page = 1; let searchId = ""; let searchSessionId = "";
    while (items.length < limit) {
      await this.reportProgress(`正在搜索小红书笔记，第 ${page} 页，已获取 ${items.length} 条`);
      const data = await this.request("/api/sns/web/v2/search/notes", { keyword, page, page_size: 20, search_id: searchId, session_id: searchSessionId, sort, note_type: Number(noteType), ext_flags: [], geo: "", image_formats: ["jpg", "webp", "avif"] }, "POST", "https://so.xiaohongshu.com");
      searchId = data.search_id || searchId; searchSessionId = data.session_id || data.search_session_id || searchSessionId; items.push(...(data.items || []));
      if (!data.has_more || !data.next_page) break; page = data.next_page; if (interval) await this.sleepFn(interval);
    }
    return { kind: "xhs-search", items: items.slice(0, limit), meta: { keyword, total: Math.min(items.length, limit), limit, pagesFetched: page } };
  }
  async getNoteInfo(options) {
    const req = noteRequest(options.url, options.noteId, options.xsecToken, options.xsecSource);
    const parsed = req.fromUrl ? new URL(req.url) : null;
    const data = await this.request("/api/sns/web/v1/feed", { source_note_id: req.noteId || parsed.pathname.split("/").pop(), image_formats: ["jpg", "webp", "avif"], extra: { need_body_topic: "1" }, xsec_source: req.xsecSource || parsed.searchParams.get("xsec_source") || "pc_search", xsec_token: req.xsecToken || parsed.searchParams.get("xsec_token") || "" });
    return { kind: "xhs-note", items: data.items || [], meta: { total: (data.items || []).length } };
  }
  async getUserNotes({ userId, userUrl, limit = 30, interval = 1000 }) {
    const parsed = userUrl ? new URL(normalizeXhsUrl(userUrl, "用户主页")) : null; const id = userId || parsed?.pathname.split("/").pop(); const items = []; let cursor = "";
    while (items.length < limit) {
      await this.reportProgress(`正在读取小红书用户笔记，已获取 ${items.length} 条`);
      // 用户笔记接口使用 GET 查询参数，避免 POST 预检被小红书网关拦截。
      const data = await this.request("/api/sns/web/v1/user_posted", { num: 30, cursor, user_id: normalizeXhsId(id, "用户 ID"), image_formats: ["jpg", "webp", "avif"], xsec_token: parsed?.searchParams.get("xsec_token") || "", xsec_source: parsed?.searchParams.get("xsec_source") || "pc_search" }, "GET");
      items.push(...(data.notes || [])); if (!data.has_more || !data.cursor) break; cursor = data.cursor; if (interval) await this.sleepFn(interval);
    }
    return { kind: "xhs-user-notes", items: items.slice(0, limit), meta: { total: Math.min(items.length, limit), limit } };
  }
  async getNoteComments({ url, noteId, xsecToken, limit = 100, includeReplies = false, interval = 1000 }) {
    const req = noteRequest(url, noteId, xsecToken); const parsed = req.fromUrl ? new URL(req.url) : null; const id = req.noteId || parsed.pathname.split("/").pop(); const token = req.xsecToken || parsed.searchParams.get("xsec_token") || ""; const items = []; let cursor = "";
    while (items.length < limit) {
      await this.reportProgress(`正在读取小红书评论，已获取 ${items.length} 条`);
      // 评论接口使用 GET 查询参数，避免 POST 预检导致 Failed to fetch。
      const data = await this.request("/api/sns/web/v2/comment/page", { note_id: id, cursor, top_comment_id: "", image_formats: ["jpg", "webp", "avif"], xsec_token: token }, "GET"); const page = data.comments || []; items.push(...page);
      if (includeReplies) for (const comment of page) { const rootId = comment.id || comment.comment_id; if (rootId && comment.sub_comment_count) comment.replies = (await this.getNoteSubComments({ noteId: id, rootCommentId: rootId, xsecToken: token, limit: comment.sub_comment_count, interval })).items; }
      if (!data.has_more || !data.cursor) break; cursor = data.cursor; if (interval) await this.sleepFn(interval);
    }
    return { kind: "xhs-comments", items: items.slice(0, limit), meta: { total: Math.min(items.length, limit), limit } };
  }
  async getNoteSubComments({ noteId, rootCommentId, xsecToken, limit = 100, interval = 1000 }) {
    const items = []; let cursor = "";
    while (items.length < limit) { const data = await this.request("/api/sns/web/v2/comment/sub/page", { note_id: normalizeXhsId(noteId, "笔记 ID"), root_comment_id: normalizeXhsId(rootCommentId, "评论 ID"), num: 10, cursor, image_formats: ["jpg", "webp", "avif"], top_comment_id: "", xsec_token: xsecToken }, "GET"); items.push(...(data.comments || [])); if (!data.has_more || !data.cursor) break; cursor = data.cursor; if (interval) await this.sleepFn(interval); }
    return { kind: "xhs-sub-comments", items: items.slice(0, limit), meta: { total: Math.min(items.length, limit), limit } };
  }
}

