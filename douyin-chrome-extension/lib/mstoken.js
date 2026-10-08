const CUSTOM_BASE64 = "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe";

const FINGERPRINT_TEMPLATE = {
  tokenList: [],
  navigator: {
    appCodeName: "Mozilla",
    appMinorVersion: "undefined",
    appName: "Netscape",
    appVersion: "5.0 (Windows)",
    buildID: "undefined",
    doNotTrack: "null",
    msDoNotTrack: "undefined",
    oscpu: "undefined",
    platform: "Win32",
    product: "Gecko",
    productSub: "20030107",
    cpuClass: "undefined",
    vendor: "Google Inc.",
    vendorSub: "undefined",
    deviceMemory: "8",
    language: "zh-CN",
    systemLanguage: "undefined",
    userLanguage: "undefined",
    webdriver: "false",
    cookieEnabled: 1,
    vibrate: 4,
    credentials: 4,
    storage: 4,
    requestMediaKeySystemAccess: 4,
    bluetooth: 4,
    hardwareConcurrency: 12,
    maxTouchPoints: -1,
    languages: "zh-CN,zh",
    touchEvent: 1,
    touchstart: 2,
  },
  wID: {
    load: 0,
    nap: "6",
    nativeLength: 33,
    jsFontsList: "0",
    timestamp: "0",
    timezone: 8,
    magic: 3,
    canvas: "-1",
    wProps: 374262,
    dProps: 2,
    jsv: "",
    browserType: 0,
    iframe: 2,
    aid: 0,
    msgType: 1,
    privacyMode: 0,
    aidList: [],
    index: 1,
  },
  window: {
    Image: 3,
    isSecureContext: 4,
    ActiveXObject: 4,
    toolbar: 4,
    locationbar: 4,
    external: 4,
    mozRTCPeerConnection: 4,
    postMessage: 3,
    webkitRequestAnimationFrame: 4,
    BluetoothUUID: 4,
    netscape: 4,
    localStorage: 11,
    sessionStorage: 11,
    indexDB: 4,
    devicePixelRatio: 1,
    location: "https://www.douyin.com/",
  },
  webgl: {},
  document: {
    characterSet: "UTF-8",
    compatMode: "undefined",
    documentMode: "undefined",
    layers: 4,
    all: 4,
    images: 4,
  },
  screen: {
    innerWidth: 1707,
    innerHeight: 809,
    outerWidth: 1707,
    outerHeight: 912,
    screenX: 0,
    screenY: 0,
    pageXOffset: 0,
    pageYOffset: 0,
    availWidth: 1707,
    availHeight: 912,
    sizeWidth: 1707,
    sizeHeight: 960,
    clientWidth: 1697,
    clientHeight: 809,
    colorDepth: 24,
    pixelDepth: 24,
  },
  plugins: {
    plugin: [],
    pv: "0",
  },
  custom: {},
};

function rc4(key, data) {
  const state = Array.from({ length: 256 }, (_value, index) => index);
  let j = 0;
  for (let i = 0; i < 256; i += 1) {
    j = (j + state[i] + key[i % key.length]) & 255;
    [state[i], state[j]] = [state[j], state[i]];
  }

  const output = new Uint8Array(data.length);
  let i = 0;
  j = 0;
  for (let index = 0; index < data.length; index += 1) {
    i = (i + 1) & 255;
    j = (j + state[i]) & 255;
    [state[i], state[j]] = [state[j], state[i]];
    output[index] = data[index] ^ state[(state[i] + state[j]) & 255];
  }
  return output;
}

function customBase64Encode(data) {
  let output = "";
  for (let index = 0; index < data.length; index += 3) {
    const remaining = data.length - index;
    const b0 = data[index];
    const b1 = remaining > 1 ? data[index + 1] : 0;
    const b2 = remaining > 2 ? data[index + 2] : 0;
    const triplet = (b0 << 16) | (b1 << 8) | b2;
    output += CUSTOM_BASE64[(triplet >> 18) & 63];
    output += CUSTOM_BASE64[(triplet >> 12) & 63];
    output += remaining > 1 ? CUSTOM_BASE64[(triplet >> 6) & 63] : "=";
    output += remaining > 2 ? CUSTOM_BASE64[triplet & 63] : "=";
  }
  return output;
}

function encodeStrData(plaintext, nonce) {
  const cipher = rc4(Uint8Array.of(nonce), new TextEncoder().encode(plaintext));
  const raw = new Uint8Array(cipher.length + 2);
  raw[0] = 0x41;
  raw[1] = nonce;
  raw.set(cipher, 2);
  return customBase64Encode(raw);
}

function randomNonce() {
  const value = new Uint8Array(1);
  crypto.getRandomValues(value);
  return value[0];
}

export function buildMsTokenReportBody(profile, options = {}) {
  const now = options.now ?? Date.now();
  const nonce = options.nonce ?? randomNonce();
  if (!Number.isFinite(now) || !Number.isInteger(nonce) || nonce < 0 || nonce > 255) {
    throw new Error("无效的 msToken 上报参数");
  }

  // 与后端使用同一模板，仅覆盖当前浏览器会话的硬件和窗口数据。
  const fingerprint = JSON.parse(JSON.stringify(FINGERPRINT_TEMPLATE));
  const geometry = Array.isArray(profile?.geometry) ? profile.geometry.map(Number) : [];
  const [
    innerWidth = 1707,
    innerHeight = 809,
    outerWidth = 1707,
    outerHeight = 912,
    availWidth = 1707,
    availHeight = 912,
    screenWidth = 1707,
    screenHeight = 960,
  ] = geometry;

  fingerprint.navigator.hardwareConcurrency = Number(profile?.hardwareConcurrency || 12);
  fingerprint.navigator.deviceMemory = String(profile?.deviceMemory || 8);
  fingerprint.wID.timestamp = String(now);
  Object.assign(fingerprint.screen, {
    innerWidth,
    innerHeight,
    outerWidth,
    outerHeight,
    availWidth,
    availHeight,
    sizeWidth: screenWidth,
    sizeHeight: screenHeight,
    clientWidth: innerWidth - 10,
    clientHeight: innerHeight,
  });

  const strData = encodeStrData(JSON.stringify(fingerprint), nonce);
  return JSON.stringify({
    magic: 538969122,
    version: 1,
    dataType: 8,
    strData,
    tspFromClient: now,
    ulr: 0,
  });
}
