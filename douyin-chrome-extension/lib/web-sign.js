// 与后端 utils/user_read/web_sign.py 保持一致的浏览器端 webSign。
const SALT = "A96D855A08C0A9707F8BEF0D9A527E4E";
const SHIFTS = [7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21];
const CONSTANTS = Array.from({ length: 64 }, (_, i) => Math.floor(Math.abs(Math.sin(i + 1)) * 2 ** 32));

export function md5(value) {
  // Web Crypto 不提供 MD5，在本地计算协议要求的摘要。
  const input = new TextEncoder().encode(value);
  const buffer = new Uint8Array(Math.ceil((input.length + 9) / 64) * 64);
  buffer.set(input);
  buffer[input.length] = 128;
  const view = new DataView(buffer.buffer);
  view.setUint32(buffer.length - 8, (input.length * 8) >>> 0, true);
  view.setUint32(buffer.length - 4, Math.floor(input.length / 2 ** 29), true);
  const state = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476];
  for (let offset = 0; offset < buffer.length; offset += 64) {
    let [a, b, c, d] = state;
    for (let i = 0; i < 64; i += 1) {
      let f;
      let word;
      if (i < 16) { f = (b & c) | (~b & d); word = i; }
      else if (i < 32) { f = (d & b) | (~d & c); word = (5 * i + 1) % 16; }
      else if (i < 48) { f = b ^ c ^ d; word = (3 * i + 5) % 16; }
      else { f = c ^ (b | ~d); word = (7 * i) % 16; }
      const sum = (a + f + CONSTANTS[i] + view.getUint32(offset + word * 4, true)) | 0;
      const shift = SHIFTS[Math.floor(i / 16) * 4 + i % 4];
      [a, b, c, d] = [d, (b + ((sum << shift) | (sum >>> (32 - shift)))) | 0, b, c];
    }
    [a, b, c, d].forEach((value, i) => { state[i] = (state[i] + value) | 0; });
  }
  return state.map((word) => [0, 8, 16, 24]
    .map((shift) => ((word >>> shift) & 255).toString(16).padStart(2, "0")).join("")).join("");
}

export function signWebUrl(url, { timestamp = Math.floor(Date.now() / 1000), uifid = "" } = {}) {
  const separator = url.indexOf("?");
  const base = separator < 0 ? url : url.slice(0, separator);
  const raw = separator < 0 ? "" : url.slice(separator + 1);
  // URLSearchParams 只用于解码；输出使用 encodeURIComponent，保留顺序和空值。
  const entries = [...new URLSearchParams(raw)]
    .filter(([key]) => !["timestamp", "x-secsdk-web-signature"].includes(key));
  if (uifid && !entries.some(([key]) => key === "uifid")) entries.push(["uifid", uifid]);
  const query = entries.map(([key, value]) => `${key}=${encodeURIComponent(value)}`);
  query.push(`timestamp=${timestamp}`);
  const canonical = query.join("&");
  const identity = entries.find(([key]) => key === "uifid")?.[1] || uifid;
  const signature = md5(`${identity}_${timestamp}_${SALT}_${canonical}`);
  return `${base}?${canonical}&x-secsdk-web-signature=${signature}`;
}
