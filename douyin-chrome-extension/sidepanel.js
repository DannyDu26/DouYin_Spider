import {
  generateVerifyFingerprint,
  inspectLoginCookies,
  mergeCookies,
  normalizeVideoId,
  parseCompactCount,
  parseDouyinPage,
  randomNumericId,
  resolveUserRequest,
} from "./lib/core.js";
import { DouyinClient, DouyinRequestError } from "./lib/douyin-api.js";
import { buildMsTokenReportBody } from "./lib/mstoken.js";

const elements = {
  pageSummary: document.querySelector("#page-summary"),
  refreshContext: document.querySelector("#refresh-context"),
  sessionBanner: document.querySelector("#session-banner"),
  sessionTitle: document.querySelector("#session-title"),
  sessionDetail: document.querySelector("#session-detail"),
  openDouyin: document.querySelector("#open-douyin"),
  modeTabs: [...document.querySelectorAll(".mode-tab")],
  forms: [...document.querySelectorAll(".crawler-form")],
  runButton: document.querySelector("#run-button"),
  stopButton: document.querySelector("#stop-button"),
  taskStatus: document.querySelector("#task-status"),
  resultSection: document.querySelector("#result-section"),
  resultSummary: document.querySelector("#result-summary"),
  resultList: document.querySelector("#result-list"),
  rawJson: document.querySelector("#raw-json"),
  copyJson: document.querySelector("#copy-json"),
  downloadJson: document.querySelector("#download-json"),
  downloadCsv: document.querySelector("#download-csv"),
  searchKeyword: document.querySelector("#search-keyword"),
  userId: document.querySelector("#user-id"),
  commentsVideoId: document.querySelector("#comments-video-id"),
};

const state = {
  mode: "search",
  tab: null,
  pageContext: null,
  login: null,
  profile: null,
  result: null,
  running: false,
  cancelRequested: false,
  refreshTimer: null,
};

function clearResult() {
  state.result = null;
  elements.resultSection.hidden = true;
  elements.resultSummary.textContent = "";
  elements.resultList.replaceChildren();
  elements.rawJson.textContent = "";
}

function setMode(mode) {
  const nextMode = mode === "replies" ? "comments" : mode;
  if (nextMode !== state.mode) {
    // 不在不同功能之间复用上一份抓取结果。
    clearResult();
    setTaskStatus("");
  }
  state.mode = nextMode;
  elements.modeTabs.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.mode === nextMode);
  });
  elements.forms.forEach((form) => {
    form.hidden = form.dataset.form !== nextMode;
  });
}

function setTaskStatus(message, isError = false) {
  elements.taskStatus.textContent = message;
  elements.taskStatus.classList.toggle("is-error", isError);
}

function setRunning(running) {
  state.running = running;
  elements.runButton.disabled = running || !state.login?.ready || !state.tab;
  elements.runButton.textContent = running ? "抓取中" : "开始抓取";
  elements.stopButton.hidden = !running;
  elements.modeTabs.forEach((button) => {
    button.disabled = running;
  });
}

function describePage(context) {
  if (context.kind === "search") return `搜索页：${context.keyword}`;
  if (context.kind === "user") return `用户主页：${context.userId}`;
  if (context.kind === "video") return `作品页：${context.videoId}`;
  if (context.kind === "douyin") return "平台页面";
  return "当前标签页不是目标平台";
}

function applyPageContext(context) {
  elements.pageSummary.textContent = describePage(context);
  if (context.kind === "search") {
    elements.searchKeyword.value = context.keyword;
    setMode("search");
  } else if (context.kind === "user") {
    elements.userId.value = context.userId;
    setMode("user");
  } else if (context.kind === "video") {
    elements.commentsVideoId.value = context.videoId;
    setMode("comments");
  }
}

async function findCookieStoreId(tabId) {
  const stores = await chrome.cookies.getAllCookieStores();
  return stores.find((store) => store.tabIds.includes(tabId))?.id;
}

