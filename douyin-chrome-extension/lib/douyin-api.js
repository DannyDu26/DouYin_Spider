import { ABogusSigner } from "./abogus.js";
import { signWebUrl } from "./web-sign.js";
import { buildQuery, randomMsToken, resolveUserRequest, sleep } from "./core.js";

const BASE_URL = "https://www.douyin.com";
const USER_WORK_MAX_ATTEMPTS = 5;
const USER_WORK_RETRY_DELAY = 500;
const USER_WORK_PAGE_SIZE = 18;
const PAGE_REQUEST_MAX_ATTEMPTS = 5;
const PAGE_REQUEST_RETRY_DELAY = 500;

function asBooleanFlag(value) {
  return value === 1 || value === "1" || value === true;
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function ensureWorkMetrics(item) {
  const work = item?.aweme_info || item;
  if (!work || typeof work !== "object") return item;
  // 保证搜索、用户作品的 JSON 统计字段结构一致。
  work.statistics = {
    ...(work.statistics || {}),
    share_count: work.statistics?.share_count ?? 0,
    collect_count: work.statistics?.collect_count ?? 0,
  };
  return item;
}

export class DouyinRequestError extends Error {
  constructor(message, code = "REQUEST_FAILED", details = null) {
    super(message);
    this.name = "DouyinRequestError";
    this.code = code;
    this.details = details;
  }
}

export class DouyinClient {
  constructor({
    profile,
    cookieMap,
    signBdms,
    getMsToken = async () => randomMsToken(),
    fetchPage,
    fetchUserPage = fetchPage,
    sleepFn = sleep,
    onProgress = () => {},
    shouldCancel = () => false,
  }) {
    this.profile = profile;
    this.cookieMap = cookieMap;
    this.signBdms = signBdms;
    this.getMsToken = getMsToken;
    this.fetchPage = fetchPage;
    this.fetchUserPage = fetchUserPage;
    this.sleep = sleepFn;
    this.onProgress = onProgress;
    this.shouldCancel = shouldCancel;
    this.msToken = cookieMap.msToken || randomMsToken();
    this.fingerprint = cookieMap.s_v_web_id || profile.fingerprint;
    this.userSigner = new ABogusSigner({
      mainSite: true, userAgent: profile.userAgent, geometry: profile.geometry,
    });
    this.pureSigner = new ABogusSigner({
      userAgent: profile.userAgent,
      geometry: profile.geometry,
    });
  }

  checkCancelled() {
    if (this.shouldCancel()) {
      throw new DouyinRequestError("任务已停止", "CANCELLED");
    }
  }

  async resolveUserMsToken(forceRefresh = false) {
    try {
      return await this.getMsToken(forceRefresh) || randomMsToken();
    } catch {
      // 与后端一致：mssdk 暂时不可用时回退随机 token。
      return randomMsToken();
    }
  }

  platformParams(overrides = {}) {
    return {
      device_platform: "webapp",
      aid: "6383",
      channel: "channel_pc_web",
      update_version_code: "170400",
      pc_client_type: "1",
      version_code: "170400",
      version_name: "17.4.0",
      cookie_enabled: "true",
      screen_width: this.profile.screenWidth,
      screen_height: this.profile.screenHeight,
      browser_language: this.profile.language,
      browser_platform: this.profile.platform,
      browser_name: "Chrome",
      browser_version: this.profile.browserVersion,
      browser_online: "true",
      engine_name: "Blink",
      engine_version: this.profile.browserVersion,
      os_name: this.profile.osName,
      os_version: this.profile.osVersion,
      cpu_core_num: this.profile.hardwareConcurrency,
      device_memory: this.profile.deviceMemory,
      platform: "PC",
      downlink: "10",
      effective_type: "4g",
      round_trip_time: "100",
      ...overrides,
    };
  }

  userWorkParams({ userId, maxCursor, count, msToken }) {
    // 参数插入顺序与后端 get_user_work_info 保持一致。
    return {
      device_platform: "webapp",
      aid: "6383",
      channel: "channel_pc_web",
      sec_user_id: userId,
      max_cursor: maxCursor,
      locate_query: "false",
      show_live_replay_strategy: "1",
      need_time_list: maxCursor === "0" ? "1" : "0",
      time_list_query: "0",
      whale_cut_token: "",
      cut_version: "1",
      count: String(count),
      publish_video_strategy_type: "2",
      from_user_page: this.profile.secUserId === userId ? "0" : "1",
      update_version_code: "170400",
      pc_client_type: "1",
      pc_libra_divert: this.profile.osName,
      support_h265: "1",
      support_dash: "1",
      cpu_core_num: this.profile.hardwareConcurrency,
      version_code: "290100",
      version_name: "29.1.0",
      cookie_enabled: "true",
      screen_width: this.profile.screenWidth,
      screen_height: this.profile.screenHeight,
      browser_language: this.profile.language,
      browser_platform: this.profile.platform,
      browser_name: "Chrome",
      browser_version: this.profile.browserVersion,
      browser_online: "true",
      engine_name: "Blink",
      engine_version: this.profile.browserVersion,
      os_name: this.profile.osName,
      os_version: this.profile.osVersion,
      device_memory: this.profile.deviceMemory,
      platform: "PC",
      downlink: "10",
      effective_type: "4g",
      round_trip_time: "0",
      webid: this.profile.webId,
      ...(this.cookieMap.UIFID ? { uifid: this.cookieMap.UIFID } : {}),
      msToken,
    };
  }

  async request(apiPath, params, referrer, signer = "pure", fetcher = null) {
    this.checkCancelled();
    const unsignedQuery = buildQuery(params);
    let signature;
    if (signer === "bdms-full") {
      signature = await this.signBdms(apiPath, unsignedQuery, null);
    } else if (signer === "bdms-tail") {
      signature = await this.signBdms(apiPath, unsignedQuery, 87);
    } else if (signer === "user-main") {
      signature = this.userSigner.signQuery(unsignedQuery);
    } else {
      signature = this.pureSigner.signQuery(unsignedQuery);
    }

    let url = `${BASE_URL}${apiPath}?${buildQuery({ ...params, a_bogus: signature })}`;
    if (signer === "user-main") {
      // 指纹放在 a_bogus 之后，再对最终查询串添加 webSign。
      if (this.fingerprint) url += `&${buildQuery({ verifyFp: this.fingerprint, fp: this.fingerprint })}`;
      url = signWebUrl(url, { uifid: this.cookieMap.UIFID || "" });
    }
    const response = await (fetcher || this.fetchPage)({ url, referrer });
    if (response.status === 401) {
      throw new DouyinRequestError("平台登录状态已失效，请重新登录", "AUTH_EXPIRED");
    }
    if (response.status === 403) {
      // 403 通常是签名或风控拒绝，不能直接判定为登录失效。
      throw new DouyinRequestError(
        "目标平台拒绝了当前请求，可能是接口签名或安全限制，请稍后重试",
        "REQUEST_REJECTED",
        {
          status: response.status,
          finalUrl: response.finalUrl || "",
          pageUrl: response.pageUrl || "",
          requestReferrer: response.requestReferrer || "",
        },
      );
    }
    if (response.status === 429) {
      throw new DouyinRequestError("请求过于频繁，请稍后再试", "RISK_CONTROL");
    }
    if (/\/(?:login|passport|verify|captcha|challenge)/i.test(response.finalUrl || "")) {
      throw new DouyinRequestError("目标平台要求重新登录或完成安全验证", "AUTH_REQUIRED");
    }

    let data;
    try {
      data = JSON.parse(response.text);
    } catch {
      throw new DouyinRequestError(
        response.text?.trim()
          ? "目标平台返回了非 JSON 数据，可能需要登录或安全验证"
          : "目标平台返回空响应",
        "INVALID_RESPONSE",
      );
    }

    const statusCode = data?.status_code;
    const message = String(data?.status_msg || data?.message || "");
    if (![undefined, null, 0, "0"].includes(statusCode)) {
      if (/未登录|请.*登录|登录.*(?:失效|过期)|login|required|session expired/i.test(message)) {
        throw new DouyinRequestError("平台登录状态已失效，请重新登录", "AUTH_EXPIRED");
      }
      if (
        statusCode === 2484
        || statusCode === "2484"
        || /频繁|验证|验证码|风控|captcha|rate limit|antispam/i.test(message)
      ) {
        throw new DouyinRequestError(
          message || "目标平台触发安全限制，请降低请求频率",
          "RISK_CONTROL",
        );
      }
      throw new DouyinRequestError(message || `目标平台返回业务错误 ${statusCode}`, "UPSTREAM_ERROR");
    }
    return { data, headers: response.headers || {} };
  }

  async requestWithRetry({
    apiPath,
    params,
    referrer,
    signer = "pure",
    label,
    retryDelay = 0,
  }) {
    for (let attempt = 0; attempt < PAGE_REQUEST_MAX_ATTEMPTS; attempt += 1) {
      try {
        // 每次重试都重新执行签名和请求。
        return await this.request(apiPath, params, referrer, signer);
      } catch (error) {
        const retryable = !(error instanceof DouyinRequestError)
          || ["REQUEST_REJECTED", "INVALID_RESPONSE"].includes(error.code);
        if (!retryable || attempt >= PAGE_REQUEST_MAX_ATTEMPTS - 1) {
          throw error;
        }
        const reason = error instanceof DouyinRequestError
          && error.code === "REQUEST_REJECTED"
          ? "被拒绝"
          : "失败";
        this.onProgress(
          `${label}请求${reason}，正在重试 ${attempt + 1}/${PAGE_REQUEST_MAX_ATTEMPTS - 1}`,
        );
        const delay = Math.max(
          Number(retryDelay) || 0,
          PAGE_REQUEST_RETRY_DELAY * (attempt + 1),
        );
        await this.sleep(delay);
      }
    }
    throw new DouyinRequestError("请求重试失败", "REQUEST_FAILED");
  }

  async searchWorks(options) {
    const {
      keyword,
      limit,
      sortType,
      publishTime,
      duration,
      searchRange,
      contentType,
      interval,
    } = options;
    let offset = 0;
    let searchId = "";
    let hasMore = true;
    const items = [];
    const rawPageCounts = [];
    let page = 0;

    while (items.length < limit && hasMore) {
      this.checkCancelled();
      page += 1;
      this.onProgress(`正在抓取搜索结果，第 ${page} 页`);
      const referrer = `${BASE_URL}/search/${encodeURIComponent(keyword)}?aid=${crypto.randomUUID()}&type=general`;
      const params = this.platformParams({
        search_channel: "aweme_general",
        enable_history: "1",
        filter_selected: JSON.stringify({
          sort_type: sortType,
          publish_time: publishTime,
          filter_duration: duration,
          search_range: searchRange,
          content_type: contentType,
        }),
        keyword,
        search_source: searchId || offset !== 0 ? "normal_search" : "tab_search",
        query_correct_type: "1",
        is_filter_search: "1",
        from_group_id: "",
        offset: String(offset),
        count: "25",
        need_filter_settings: offset === 0 ? "1" : "0",
        ...(searchId ? { search_id: searchId } : {}),
        list_type: "single",
        version_code: "190600",
        version_name: "19.6.0",
        round_trip_time: "50",
        webid: this.profile.webId,
        msToken: this.msToken,
      });
      const response = await this.requestWithRetry({
        apiPath: "/aweme/v1/web/general/search/single/",
        params,
        referrer,
        signer: "bdms-full",
        label: `搜索第 ${page} 页`,
        retryDelay: interval,
      });
      const pageData = safeArray(response.data?.data);
      rawPageCounts.push(pageData.length);
      searchId = response.headers.xTtLogId || searchId;
      for (const item of pageData) {
        if (item && typeof item === "object" && item.aweme_info) {
          items.push(item);
          if (items.length >= limit) break;
        }
      }
      hasMore = asBooleanFlag(response.data?.has_more) && pageData.length > 0;
      offset += pageData.length;
      if (items.length < limit && hasMore && interval > 0) {
        await this.sleep(interval);
      }
    }

    const resultItems = items.slice(0, limit).map(ensureWorkMetrics);
    return {
      kind: "search",
      items: resultItems,
      meta: { keyword, total: resultItems.length, hasMore, rawPageCounts },
    };
  }

  async getUserWorks(options) {
    const parsedLimit = Number.parseInt(options.limit, 10);
    const limit = Number.isFinite(parsedLimit)
      ? Math.min(Math.max(parsedLimit, 1), 10000)
      : USER_WORK_PAGE_SIZE;
    const {
      interval,
      referrer: requestedReferrer,
    } = options;
    const { userId, referrer: resolvedReferrer } = resolveUserRequest(options.userId);
    let maxCursor = "0";
    let hasMore = true;
    const items = [];
    let page = 0;
    let stoppedEarly = false;
    let requestMsToken = await this.resolveUserMsToken(false);
    const referrer = requestedReferrer || resolvedReferrer;

    while (items.length < limit && hasMore) {
      this.checkCancelled();
      page += 1;
      this.onProgress(`正在抓取用户作品，第 ${page} 页，已获取 ${items.length} 条`);
      let response;
      for (let attempt = 0; attempt < USER_WORK_MAX_ATTEMPTS; attempt += 1) {
        const params = this.userWorkParams({
          userId,
          maxCursor,
          count: USER_WORK_PAGE_SIZE,
          msToken: requestMsToken,
        });
        try {
          // 每次调用都会重新生成 a_bogus。
          response = await this.request(
            "/aweme/v1/web/aweme/post/",
            params,
            referrer,
            "user-main",
            this.fetchUserPage,
          );
          break;
        } catch (error) {
          const retryable = error instanceof DouyinRequestError
            && error.code === "REQUEST_REJECTED";
          if (!retryable || attempt >= USER_WORK_MAX_ATTEMPTS - 1) {
            throw error;
          }
          if (attempt === 1) {
            // 连续两次 403 后强制换取一次新 token。
            requestMsToken = await this.resolveUserMsToken(true);
          }
          this.onProgress(
            `用户作品第 ${page} 页请求被拒绝，正在重试 ${attempt + 1}/${USER_WORK_MAX_ATTEMPTS - 1}`,
          );
          await this.sleep(USER_WORK_RETRY_DELAY * (attempt + 1));
        }
      }
      if (!Array.isArray(response.data?.aweme_list)) {
        throw new DouyinRequestError("上游响应缺少有效作品列表", "INVALID_RESPONSE");
      }
      const pageItems = response.data.aweme_list;
      const remaining = limit - items.length;
      items.push(...pageItems.slice(0, remaining));
      const returnedHasMore = asBooleanFlag(response.data?.has_more) && pageItems.length > 0;
      stoppedEarly = stoppedEarly
        || pageItems.length > remaining
        || (items.length >= limit && returnedHasMore);
      hasMore = returnedHasMore;
      const nextCursor = String(response.data?.max_cursor ?? "");
      if (!nextCursor || nextCursor === maxCursor) {
        hasMore = false;
      } else {
        maxCursor = nextCursor;
      }
      if (items.length < limit && hasMore && interval > 0) {
        await this.sleep(interval);
      }
    }

    return {
      kind: "user",
      items: items.map(ensureWorkMetrics),
      meta: {
        userId,
        total: items.length,
        limit,
        pagesFetched: page,
        hasMore: stoppedEarly || hasMore,
        nextCursor: maxCursor,
      },
    };
  }

  async getComments(options) {
    const parsedLimit = Number.parseInt(options.limit, 10);
    const limit = Number.isFinite(parsedLimit)
      ? Math.min(Math.max(parsedLimit, 1), 10000)
      : 100;
    const includeReplies = Boolean(options.includeReplies);
    const interval = Math.max(Number(options.interval) || 0, 0);
    const videoId = options.videoId;
    const referrer = `${BASE_URL}/video/${videoId}`;
    const items = [];
    let firstLevelCount = 0;
    let replyCount = 0;
    let firstCursor = "0";
    let firstHasMore = true;
    let firstPage = 0;
    let requestCount = 0;
    let stoppedEarly = false;

    const waitBeforeRequest = async () => {
      if (requestCount > 0 && interval > 0) {
        await this.sleep(interval);
      }
      requestCount += 1;
      this.checkCancelled();
    };

    while (items.length < limit && firstHasMore) {
      firstPage += 1;
      this.onProgress(`正在抓取评论，第 ${firstPage} 页，已获取 ${items.length} 条`);
      await waitBeforeRequest();
      const remaining = limit - items.length;
      const params = this.platformParams({
        aweme_id: videoId,
        cursor: firstCursor,
        count: String(Math.min(20, remaining)),
        item_type: "0",
        whale_cut_token: "",
        cut_version: "1",
        rcFT: "",
        round_trip_time: "0",
        webid: this.profile.webId,
        verifyFp: this.fingerprint,
        fp: this.fingerprint,
        msToken: this.msToken,
      });
      const response = await this.requestWithRetry({
        apiPath: "/aweme/v1/web/comment/list/",
        params,
        referrer,
        label: `评论第 ${firstPage} 页`,
        retryDelay: interval,
      });
      const pageItems = safeArray(response.data?.comments);
      const returnedHasMore = asBooleanFlag(response.data?.has_more) && pageItems.length > 0;
      const nextFirstCursor = String(response.data?.cursor ?? "");

      for (let index = 0; index < pageItems.length; index += 1) {
        const comment = pageItems[index];
        if (items.length >= limit) {
          stoppedEarly = true;
          break;
        }
        items.push({
          ...comment,
          comment_level: 1,
          parent_comment_id: "",
        });
        firstLevelCount += 1;

        if (items.length >= limit) {
          stoppedEarly = returnedHasMore
            || index < pageItems.length - 1
            || (
              includeReplies
              && Number(comment?.reply_comment_total || 0) > 0
            );
          break;
        }

        if (
          !includeReplies
          || Number(comment?.reply_comment_total || 0) <= 0
          || !comment?.cid
        ) {
          continue;
        }

        let replyCursor = "0";
        let replyHasMore = true;
        let replyPage = 0;
        while (items.length < limit && replyHasMore) {
          replyPage += 1;
          this.onProgress(
            `正在抓取第 ${firstLevelCount} 条评论的回复，第 ${replyPage} 页，已获取 ${items.length} 条`,
          );
          await waitBeforeRequest();
          const replyParams = this.platformParams({
            item_id: videoId,
            comment_id: comment.cid,
            cut_version: "1",
            cursor: replyCursor,
            count: String(Math.min(50, limit - items.length)),
            item_type: "0",
            round_trip_time: "0",
            webid: this.profile.webId,
            verifyFp: this.fingerprint,
            fp: this.fingerprint,
            msToken: this.msToken,
          });
          const replyResponse = await this.requestWithRetry({
            apiPath: "/aweme/v1/web/comment/list/reply/",
            referrer,
            params: replyParams,
            signer: "bdms-tail",
            label: `第 ${firstLevelCount} 条评论的回复第 ${replyPage} 页`,
            retryDelay: interval,
          });
          const replies = safeArray(replyResponse.data?.comments);
          for (const reply of replies) {
            if (items.length >= limit) {
              stoppedEarly = true;
              break;
            }
            items.push({
              ...reply,
              comment_level: 2,
              parent_comment_id: comment.cid,
            });
            replyCount += 1;
          }

          const nextReplyCursor = String(replyResponse.data?.cursor ?? "");
          replyHasMore = asBooleanFlag(replyResponse.data?.has_more) && replies.length > 0;
          if (!nextReplyCursor || nextReplyCursor === replyCursor) {
            replyHasMore = false;
          } else {
            replyCursor = nextReplyCursor;
          }
          if (items.length >= limit && replyHasMore) {
            stoppedEarly = true;
          }
        }

        if (items.length >= limit) {
          stoppedEarly = stoppedEarly || returnedHasMore || index < pageItems.length - 1;
          break;
        }
      }

      firstHasMore = returnedHasMore;
      if (!nextFirstCursor || nextFirstCursor === firstCursor) {
        firstHasMore = false;
      } else {
        firstCursor = nextFirstCursor;
      }
      stoppedEarly = stoppedEarly || (items.length >= limit && firstHasMore);
    }

    return {
      kind: "comments",
      items,
      meta: {
        videoId,
        total: items.length,
        includeReplies,
        firstLevelCount,
        replyCount,
        limit,
        hasMore: stoppedEarly || firstHasMore,
        nextCursor: firstCursor,
      },
    };
  }
}
