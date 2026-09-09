import assert from "node:assert/strict";

import { ABogusSigner } from "../lib/abogus.js";
import {
  buildQuery,
  generateVerifyFingerprint,
  inspectLoginCookies,
  mergeCookies,
  normalizeUserId,
  normalizeVideoId,
  parseCompactCount,
  parseDouyinPage,
  resolveUserRequest,
} from "../lib/core.js";
import { DouyinClient } from "../lib/douyin-api.js";
import { buildMsTokenReportBody } from "../lib/mstoken.js";
import { sm3Hex } from "../lib/sm3.js";

function testCoreHelpers() {
  assert.equal(sm3Hex("abc"), "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0");
  assert.deepEqual(
    parseDouyinPage("https://www.douyin.com/jingxuan/search/%E5%89%91%E7%BD%913?type=general"),
    {
      kind: "search",
      keyword: "剑网3",
      url: "https://www.douyin.com/jingxuan/search/%E5%89%91%E7%BD%913?type=general",
    },
  );
  assert.equal(
    parseDouyinPage("https://www.douyin.com/user/MS4wLjABAAAA-test?from_tab_name=main").userId,
    "MS4wLjABAAAA-test",
  );
  assert.equal(
    parseDouyinPage("https://www.douyin.com/discover?modal_id=7517981045911538959").videoId,
    "7517981045911538959",
  );
  assert.equal(normalizeVideoId("https://www.douyin.com/video/7517981045911538959"), "7517981045911538959");
  assert.equal(normalizeUserId("https://www.douyin.com/user/MS4wLjABAAAA-test"), "MS4wLjABAAAA-test");
  assert.deepEqual(
    resolveUserRequest(
      "MS4wLjABAAAA-test",
      parseDouyinPage(
        "https://www.douyin.com/user/MS4wLjABAAAA-test?from_tab_name=main&vid=123",
      ),
    ),
    {
      userId: "MS4wLjABAAAA-test",
      referrer: "https://www.douyin.com/user/MS4wLjABAAAA-test?from_tab_name=main&vid=123",
    },
  );
  assert.equal(
    resolveUserRequest("MS4wLjABAAAA-test").referrer,
    "https://www.douyin.com/user/MS4wLjABAAAA-test?from_tab_name=main",
  );
  assert.equal(
    resolveUserRequest(
      "MS4wLjABAAAA-test",
      parseDouyinPage("https://www.douyin.com/search/test?type=general"),
    ).referrer,
    "https://www.douyin.com/user/MS4wLjABAAAA-test?from_tab_name=main",
  );
  assert.equal(buildQuery({ keyword: "剑网3", filter: '{"a":"1/2"}' }), "keyword=%E5%89%91%E7%BD%913&filter=%7B%22a%22%3A%221/2%22%7D");
  assert.equal(parseCompactCount("3346"), 3346);
  assert.equal(parseCompactCount("1.5万"), 15000);
  assert.equal(parseCompactCount("1.2亿"), 120000000);
  assert.equal(parseCompactCount("点赞"), 0);

  const login = inspectLoginCookies([
    { name: "sessionid", value: "session" },
    { name: "s_v_web_id", value: "verify_test" },
    { name: "msToken", value: "current-token", path: "/" },
    { name: "msToken", value: "stale-token", path: "/other" },
  ]);
  assert.equal(login.ready, true);
  assert.equal(login.cookieMap.msToken, "current-token");
  assert.equal(inspectLoginCookies([{ name: "sessionid", value: "session" }]).ready, true);
  assert.equal(inspectLoginCookies([], true).ready, true);
  assert.equal(inspectLoginCookies([], false).ready, false);

  const merged = mergeCookies([
    [{ name: "sessionid", value: "session", domain: ".douyin.com", path: "/" }],
    [
      { name: "sessionid", value: "session", domain: ".douyin.com", path: "/" },
      { name: "s_v_web_id", value: "verify_test", domain: ".douyin.com", path: "/" },
    ],
  ]);
  assert.equal(merged.length, 2);
  assert.match(generateVerifyFingerprint(1_700_000_000_000), /^verify_[a-z0-9]+_[A-Za-z0-9_]+$/);
}