async function readDouyinCookies(tabId) {
  const storeId = await findCookieStoreId(tabId);
  const tab = await chrome.tabs.get(tabId);
  const queries = [
    { url: "https://www.douyin.com/" },
    { url: "https://douyin.com/" },
    { domain: "douyin.com" },
    { domain: ".douyin.com" },
  ];
  if (tab.url?.startsWith("https://")) {
    queries.unshift({ url: tab.url });
  }

  // 同时覆盖当前标签页、主域名、子域名和当前 Cookie Store。
  const results = await Promise.allSettled(queries.map((query) => (
    chrome.cookies.getAll(storeId ? { ...query, storeId } : query)
  )));
  return mergeCookies(results
    .filter((result) => result.status === "fulfilled")
    .map((result) => result.value));
}

function renderLoginStatus(login, isDouyinTab) {
  elements.sessionBanner.classList.remove("is-loading", "is-warning");
  if (!isDouyinTab) {
    elements.sessionBanner.classList.add("is-warning");
    elements.sessionTitle.textContent = "请打开目标平台标签页";
    elements.sessionDetail.textContent = "采集请求需要在已登录的平台页面中执行";
    elements.openDouyin.hidden = false;
  } else if (!login.ready) {
    elements.sessionBanner.classList.add("is-warning");
    elements.sessionTitle.textContent = "未检测到平台登录状态";
    elements.sessionDetail.textContent = login.detectedNames.length
      ? `已读取 ${login.detectedNames.length} 个 Cookie，但未发现登录标识`
      : "Cookie API 未返回平台 Cookie，请登录后重新检测";
    elements.openDouyin.hidden = false;
  } else {
    elements.sessionTitle.textContent = "已检测到平台登录状态";
    const source = login.hasLoginCookie
      ? `登录 Cookie：${login.loginCookieNames.join("、")}`
      : "已通过当前页面账号状态确认";
    const fingerprint = login.hasFingerprint ? "指纹 Cookie：已检测" : "指纹：使用本地回退";
    elements.sessionDetail.textContent = `${source}；${fingerprint}`;
    elements.openDouyin.hidden = true;
  }
}

async function inspectPageEnvironment(tabId) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: async () => {
      // 优先从页面初始数据和本地存储提取稳定 webid。
      const candidates = [document.documentElement?.innerHTML || ""];
      const cookieText = document.cookie || "";
      for (let index = 0; index < localStorage.length && index < 80; index += 1) {
        const key = localStorage.key(index);
        const value = key ? localStorage.getItem(key) : "";
        if (value && /user[_-]?unique[_-]?id|webid/i.test(value)) {
          candidates.push(value);
        }
      }
      let webId = "";
      for (const source of candidates) {
        const match = source.match(/(?:user_unique_id|userUniqueId|webid)\\?["']?\s*[:=]\s*\\?["'](\d{8,24})/i);
        if (match) {
          webId = match[1];
          break;
        }
      }
      const chromeVersion = navigator.userAgent.match(/(?:Chrome|Chromium)\/([\d.]+)/)?.[1] || "120.0.0.0";
      const fingerprint = cookieText.match(/(?:^|;\s*)s_v_web_id=([^;]+)/)?.[1] || "";
      const selfLinks = [...document.querySelectorAll(
        'a[href^="/user/self"], a[href*="douyin.com/user/self"]',
      )];
      const hasAccountAvatar = selfLinks.some((link) => (
        !(link.textContent || "").trim() && link.childElementCount > 0
      ));
      const hasLoginButton = [...document.querySelectorAll("button")].some(
        (button) => button.textContent?.trim() === "登录",
      );
      const userAgentData = navigator.userAgentData;
      const secChUa = (userAgentData?.brands || [])
        .map(({ brand, version }) => `"${brand}";v="${version}"`)
        .join(", ");
      return {
        webId,
        fingerprint,
        // 账号头像存在且登录按钮消失时，作为 Cookie API 之外的登录兜底信号。
        pageAuthenticated: hasAccountAvatar && !hasLoginButton,
        userAgent: navigator.userAgent,
        language: navigator.language || "zh-CN",
        platform: navigator.platform || "Win32",
        secChUa,
        secChUaPlatform: userAgentData?.platform || "Windows",
        browserVersion: chromeVersion,
        hardwareConcurrency: String(navigator.hardwareConcurrency || 8),
        deviceMemory: String(navigator.deviceMemory || 8),
        screenWidth: String(screen.width || 1920),
        screenHeight: String(screen.height || 1080),
        osName: /Windows/i.test(navigator.userAgent) ? "Windows" : /Mac OS/i.test(navigator.userAgent) ? "Mac OS" : "Linux",
        osVersion: /Windows/i.test(navigator.userAgent) ? "10" : "",
        geometry: [
          window.innerWidth,
          window.innerHeight,
          window.outerWidth,
          window.outerHeight,
          screen.availWidth,
          screen.availHeight,
          screen.width,
          screen.height,
        ],
      };
    },
  });
  if (!result) {
    throw new Error("无法读取当前平台页面环境");
  }
  result.webId ||= randomNumericId();
  return result;
}

