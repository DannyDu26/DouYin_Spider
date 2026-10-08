const LOGIN_COOKIE_NAMES = new Set([
  "sessionid",
  "sessionid_ss",
  "sid_guard",
  "sid_tt",
  "uid_tt",
  "uid_tt_ss",
  "passport_auth_status",
  "passport_auth_status_ss",
]);

export function isDouyinHost(hostname) {
  const host = String(hostname || "").toLowerCase();
  return host === "douyin.com" || host.endsWith(".douyin.com");
}

export function parseDouyinPage(rawUrl) {
  try {
    const url = new URL(rawUrl);
    if (url.protocol !== "https:" || !isDouyinHost(url.hostname)) {
      return { kind: "external", url: rawUrl || "" };
    }

    const searchMatch = url.pathname.match(/^\/(?:jingxuan\/)?search\/([^/]+)\/?$/);
    if (searchMatch) {
      return {
        kind: "search",
        keyword: decodeURIComponent(searchMatch[1]),
        url: url.href,
      };
    }

    const userMatch = url.pathname.match(/^\/user\/([^/]+)\/?$/);
    if (userMatch) {
      return {
        kind: "user",
        userId: decodeURIComponent(userMatch[1]),
        url: url.href,
      };
    }

    const videoMatch = url.pathname.match(/^\/video\/(\d+)\/?$/);
    const modalId = url.searchParams.get("modal_id");
    if (videoMatch || /^\d+$/.test(modalId || "")) {
      return {
        kind: "video",
        videoId: videoMatch?.[1] || modalId,
        url: url.href,
      };
    }
    return { kind: "douyin", url: url.href };
  } catch {
    return { kind: "external", url: rawUrl || "" };
  }
}

export function cookiesToMap(cookies) {
  const cookieMap = {};
  for (const cookie of cookies || []) {
    if (!cookie?.name || Object.hasOwn(cookieMap, cookie.name)) continue;
    // 当前页面精确查询结果排在前面，避免被其他路径旧 Cookie 覆盖。
    cookieMap[cookie.name] = cookie.value;
  }
  return cookieMap;
}

export function mergeCookies(cookieGroups) {
  const merged = new Map();
  for (const cookie of (cookieGroups || []).flat()) {
    if (!cookie?.name) continue;
    // 同名 Cookie 可能属于不同路径或分区，保留每个唯一条目。
    const partitionKey = JSON.stringify(cookie.partitionKey || {});
    const key = [
      cookie.name,
      cookie.domain || "",
      cookie.path || "",
      cookie.storeId || "",
      partitionKey,
    ].join("\u0000");
    merged.set(key, cookie);
  }
  return [...merged.values()];
}

export function inspectLoginCookies(cookies, pageAuthenticated = false) {
  const cookieMap = cookiesToMap(cookies);
  const loginCookieNames = [...LOGIN_COOKIE_NAMES].filter((name) => Boolean(cookieMap[name]));
  const detectedNames = [...new Set((cookies || []).map((cookie) => cookie.name).filter(Boolean))].sort();
  const hasLoginCookie = loginCookieNames.length > 0;
  return {
    cookieMap,
    detectedNames,
    loginCookieNames,
    hasLoginCookie,
    hasFingerprint: Boolean(cookieMap.s_v_web_id),
    pageAuthenticated: Boolean(pageAuthenticated),
    ready: hasLoginCookie || Boolean(pageAuthenticated),
  };
}

export function generateVerifyFingerprint(now = Date.now()) {
  const alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
  const randomPart = (length) => {
    const values = new Uint32Array(length);
    crypto.getRandomValues(values);
    return [...values].map((value) => alphabet[value % alphabet.length]).join("");
  };
  // 保持与网页常见 verifyFp 格式一致，作为缺失 s_v_web_id 时的本地回退。
  return `verify_${now.toString(36)}_${[
    randomPart(8),
    randomPart(4),
    randomPart(4),
    randomPart(4),
    randomPart(12),
  ].join("_")}`;
}

export function normalizeVideoId(value) {
  const input = String(value || "").trim();
  if (/^\d+$/.test(input)) {
    return input;
  }
  const context = parseDouyinPage(input);
  if (context.kind === "video") {
    return context.videoId;
  }
  throw new Error("请输入数字作品 ID 或有效的作品链接");
}

export function normalizeUserId(value) {
  const input = String(value || "").trim();
  if (/^[A-Za-z0-9._~-]{1,128}$/.test(input)) {
    return input;
  }
  if (input.length > 2048 || /[\u0000-\u001f\u007f]/.test(input)) {
    throw new Error("用户主页链接格式不合法");
  }
  const context = parseDouyinPage(input);
  if (context.kind === "user" && !new URL(input).username && !new URL(input).password) {
    return context.userId;
  }
  throw new Error("请输入主页 sec_user_id 或有效的用户主页链接");
}

export function resolveUserRequest(value, currentContext = null) {
  const input = String(value || "").trim();
  const userId = normalizeUserId(input);
  const inputContext = parseDouyinPage(input);
  // 优先保留真实主页查询参数，避免上游拒绝简化 Referer。
  const matchedContext = [inputContext, currentContext].find(
    (context) => context?.kind === "user"
      && context.userId === userId
      && typeof context.url === "string",
  );
  return {
    userId,
    referrer: matchedContext?.url
      || `https://www.douyin.com/user/${encodeURIComponent(userId)}?from_tab_name=main`,
  };
}

export function randomNumericId(length = 19) {
  let output = "";
  crypto.getRandomValues(new Uint32Array(length)).forEach((value) => {
    output += String(value % 10);
  });
  return output;
}

export function randomMsToken(length = 107) {
  const alphabet = "ABCDEFGHIGKLMNOPQRSTUVWXYZabcdefghigklmnopqrstuvwxyz0123456789=";
  const values = new Uint32Array(length);
  crypto.getRandomValues(values);
  return [...values].map((value) => alphabet[value % alphabet.length]).join("");
}

export function parseCompactCount(value) {
  const text = String(value || "").trim();
  const match = text.match(/^(\d+(?:\.\d+)?)(万|亿)?$/);
  if (!match) return 0;
  const multiplier = match[2] === "亿" ? 100_000_000 : match[2] === "万" ? 10_000 : 1;
  return Math.round(Number(match[1]) * multiplier);
}

export function encodeQueryValue(value) {
  return encodeURIComponent(value == null ? "" : String(value))
    .replace(/[!'()*]/g, (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`)
    .replace(/%2F/gi, "/");
}

export function buildQuery(params) {
  return Object.entries(params)
    .map(([key, value]) => `${encodeQueryValue(key)}=${encodeQueryValue(value)}`)
    .join("&");
}

export function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
