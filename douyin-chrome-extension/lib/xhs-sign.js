import { md5 } from "./web-sign.js";

// 签名阶段单独使用错误类型，避免和请求模块形成循环依赖。
export class XhsSignError extends Error {
  constructor(message, code = "XHS_SIGN_FAILED") {
    super(message);
    this.name = "XhsSignError";
    this.code = code;
  }
}

// 小红书 PC 端 MNS/X-S-Common 纯算法，浏览器插件直接生成请求头。
const MNS_ALPHABET = "MfgqrsbcyzPQRStuvC7mn501HIJBo2DEFTKdeNOwxWXYZap89+/A4UVLhijkl63G";
const XS_ALPHABET = "ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5";
const KEYSTREAM_B64 = "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";
const KEYSTREAM = Uint8Array.from(atob(KEYSTREAM_B64), (char) => char.charCodeAt(0));
const utf8 = (value) => [...new TextEncoder().encode(String(value))];
const hexBytes = (value) => Array.from({ length: value.length / 2 }, (_, index) => Number.parseInt(value.slice(index * 2, index * 2 + 2), 16));
const le32 = (value) => [value >>> 0 & 255, value >>> 8 & 255, value >>> 16 & 255, value >>> 24 & 255];
const le64 = (value) => { let number = BigInt(value); const output = []; for (let i = 0; i < 8; i += 1) { output.push(Number(number & 255n)); number >>= 8n; } return output; };
function customBase64(bytes, alphabet = MNS_ALPHABET) { let output = ""; for (let i = 0; i < bytes.length; i += 3) { const a = bytes[i], b = bytes[i + 1], c = bytes[i + 2]; output += alphabet[a >>> 2]; output += alphabet[((a & 3) << 4) | ((b || 0) >>> 4)]; if (b === undefined) { output += "=="; break; } output += alphabet[((b & 15) << 2) | ((c || 0) >>> 6)]; if (c === undefined) { output += "="; break; } output += alphabet[c & 63]; } return output; }
function dsf(bytes) { const read = (offset) => (bytes[offset] | (bytes[offset + 1] << 8) | (bytes[offset + 2] << 16) | (bytes[offset + 3] << 24)) | 0; const rotate = (value, bits) => (((value << bits) | 0) + (value >>> (32 - bits))) | 0; const length = bytes.length; let h1 = 0x6d2b79f5 ^ length, h2 = 0x1b873593 ^ (length << 8), h3 = 0x85ebca6b ^ (length << 16), h4 = 0xc2b2ae35 ^ (length << 24); for (let i = 0; i + 8 <= length; i += 8) { const w0 = read(i), w1 = read(i + 4); h1 = rotate((((h1 + w0) | 0) ^ h3) | 0, 7); h2 = rotate((((h2 ^ w0) | 0) + h4) | 0, 11); h3 = rotate((((h3 + w1) | 0) ^ h1) | 0, 13); h4 = rotate((((h4 ^ w1) | 0) + h2) | 0, 17); } h1 = (h1 ^ length) | 0; h2 = (h2 ^ h1) | 0; h3 = (h3 + h2) | 0; h4 = (h4 ^ h3) | 0; h1 = rotate(h1, 9); h2 = rotate(h2, 13); h3 = rotate(h3, 17); h4 = rotate(h4, 19); h1 = (h1 + h3) | 0; h2 = (h2 ^ h4) | 0; h3 = (h3 + h1) | 0; h4 = (h4 ^ h2) | 0; return [h1, h2, h3, h4].flatMap((value) => [value >>> 0 & 255, value >>> 8 & 255, value >>> 16 & 255, value >>> 24 & 255]); }
function pack({ api, data, a1, now, loadts, seq, version, envConst, envFpTail }) {
  const dataText = data == null ? "" : typeof data === "object" ? JSON.stringify(data) : String(data); const full = api + dataText; const md5Full = hexBytes(md5(full)); const md5Api = hexBytes(md5(api)); const low = version & 255; const dsSig = dsf([...le64(now), ...md5Api]).map((item) => item ^ low); return [121, 104, 96, 41, ...le32(version), ...le64(now), ...le64(loadts), ...le32(seq), ...le32(envConst), ...le32(new TextEncoder().encode(full).length), ...md5Full.slice(0, 8).map((item) => item ^ low), a1.length, ...utf8(a1), 10, ...utf8("xhs-pc-web"), 1, low ^ 115, ...envFpTail, 2, ...utf8("a3"), 16, ...dsSig];
}
const CRC32_TABLE = Array.from({ length: 256 }, (_, index) => { let value = index; for (let bit = 0; bit < 8; bit += 1) value = value & 1 ? (value >>> 1) ^ 0xedb88320 : value >>> 1; return value >>> 0; });
function crc32Variant(value) { let crc = -1; for (const char of String(value)) crc = CRC32_TABLE[(crc & 255) ^ char.charCodeAt(0)] ^ (crc >>> 8); return (-1 ^ crc ^ 0xedb88320) | 0; }
function xsCommon(a1, b1, dslPair, webBuild = "6.47.2") { const b1Value = String(b1 ?? ""); const json = JSON.stringify({ s0: 5, s1: "", x0: "1", x1: "4.4.3", x2: "Windows", x3: "xhs-pc-web", x4: String(webBuild || "6.47.2"), x5: a1, x6: "", x7: "", x8: b1Value ? b1Value : null, x9: crc32Variant(b1Value || "null"), x10: 0, x11: "normal", x12: String(dslPair || "") }); return customBase64(utf8(json), XS_ALPHABET); }
export function signXhs({ api, data = "", cookieMap = {}, seq = 1, now: inputNow, version: inputVersion, envConst = 1352, envFpTail = "f9416767c9b581635e0744fa8415" }) {
  const a1 = cookieMap.a1 || "";
  if (!a1) throw new XhsSignError("小红书 Cookie 缺少 a1", "XHS_COOKIE_MISSING");
  const now = inputNow == null ? Date.now() : Number(inputNow);
  const version = inputVersion == null ? Math.floor(Math.random() * 0x100000000) >>> 0 : Number(inputVersion) >>> 0;
  const encrypted = pack({ api, data, a1, now, loadts: Number(cookieMap.loadts) || now, seq, version, envConst, envFpTail: hexBytes(envFpTail) }).map((item, index) => item ^ KEYSTREAM[index]);
  const mns = `mns0301_${customBase64(encrypted)}`;
  const dataText = data == null ? "" : typeof data === "object" ? JSON.stringify(data) : String(data);
  const md5Full = hexBytes(md5(api + dataText));
  const xsPayload = JSON.stringify({ x0: "4.4.3", x1: "xhs-pc-web", x2: "Windows", x3: mns, x4: typeof data === "object" ? "object" : data ? "string" : "", x5: md5Full.map((item) => item.toString(16).padStart(2, "0")).join("") });
  const headers = { "x-s": `XYS_${customBase64(utf8(xsPayload), XS_ALPHABET)}`, "x-t": String(now) };
  if (cookieMap._dsl) headers["x-s-common"] = xsCommon(a1, "", `${now};${cookieMap._dsl}`, cookieMap.webbuild || cookieMap.webBuild);
  return headers;
}