async function getFallbackFingerprint() {
  const key = "fallbackVerifyFingerprint";
  const saved = await chrome.storage.local.get(key);
  if (saved[key]) return saved[key];
  const fingerprint = generateVerifyFingerprint();
  await chrome.storage.local.set({ [key]: fingerprint });
  return fingerprint;
}

async function refreshContext() {
  if (state.running) return;
  elements.sessionBanner.classList.add("is-loading");
  elements.sessionTitle.textContent = "正在检测登录状态";
  elements.sessionDetail.textContent = "读取当前 Chrome 的平台 Cookie";
  setTaskStatus("");
  try {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    state.tab = tab || null;
    state.pageContext = parseDouyinPage(tab?.url || "");
    applyPageContext(state.pageContext);
    const isDouyinTab = state.pageContext.kind !== "external";
    const cookies = tab?.id ? await readDouyinCookies(tab.id) : [];
    state.profile = isDouyinTab && tab?.id ? await inspectPageEnvironment(tab.id) : null;
    state.login = inspectLoginCookies(cookies, state.profile?.pageAuthenticated);
    if (state.profile) {
      state.profile.fingerprint = state.login.cookieMap.s_v_web_id
        || state.profile.fingerprint
        || await getFallbackFingerprint();
    }
    renderLoginStatus(state.login, isDouyinTab);
  } catch (error) {
    state.login = {
      ready: false,
      cookieMap: {},
      detectedNames: [],
      loginCookieNames: [],
    };
    state.profile = null;
    elements.sessionBanner.classList.remove("is-loading");
    elements.sessionBanner.classList.add("is-warning");
    elements.sessionTitle.textContent = "检测失败";
    elements.sessionDetail.textContent = error instanceof Error ? error.message : String(error);
    elements.openDouyin.hidden = false;
  }
  setRunning(false);
}

async function signBdms(apiPath, query, tailLength) {
  const response = await chrome.runtime.sendMessage({
    type: "SIGN_BDMS",
    apiPath,
    query,
    tailLength,
  });
  if (!response?.ok) {
    throw new Error(response?.error || "BDMS 签名失败");
  }
  return response.signature;
}

async function getDynamicMsToken(forceRefresh = false) {
  if (!state.profile) {
    throw new Error("无法读取当前浏览器环境");
  }
  const response = await chrome.runtime.sendMessage({
    type: "GET_DYNAMIC_MSTOKEN",
    reportBody: buildMsTokenReportBody(state.profile),
    ttwid: state.login?.cookieMap?.ttwid || "",
    userAgent: state.profile.userAgent,
    forceRefresh,
  });
  if (!response?.ok || !response.token) {
    throw new Error(response?.error || "动态 msToken 获取失败");
  }
  return response.token;
}

function buildApiCookieHeader(cookieMap) {
  return Object.entries(cookieMap || {})
    .filter(([name, value]) => (
      value != null
      && value !== ""
      && name.toLowerCase() !== "mstoken"
    ))
    .map(([name, value]) => `${name}=${value}`)
    .join("; ");
}