function testPureSignatureVector() {
  const query = "device_platform=webapp&aid=6383&keyword=%E5%89%91%E7%BD%913";
  const expected = "df0bgq6idxW5cdMSuObNSHnlrHnMNkWyj0J/WmoP9xzUbwlTXbYeYYOWaxqO4MdkpWpwiFV71jUMYxncFhwTZAHkLmpDSmwWkUA5V66oZ1wXbMiQLNfBCwuLeJ7bWOvEmAojJ1UlWtmO2dC4LpaTUBlJt/PNsmipQHabdc4aE9ef6zT9Bqq2uxSdO7zqHD==";
  assert.equal(new ABogusSigner({ fixed: true }).signQuery(query), expected);
}

function testMsTokenReportBody() {
  const profile = {
    geometry: [1280, 720, 1280, 800, 1280, 760, 1280, 800],
    hardwareConcurrency: "8",
    deviceMemory: "16",
  };
  const options = { now: 1_700_000_000_000, nonce: 37 };
  const first = buildMsTokenReportBody(profile, options);
  const second = buildMsTokenReportBody(profile, options);
  const envelope = JSON.parse(first);

  assert.equal(first, second);
  assert.equal(envelope.magic, 538969122);
  assert.equal(envelope.version, 1);
  assert.equal(envelope.dataType, 8);
  assert.equal(envelope.tspFromClient, options.now);
  assert.ok(envelope.strData.length > 100);
}

function createClient(
  responseFactory,
  calls,
  cookieMap = null,
  getMsToken = async () => "dynamic-token",
) {
  return new DouyinClient({
    profile: {
      userAgent: "Mozilla/5.0 Chrome/150.0.0.0",
      geometry: [1280, 720, 1280, 800, 1280, 760, 1280, 800],
      webId: "1234567890123456789",
      fingerprint: "verify_fallback",
      screenWidth: "1280",
      screenHeight: "800",
      language: "zh-CN",
      platform: "Win32",
      browserVersion: "150.0.0.0",
      hardwareConcurrency: "8",
      deviceMemory: "8",
      osName: "Windows",
      osVersion: "10",
    },
    cookieMap: cookieMap || {
      sessionid: "session",
      s_v_web_id: "verify_test",
      msToken: "token",
    },
    signBdms: async (apiPath, query, tailLength) => {
      calls.signatures.push({ apiPath, query, tailLength });
      return tailLength ? "x".repeat(tailLength) : "x".repeat(192);
    },
    getMsToken,
    fetchPage: async (request) => {
      calls.requests.push(request);
      return responseFactory(request);
    },
    fetchUserPage: async (request) => {
      calls.userRequests ||= [];
      calls.userRequests.push(request);
      return responseFactory(request);
    },
    sleepFn: async (duration) => {
      calls.sleeps ||= [];
      calls.sleeps.push(duration);
    },
  });
}

