// 页面 Cookie 读取供登录检测和同步共用；HttpOnly 凭证仍由 cookies API 获取。
export async function readPlatformPageCookies(tabId, platform = "xhs") {
  const domain = platform === "douyin" ? "douyin.com" : "xiaohongshu.com";
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [domain],
      func: (domain = "xiaohongshu.com") => {
        if (location.protocol !== "https:"
          || !(location.hostname === domain || location.hostname.endsWith(`.${domain}`))) return [];
        const cookies = document.cookie.split(";").map((part) => {
          const separator = part.indexOf("=");
          if (separator < 1) return null;
          // 保留原始编码，避免改变上传的 Cookie 内容。
          return { name: part.slice(0, separator).trim(), value: part.slice(separator + 1).trim(), domain: location.hostname, path: "/" };
        }).filter(Boolean);
        // _dsl 仅用于浏览器采集签名，不作为同步 Cookie 上传。
        if (domain === "xiaohongshu.com" && window._dsl) cookies.push({ name: "_dsl", value: String(window._dsl), domain: location.hostname, path: "/" });
        return cookies;
      },
    });
    return Array.isArray(result) ? result : [];
  } catch {
    // 页面尚未就绪或缺少脚本权限时，保留 cookies API 的读取结果。
    return [];
  }
}

export function readXhsPageCookies(tabId) {
  return readPlatformPageCookies(tabId, "xhs");
}