async function fetchUserWorksDirect(request) {
  const response = await chrome.runtime.sendMessage({
    type: "FETCH_USER_WORKS",
    ...request,
    cookieHeader: buildApiCookieHeader(state.login?.cookieMap),
    userAgent: state.profile?.userAgent || "",
    secChUa: state.profile?.secChUa || "",
    secChUaPlatform: state.profile?.secChUaPlatform || "Windows",
    acceptLanguage: state.profile?.language || "zh-CN",
  });
  if (!response?.ok || !response.response) {
    throw new Error(response?.error || "用户作品请求失败");
  }
  return response.response;
}

async function fetchInPage(request) {
  if (!state.tab?.id) {
    throw new Error("未找到目标平台标签页");
  }
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: state.tab.id },
    world: "MAIN",
    args: [request],
    func: async ({ url, referrer }) => {
      try {
        // 同源请求显式保留完整主页地址，包括 from_tab_name 等查询参数。
        const fetchRequest = new Request(url, {
          method: "GET",
          credentials: "include",
          cache: "no-store",
          referrer,
          referrerPolicy: "unsafe-url",
          headers: {
            accept: "application/json, text/plain, */*",
          },
        });
        const response = await fetch(fetchRequest);
        return {
          ok: true,
          status: response.status,
          finalUrl: response.url,
          pageUrl: location.href,
          requestReferrer: fetchRequest.referrer,
          headers: {
            xTtLogId: response.headers.get("x-tt-logid") || "",
          },
          text: await response.text(),
        };
      } catch (error) {
        return {
          ok: false,
          error: error instanceof Error ? error.message : String(error),
        };
      }
    },
  });
  if (!result?.ok) {
    throw new Error(result?.error || "平台页面请求失败");
  }
  return result;
}

async function collectUserWorksFromPage(userId) {
  if (!state.tab?.id) {
    throw new Error("未找到目标平台标签页");
  }
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: state.tab.id },
    world: "MAIN",
    args: [userId],
    func: (expectedUserId) => {
      const currentMatch = location.pathname.match(/^\/user\/([^/]+)\/?$/);
      if (!currentMatch || decodeURIComponent(currentMatch[1]) !== expectedUserId) {
        return { ok: false, error: "当前页面不是目标用户主页" };
      }

      const list = document.querySelector('[data-e2e="user-post-list"]');
      if (!list) {
        return { ok: false, error: "主页作品列表尚未加载，请稍后重试" };
      }

      const nickname = document.querySelector("h1")?.textContent?.trim() || "未知作者";
      const items = [];
      const seen = new Set();
      for (const link of list.querySelectorAll('a[href*="/video/"]')) {
        const url = new URL(link.href, location.href);
        const match = url.pathname.match(/^\/video\/(\d+)\/?$/);
        if (!match || seen.has(match[1])) continue;
        seen.add(match[1]);

        const lines = (link.innerText || link.getAttribute("aria-label") || "")
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean);
        const popularityText = lines.find((line) => /^\d+(?:\.\d+)?(?:万|亿)?$/.test(line)) || "";
        const description = lines
          .filter((line) => line !== popularityText && line !== "置顶")
          .join(" ");
        items.push({
          awemeId: match[1],
          description,
          nickname,
          popularityText,
          shareUrl: url.href,
        });
      }

      // 逐级查找包含作品列表的实际滚动容器。
      let scrollContainer = list;
      while (
        scrollContainer.parentElement
        && !(
          scrollContainer.scrollHeight > scrollContainer.clientHeight + 100
          && ["auto", "scroll"].includes(getComputedStyle(scrollContainer).overflowY)
        )
      ) {
        scrollContainer = scrollContainer.parentElement;
      }
      const canScroll = scrollContainer.scrollHeight > scrollContainer.clientHeight + 100;
      return {
        ok: true,
        items,
        scrollTop: scrollContainer.scrollTop,
        scrollHeight: scrollContainer.scrollHeight,
        clientHeight: scrollContainer.clientHeight,
        canScroll,
        atEnd: !canScroll
          || scrollContainer.scrollTop + scrollContainer.clientHeight >= scrollContainer.scrollHeight - 20,
      };
    },
  });
  if (!result?.ok) {
    throw new Error(result?.error || "无法读取主页作品");
  }
  return result;
}