async function testApiFlows() {
  const searchCalls = { signatures: [], requests: [] };
  const searchClient = createClient(() => ({
    status: 200,
    finalUrl: "https://www.douyin.com/aweme/v1/web/general/search/single/",
    headers: { xTtLogId: "search-log" },
    text: JSON.stringify({
      status_code: 0,
      data: [{ aweme_info: { aweme_id: "1" } }],
      has_more: 0,
    }),
  }), searchCalls);
  const searchResult = await searchClient.searchWorks({
    keyword: "剑网3",
    limit: 10,
    sortType: "0",
    publishTime: "0",
    duration: "",
    searchRange: "0",
    contentType: "0",
    interval: 0,
  });
  assert.equal(searchResult.items.length, 1);
  assert.equal(searchResult.items[0].aweme_info.statistics.share_count, 0);
  assert.equal(searchResult.items[0].aweme_info.statistics.collect_count, 0);
  assert.equal(searchCalls.signatures[0].tailLength, null);
  assert.match(searchCalls.requests[0].url, /a_bogus=/);

  const searchRetryCalls = { signatures: [], requests: [] };
  const searchRetryClient = createClient((request) => {
    if (searchRetryCalls.requests.length <= 2) {
      return {
        status: 403,
        finalUrl: request.url,
        headers: {},
        text: "",
      };
    }
    return {
      status: 200,
      finalUrl: request.url,
      headers: {},
      text: JSON.stringify({
        status_code: 0,
        data: [{ aweme_info: { aweme_id: "search-retry-success" } }],
        has_more: 0,
      }),
    };
  }, searchRetryCalls);
  const searchRetryResult = await searchRetryClient.searchWorks({
    keyword: "重试",
    limit: 1,
    sortType: "0",
    publishTime: "0",
    duration: "",
    searchRange: "0",
    contentType: "0",
    interval: 1000,
  });
  assert.equal(searchRetryResult.items[0].aweme_info.aweme_id, "search-retry-success");
  assert.equal(searchRetryCalls.requests.length, 3);
  assert.equal(searchRetryCalls.signatures.length, 3);
  assert.deepEqual(searchRetryCalls.sleeps, [1000, 1000]);

  const userCalls = { signatures: [], requests: [] };
  const userClient = createClient((request) => ({
    status: 200,
    finalUrl: request.url,
    headers: {},
    text: JSON.stringify({
      status_code: 0,
      aweme_list: [{ aweme_id: "2" }],
      max_cursor: 10,
      has_more: 0,
    }),
  }), userCalls);
  const userResult = await userClient.getUserWorks({
    userId: "MS4wLjABAAAA-test",
    referrer: "https://www.douyin.com/user/MS4wLjABAAAA-test?from_tab_name=main&vid=123",
    limit: 18,
    interval: 0,
  });
  assert.equal(userResult.items[0].aweme_id, "2");
  assert.equal(userResult.items[0].statistics.share_count, 0);
  assert.equal(userResult.items[0].statistics.collect_count, 0);
  assert.equal(userCalls.requests.length, 0);
  assert.equal(userCalls.userRequests.length, 1);
  assert.match(userCalls.userRequests[0].url, /sec_user_id=MS4wLjABAAAA-test/);
  assert.match(userCalls.userRequests[0].url, /verifyFp=verify_test/);
  const userRequestUrl = new URL(userCalls.userRequests[0].url);
  assert.equal(userRequestUrl.searchParams.get("msToken"), "dynamic-token");
  assert.equal(userRequestUrl.searchParams.get("count"), "18");
  assert.deepEqual([...userRequestUrl.searchParams.keys()].slice(0, 5), [
    "device_platform",
    "aid",
    "channel",
    "sec_user_id",
    "max_cursor",
  ]);
  assert.ok(
    [...userRequestUrl.searchParams.keys()].indexOf("publish_video_strategy_type")
      < [...userRequestUrl.searchParams.keys()].indexOf("update_version_code"),
  );
  assert.equal(userRequestUrl.searchParams.has("uifid"), false);
  assert.equal(userRequestUrl.searchParams.has("timestamp"), false);
  assert.equal(userRequestUrl.searchParams.has("x-secsdk-web-signature"), false);
  assert.equal(
    userCalls.userRequests[0].referrer,
    "https://www.douyin.com/user/MS4wLjABAAAA-test?from_tab_name=main&vid=123",
  );
  // 用户作品接口使用纯签名，不应调用 BDMS。
  assert.equal(userCalls.signatures.length, 0);

  const userPagingCalls = { signatures: [], requests: [] };
  const userPagingClient = createClient((request) => {
    const cursor = new URL(request.url).searchParams.get("max_cursor");
    return {
      status: 200,
      finalUrl: request.url,
      headers: {},
      text: JSON.stringify(cursor === "0" ? {
        status_code: 0,
        aweme_list: [{ aweme_id: "page-1-a" }, { aweme_id: "page-1-b" }],
        max_cursor: 10,
        has_more: 1,
      } : {
        status_code: 0,
        aweme_list: [{ aweme_id: "page-2-a" }, { aweme_id: "page-2-b" }],
        max_cursor: 20,
        has_more: 0,
      }),
    };
  }, userPagingCalls);
  const pagedUserResult = await userPagingClient.getUserWorks({
    userId: "MS4wLjABAAAA-test",
    limit: 3,
    interval: 1000,
  });
  assert.deepEqual(
    pagedUserResult.items.map((item) => item.aweme_id),
    ["page-1-a", "page-1-b", "page-2-a"],
  );
  assert.equal(pagedUserResult.meta.limit, 3);
  assert.equal(pagedUserResult.meta.hasMore, true);
  assert.equal(userPagingCalls.userRequests.length, 2);
  assert.deepEqual(userPagingCalls.sleeps, [1000]);

  const retryCalls = { signatures: [], requests: [] };
  const retryMsTokenCalls = [];
  const retryClient = createClient((_request) => {
    if ((retryCalls.userRequests?.length || 0) <= 2) {
      return {
        status: 403,
        finalUrl: "https://www.douyin.com/aweme/v1/web/aweme/post/",
        headers: {},
        text: "",
      };
    }
    return {
      status: 200,
      finalUrl: "https://www.douyin.com/aweme/v1/web/aweme/post/",
      headers: {},
      text: JSON.stringify({
        status_code: 0,
        aweme_list: [{ aweme_id: "retry-success" }],
        has_more: 0,
      }),
    };
  }, retryCalls, {
    sessionid: "session",
    s_v_web_id: "verify_test",
    msToken: "cookie-token-must-not-be-used",
  }, async (forceRefresh) => {
    retryMsTokenCalls.push(forceRefresh);
    return forceRefresh ? "fresh-dynamic-token" : "initial-dynamic-token";
  });
  const retryResult = await retryClient.getUserWorks({
    userId: "MS4wLjABAAAA-test",
    limit: 18,
    interval: 0,
  });
  assert.equal(retryResult.items[0].aweme_id, "retry-success");
  assert.equal(retryCalls.requests.length, 0);
  assert.equal(retryCalls.userRequests.length, 3);
  const retryTokens = retryCalls.userRequests.map(
    (request) => new URL(request.url).searchParams.get("msToken"),
  );
  assert.deepEqual(retryTokens, [
    "initial-dynamic-token",
    "initial-dynamic-token",
    "fresh-dynamic-token",
  ]);
  assert.deepEqual(retryMsTokenCalls, [false, true]);

  const commentCalls = { signatures: [], requests: [] };
  const commentClient = createClient((request) => {
    const url = new URL(request.url);
    const cursor = url.searchParams.get("cursor");
    return {
      status: 200,
      finalUrl: request.url,
      headers: {},
      text: JSON.stringify(cursor === "0" ? {
        status_code: 0,
        comments: [{ cid: "9", text: "first page" }],
        cursor: 10,
        has_more: 1,
      } : {
        status_code: 0,
        comments: [{ cid: "10", text: "second page" }],
        cursor: 10,
        has_more: 0,
      }),
    };
  }, commentCalls);
  const commentsOnly = await commentClient.getComments({
    videoId: "7517981045911538959",
    limit: 5,
    includeReplies: false,
    interval: 0,
  });
  assert.deepEqual(commentsOnly.items.map((item) => item.cid), ["9", "10"]);
  assert.ok(commentsOnly.items.every((item) => item.comment_level === 1));
  assert.ok(commentsOnly.items.every((item) => item.parent_comment_id === ""));
  assert.equal(commentsOnly.meta.firstLevelCount, 2);
  assert.equal(commentsOnly.meta.replyCount, 0);
  assert.equal(commentCalls.requests.length, 2);
  assert.ok(commentCalls.requests.every(
    (request) => new URL(request.url).pathname === "/aweme/v1/web/comment/list/",
  ));
  assert.equal(commentCalls.signatures.length, 0);

  const commentRetryCalls = { signatures: [], requests: [] };
  const commentRetryClient = createClient((request) => ({
    status: commentRetryCalls.requests.length === 1 ? 403 : 200,
    finalUrl: request.url,
    headers: {},
    text: commentRetryCalls.requests.length === 1
      ? ""
      : JSON.stringify({
        status_code: 0,
        comments: [{ cid: "comment-retry-success", text: "retry" }],
        cursor: 1,
        has_more: 0,
      }),
  }), commentRetryCalls);
  const commentRetryResult = await commentRetryClient.getComments({
    videoId: "7517981045911538959",
    limit: 1,
    includeReplies: false,
    interval: 1000,
  });
  assert.equal(commentRetryResult.items[0].cid, "comment-retry-success");
  assert.equal(commentRetryCalls.requests.length, 2);
  assert.deepEqual(commentRetryCalls.sleeps, [1000]);

  const replyCalls = { signatures: [], requests: [] };
  const replyClient = createClient((request) => {
    const url = new URL(request.url);
    const isReply = url.pathname === "/aweme/v1/web/comment/list/reply/";
    const cursor = url.searchParams.get("cursor");
    let body;
    if (!isReply) {
      body = {
        status_code: 0,
        comments: [
          { cid: "parent-1", text: "parent", reply_comment_total: 3 },
          { cid: "parent-2", text: "unread parent", reply_comment_total: 0 },
        ],
        cursor: 20,
        has_more: 1,
      };
    } else if (cursor === "0") {
      body = {
        status_code: 0,
        comments: [
          { cid: "reply-1", text: "reply 1" },
          { cid: "reply-2", text: "reply 2" },
        ],
        cursor: 2,
        has_more: 1,
      };
    } else {
      body = {
        status_code: 0,
        comments: [{ cid: "reply-3", text: "reply 3" }],
        cursor: 3,
        has_more: 1,
      };
    }
    return {
      status: 200,
      finalUrl: request.url,
      headers: {},
      text: JSON.stringify(body),
    };
  }, replyCalls);
  const commentsWithReplies = await replyClient.getComments({
    videoId: "7517981045911538959",
    limit: 4,
    includeReplies: true,
    interval: 1000,
  });
  assert.deepEqual(
    commentsWithReplies.items.map((item) => item.cid),
    ["parent-1", "reply-1", "reply-2", "reply-3"],
  );
  assert.equal(commentsWithReplies.items[0].comment_level, 1);
  assert.ok(commentsWithReplies.items.slice(1).every(
    (item) => item.comment_level === 2 && item.parent_comment_id === "parent-1",
  ));
  assert.equal(commentsWithReplies.meta.total, 4);
  assert.equal(commentsWithReplies.meta.firstLevelCount, 1);
  assert.equal(commentsWithReplies.meta.replyCount, 3);
  assert.equal(commentsWithReplies.meta.hasMore, true);
  assert.deepEqual(replyCalls.sleeps, [1000, 1000]);
  assert.equal(replyCalls.requests.length, 3);
  assert.equal(
    new URL(replyCalls.requests[1].url).searchParams.get("comment_id"),
    "parent-1",
  );
  assert.deepEqual(replyCalls.signatures.map((item) => item.tailLength), [87, 87]);

  const replyRetryCalls = { signatures: [], requests: [] };
  const replyRetryClient = createClient((request) => {
    const isReply = new URL(request.url).pathname === "/aweme/v1/web/comment/list/reply/";
    if (!isReply) {
      return {
        status: 200,
        finalUrl: request.url,
        headers: {},
        text: JSON.stringify({
          status_code: 0,
          comments: [{ cid: "retry-parent", reply_comment_total: 1 }],
          cursor: 1,
          has_more: 0,
        }),
      };
    }
    const replyAttempt = replyRetryCalls.requests.filter(
      (item) => new URL(item.url).pathname === "/aweme/v1/web/comment/list/reply/",
    ).length;
    return {
      status: replyAttempt === 1 ? 403 : 200,
      finalUrl: request.url,
      headers: {},
      text: replyAttempt === 1
        ? ""
        : JSON.stringify({
          status_code: 0,
          comments: [{ cid: "reply-retry-success" }],
          cursor: 1,
          has_more: 0,
        }),
    };
  }, replyRetryCalls);
  const replyRetryResult = await replyRetryClient.getComments({
    videoId: "7517981045911538959",
    limit: 2,
    includeReplies: true,
    interval: 1000,
  });
  assert.deepEqual(
    replyRetryResult.items.map((item) => item.cid),
    ["retry-parent", "reply-retry-success"],
  );
  assert.equal(replyRetryCalls.requests.length, 3);
  assert.deepEqual(replyRetryCalls.sleeps, [1000, 1000]);
  assert.deepEqual(replyRetryCalls.signatures.map((item) => item.tailLength), [87, 87]);

  const rejectedCalls = { signatures: [], requests: [] };
  const rejectedClient = createClient(() => ({
    status: 403,
    finalUrl: "https://www.douyin.com/aweme/v1/web/aweme/post/",
    headers: {},
    text: "",
  }), rejectedCalls);
  await assert.rejects(
    rejectedClient.getUserWorks({
      userId: "MS4wLjABAAAA-test",
      limit: 18,
      interval: 0,
    }),
    (error) => error instanceof Error
      && error.code === "REQUEST_REJECTED"
      && !error.message.includes("登录状态已失效"),
  );
  assert.equal(rejectedCalls.requests.length, 0);
  assert.equal(rejectedCalls.userRequests.length, 5);
}

testCoreHelpers();
testPureSignatureVector();
testMsTokenReportBody();
await testApiFlows();
console.log("chrome-extension tests: PASS");