async function scrollUserWorksPage() {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: state.tab.id },
    world: "MAIN",
    func: () => {
      const list = document.querySelector('[data-e2e="user-post-list"]');
      if (!list) return false;
      let scrollContainer = list;
      while (
        scrollContainer.parentElement
        && !(
          scrollContainer.scrollHeight > scrollContainer.clientHeight + 100
          && ["auto", "scroll"].includes(getComputedStyle(scrollContainer).overflowY)
        )
      ) {
        scrollContainer = scrollContainer.parentElement;
      }
      const previous = scrollContainer.scrollTop;
      scrollContainer.scrollTo({
        top: Math.min(
          scrollContainer.scrollHeight,
          previous + Math.max(scrollContainer.clientHeight * 0.85, 600),
        ),
        behavior: "auto",
      });
      return scrollContainer.scrollTop > previous;
    },
  });
  return Boolean(result);
}

async function scrapeUserWorks(options) {
  const targetCount = options.limit;
  const itemsById = new Map();
  let unchangedRounds = 0;
  let rounds = 0;
  const maxRounds = Math.max(Math.ceil(targetCount / 18) * 6, 8);

  while (itemsById.size < targetCount && rounds < maxRounds) {
    if (state.cancelRequested) {
      throw new DouyinRequestError("任务已停止", "CANCELLED");
    }
    rounds += 1;
    const snapshot = await collectUserWorksFromPage(options.userId);
    const previousSize = itemsById.size;
    snapshot.items.forEach((item) => {
      itemsById.set(item.awemeId, {
        aweme_id: item.awemeId,
        desc: item.description,
        share_url: item.shareUrl,
        author: {
          nickname: item.nickname,
          sec_uid: options.userId,
        },
        statistics: {
          digg_count: parseCompactCount(item.popularityText),
          digg_count_text: item.popularityText,
          // 主页列表无法稳定读取这些统计值，保持字段结构一致。
          comment_count: 0,
          share_count: 0,
          collect_count: 0,
        },
      });
    });

    unchangedRounds = itemsById.size === previousSize ? unchangedRounds + 1 : 0;
    if (itemsById.size >= targetCount || snapshot.atEnd || unchangedRounds >= 3) {
      break;
    }
    setTaskStatus(`正在读取主页作品，已获取 ${itemsById.size} 条`);
    const scrolled = await scrollUserWorksPage();
    if (!scrolled) break;
    await new Promise((resolve) => setTimeout(resolve, options.interval));
  }

  const items = [...itemsById.values()].slice(0, targetCount);
  if (!items.length) {
    throw new Error("主页没有可读取的公开作品");
  }
  return {
    kind: "user",
    items,
    meta: {
      userId: options.userId,
      total: items.length,
      limit: targetCount,
      pagesFetched: Math.ceil(items.length / 18),
      hasMore: itemsById.size >= targetCount,
      source: "user-page",
    },
  };
}

function getFormValues() {
  const form = elements.forms.find((item) => item.dataset.form === state.mode);
  if (!form?.reportValidity()) {
    throw new Error("请检查输入参数");
  }
  const values = Object.fromEntries(new FormData(form));
  if (state.mode === "search") {
    return {
      keyword: values.keyword.trim(),
      limit: Number(values.limit),
      sortType: values.sortType,
      publishTime: values.publishTime,
      duration: values.duration,
      searchRange: values.searchRange,
      contentType: values.contentType,
      interval: Number(values.interval),
    };
  }
  if (state.mode === "user") {
    const userRequest = resolveUserRequest(values.userId, state.pageContext);
    return {
      userId: userRequest.userId,
      referrer: userRequest.referrer,
      limit: Number(values.limit),
      interval: Number(values.interval),
    };
  }
  return {
    videoId: normalizeVideoId(values.videoId),
    limit: Number(values.limit),
    includeReplies: values.includeReplies === "on",
    interval: Number(values.interval),
  };
}

async function runCrawler() {
  if (state.running) return;
  if (!state.login?.ready) {
    setTaskStatus("请先在目标平台标签页完成登录", true);
    return;
  }
  if (!state.profile || state.pageContext?.kind === "external") {
    setTaskStatus("请保持一个目标平台标签页处于当前活动状态", true);
    return;
  }

  try {
    const options = getFormValues();
    state.cancelRequested = false;
    setRunning(true);
    setTaskStatus("正在准备请求");
    const client = new DouyinClient({
      profile: state.profile,
      cookieMap: state.login.cookieMap,
      signBdms,
      getMsToken: getDynamicMsToken,
      fetchPage: fetchInPage,
      fetchUserPage: fetchUserWorksDirect,
      onProgress: (message) => setTaskStatus(message),
      shouldCancel: () => state.cancelRequested,
    });
    if (state.mode === "search") {
      state.result = await client.searchWorks(options);
    } else if (state.mode === "user") {
      try {
        // 优先使用作品接口，返回字段比页面列表完整。
        state.result = await client.getUserWorks(options);
      } catch (error) {
        if (!(error instanceof DouyinRequestError) || error.code !== "REQUEST_REJECTED") {
          throw error;
        }
        const canUsePageFallback = state.pageContext?.kind === "user"
          && state.pageContext.userId === options.userId;
        if (!canUsePageFallback) {
          // 非目标主页仍可调用接口，只是无法读取目标主页 DOM 作为兜底。
          throw new DouyinRequestError(
            `${error.message}；当前不是目标用户主页，无法启用主页读取回退`,
            error.code,
            error.details,
          );
        }
        // 连续拒绝时回退到当前主页已加载作品。
        setTaskStatus("作品接口持续被拒绝，正在改用主页读取");
        state.result = await scrapeUserWorks(options);
      }
    } else if (state.mode === "comments") {
      state.result = await client.getComments(options);
    } else {
      throw new Error("不支持的抓取模式");
    }
    renderResult(state.result);
    setTaskStatus(`抓取完成，共 ${state.result.items.length} 条`);
    await saveFormState();
  } catch (error) {
    if (error instanceof DouyinRequestError && error.code === "CANCELLED") {
      setTaskStatus("任务已停止");
    } else {
      setTaskStatus(error instanceof Error ? error.message : String(error), true);
    }
  } finally {
    setRunning(false);
  }
}

function getWorkInfo(item) {
  const work = item?.aweme_info || item;
  return {
    id: work?.aweme_id || work?.group_id || "",
    title: work?.author?.nickname || "未知作者",
    text: work?.desc || work?.item_title || "无作品描述",
    meta: [
      `点赞 ${work?.statistics?.digg_count ?? 0}`,
      `评论 ${work?.statistics?.comment_count ?? 0}`,
      `转发 ${work?.statistics?.share_count ?? 0}`,
      `收藏 ${work?.statistics?.collect_count ?? 0}`,
    ],
  };
}

function getCommentInfo(item) {
  return {
    id: item?.cid || "",
    title: item?.user?.nickname || "未知用户",
    text: item?.text || "无评论文本",
    meta: [
      item?.comment_level === 2 ? "二级回复" : "一级评论",
      `点赞 ${item?.digg_count ?? 0}`,
      `回复 ${item?.reply_comment_total ?? 0}`,
    ],
  };
}

function renderResult(result) {
  elements.resultSection.hidden = false;
  elements.resultSummary.textContent = `${result.items.length} 条数据`;
  elements.rawJson.textContent = JSON.stringify(result, null, 2);
  elements.resultList.replaceChildren();
  const visibleItems = result.items.slice(0, 30);
  visibleItems.forEach((item) => {
    const info = result.kind === "comments"
      ? getCommentInfo(item)
      : getWorkInfo(item);
    const row = document.createElement("article");
    row.className = "result-item";
    const title = document.createElement("strong");
    title.textContent = info.title;
    const text = document.createElement("p");
    text.textContent = info.text;
    const meta = document.createElement("div");
    meta.className = "result-meta";
    info.meta.forEach((value) => {
      const span = document.createElement("span");
      span.textContent = value;
      meta.append(span);
    });
    row.append(title, text, meta);
    elements.resultList.append(row);
  });
  if (result.items.length > visibleItems.length) {
    const row = document.createElement("article");
    row.className = "result-item";
    const text = document.createElement("p");
    text.textContent = `其余 ${result.items.length - visibleItems.length} 条请通过 JSON 或 CSV 查看`;
    row.append(text);
    elements.resultList.append(row);
  }
}

function csvEscape(value) {
  const text = value == null ? "" : String(value);
  return `"${text.replaceAll('"', '""')}"`;
}

function resultToCsv(result) {
  const isComment = result.kind === "comments";
  const headers = isComment
    ? [
      "comment_id",
      "comment_level",
      "parent_comment_id",
      "text",
      "nickname",
      "user_id",
      "create_time",
      "digg_count",
      "reply_count",
    ]
    : [
      "aweme_id",
      "description",
      "nickname",
      "sec_uid",
      "create_time",
      "digg_count",
      "comment_count",
      "share_count",
      "collect_count",
    ];
  const rows = result.items.map((item) => {
    if (isComment) {
      return [
        item?.cid,
        item?.comment_level || 1,
        item?.parent_comment_id || "",
        item?.text,
        item?.user?.nickname,
        item?.user?.uid,
        item?.create_time,
        item?.digg_count,
        item?.reply_comment_total,
      ];
    }
    const work = item?.aweme_info || item;
    return [
      work?.aweme_id,
      work?.desc,
      work?.author?.nickname,
      work?.author?.sec_uid,
      work?.create_time,
      work?.statistics?.digg_count,
      work?.statistics?.comment_count,
      work?.statistics?.share_count ?? 0,
      work?.statistics?.collect_count ?? 0,
    ];
  });
  return [headers, ...rows].map((row) => row.map(csvEscape).join(",")).join("\r\n");
}

function downloadFile(content, type, extension) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `short-video-${state.result?.kind || "data"}-${Date.now()}.${extension}`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function saveFormState() {
  const forms = Object.fromEntries(elements.forms.map((form) => [
    form.dataset.form,
    Object.fromEntries(new FormData(form)),
  ]));
  await chrome.storage.local.set({ forms, mode: state.mode });
}

async function restoreFormState() {
  const saved = await chrome.storage.local.get(["forms", "mode"]);
  Object.entries(saved.forms || {}).forEach(([mode, values]) => {
    const form = elements.forms.find((item) => item.dataset.form === mode);
    if (!form) return;
    Object.entries(values).forEach(([name, value]) => {
      const control = form.elements.namedItem(name);
      if (!control) return;
      if (control.type === "checkbox") {
        // 开关需要恢复 checked，而不是修改固定的 value。
        control.checked = value === "on" || value === true;
      } else {
        control.value = value;
      }
    });
  });
  if (saved.mode) setMode(saved.mode);
}

function scheduleContextRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(refreshContext, 250);
}

elements.modeTabs.forEach((button) => {
  button.addEventListener("click", () => setMode(button.dataset.mode));
});
elements.refreshContext.addEventListener("click", refreshContext);
elements.openDouyin.addEventListener("click", async () => {
  if (state.pageContext?.kind === "douyin") {
    await chrome.tabs.reload(state.tab.id);
  } else {
    await chrome.tabs.create({ url: "https://www.douyin.com/" });
  }
});
elements.runButton.addEventListener("click", runCrawler);
elements.stopButton.addEventListener("click", () => {
  state.cancelRequested = true;
  setTaskStatus("正在停止，当前请求完成后结束");
});
elements.copyJson.addEventListener("click", async () => {
  if (!state.result) return;
  await navigator.clipboard.writeText(JSON.stringify(state.result, null, 2));
  setTaskStatus("JSON 已复制");
});
elements.downloadJson.addEventListener("click", () => {
  if (state.result) {
    downloadFile(JSON.stringify(state.result, null, 2), "application/json;charset=utf-8", "json");
  }
});
elements.downloadCsv.addEventListener("click", () => {
  if (state.result) {
    downloadFile(`\ufeff${resultToCsv(state.result)}`, "text/csv;charset=utf-8", "csv");
  }
});

chrome.tabs.onActivated.addListener(scheduleContextRefresh);
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (tabId === state.tab?.id && changeInfo.url) scheduleContextRefresh();
});
chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  if (details.frameId === 0 && details.tabId === state.tab?.id) {
    scheduleContextRefresh();
  }
});

await restoreFormState();
await refreshContext();
