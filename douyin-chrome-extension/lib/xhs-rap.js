// 小红书业务接口 x-rap-param 纯浏览器实现，避免搜索接口因缺少 RAP 返回 406。
const ALPHABET36 = "0123456789abcdefghijklmnopqrstuvwxyz";
const AES_KEY = new TextEncoder().encode("kqI1DTcwKX90ZtAy");
const SBOX = [122,1,88,224,80,78,2,121,29,75,83,218,107,72,212,82,237,119,18,33,20,21,236,16,24,229,185,241,12,8,252,125,249,205,181,200,230,55,38,135,86,186,184,43,173,240,104,247,139,141,211,94,54,77,46,146,49,130,242,41,112,61,45,215,182,64,178,67,68,128,120,210,13,73,74,9,99,108,7,58,158,213,6,198,225,98,244,52,36,89,169,87,42,0,62,23,44,10,26,66,250,147,190,220,245,179,106,19,232,3,199,151,187,115,118,134,227,70,114,71,208,5,76,56,124,31,129,171,117,81,235,243,50,116,17,143,132,137,156,113,34,126,157,207,63,145,105,101,60,109,150,162,152,153,51,57,154,202,195,159,160,188,228,163,164,84,127,167,168,4,111,93,172,183,39,175,176,40,65,174,180,110,11,27,223,142,48,177,254,144,97,96,192,203,92,14,239,22,131,234,32,233,201,85,196,69,133,204,30,170,103,138,123,53,214,25,216,217,194,219,148,221,28,222,166,255,248,191,91,90,15,231,193,189,209,102,197,37,238,140,226,95,136,161,59,165,246,206,149,47,100,35,251,253,79,155];
const FINGERPRINT_HEX = "03ea00000010346c326b326a6a673668696d6472366203ebb0037bbb041b00041c00041d00041e00041f00042000042100042200042300042400042500042600042700042800042900042e00042a00042b00042c00042d00044c00000000042f00043000043100043600000000043a00000000043c00000000044000000000044200000000044900000012000000019f615912f2010000019f617b376d044400000a000446000004f704470000019f615908a50445000000000448000443000000040012ffff048400048500000000048000048100048200048300";

function mul(a, b) { let result = 0; for (let i = 0; i < 8; i += 1) { if (b & 1) result ^= a; const high = a & 0x80; a = (a << 1) & 255; if (high) a ^= 0x1b; b >>= 1; } return result; }
function aesBlock(input) {
  const words = []; const rcon = [1,2,4,8,16,32,64,128,27,54];
  for (let i = 0; i < 4; i += 1) words[i] = [...AES_KEY.slice(i * 4, i * 4 + 4)];
  for (let i = 4; i < 44; i += 1) { let temp = [...words[i - 1]]; if (i % 4 === 0) { temp = [temp[1], temp[2], temp[3], temp[0]].map((value) => SBOX[value]); temp[0] ^= rcon[i / 4 - 1]; } words[i] = words[i - 4].map((value, index) => value ^ temp[index]); }
  const state = new Array(16); for (let column = 0; column < 4; column += 1) for (let row = 0; row < 4; row += 1) state[row * 4 + column] = input[column * 4 + row];
  const addRoundKey = (round) => { for (let column = 0; column < 4; column += 1) for (let row = 0; row < 4; row += 1) state[row * 4 + column] ^= words[round * 4 + column][row]; };
  addRoundKey(0);
  for (let round = 1; round <= 10; round += 1) {
    for (let i = 0; i < 16; i += 1) state[i] = SBOX[state[i]];
    const shifted = [...state]; for (let row = 1; row < 4; row += 1) for (let column = 0; column < 4; column += 1) state[row * 4 + column] = shifted[row * 4 + ((column + row) % 4)];
    if (round < 10) { const source = [...state]; for (let column = 0; column < 4; column += 1) { const a0 = source[column], a1 = source[4 + column], a2 = source[8 + column], a3 = source[12 + column]; state[column] = mul(a0, 2) ^ mul(a1, 3) ^ a2 ^ a3; state[4 + column] = a0 ^ mul(a1, 2) ^ mul(a2, 3) ^ a3; state[8 + column] = a0 ^ a1 ^ mul(a2, 2) ^ mul(a3, 3); state[12 + column] = mul(a0, 3) ^ a1 ^ a2 ^ mul(a3, 2); } }
    addRoundKey(round);
  }
  const output = new Uint8Array(16); for (let column = 0; column < 4; column += 1) for (let row = 0; row < 4; row += 1) output[column * 4 + row] = state[row * 4 + column]; return output;
}
function aesEcb(bytes) { const output = new Uint8Array(bytes.length); for (let offset = 0; offset < bytes.length; offset += 16) output.set(aesBlock(bytes.slice(offset, offset + 16)), offset); return output; }
function xxHash32(bytes) { const imul = Math.imul; const rotl = (value, bits) => ((value << bits) | (value >>> (32 - bits))) >>> 0; const p1 = 0x9e3779b1, p2 = 0x85ebca77, p3 = 0xc2b2ae3d, p4 = 0x27d4eb2f, p5 = 0x165667b1; let offset = 0; let hash; if (bytes.length >= 16) { let v1 = (p1 + p2) >>> 0, v2 = p2 >>> 0, v3 = 0, v4 = (-p1) >>> 0; while (offset + 16 <= bytes.length) { const read = (at) => (bytes[at] | bytes[at + 1] << 8 | bytes[at + 2] << 16 | bytes[at + 3] << 24) >>> 0; v1 = imul(rotl((v1 + imul(read(offset), p2)) >>> 0, 13), p1) >>> 0; v2 = imul(rotl((v2 + imul(read(offset + 4), p2)) >>> 0, 13), p1) >>> 0; v3 = imul(rotl((v3 + imul(read(offset + 8), p2)) >>> 0, 13), p1) >>> 0; v4 = imul(rotl((v4 + imul(read(offset + 12), p2)) >>> 0, 13), p1) >>> 0; offset += 16; } hash = (rotl(v1, 1) + rotl(v2, 7) + rotl(v3, 12) + rotl(v4, 18)) >>> 0; } else hash = p5; hash = (hash + bytes.length) >>> 0; while (offset + 4 <= bytes.length) { const word = (bytes[offset] | bytes[offset + 1] << 8 | bytes[offset + 2] << 16 | bytes[offset + 3] << 24) >>> 0; hash = imul(rotl((hash + imul(word, p3)) >>> 0, 17), p4) >>> 0; offset += 4; } while (offset < bytes.length) { hash = imul(rotl((hash + imul(bytes[offset], p5)) >>> 0, 11), p1) >>> 0; offset += 1; } hash ^= hash >>> 15; hash = imul(hash, p2) >>> 0; hash ^= hash >>> 13; hash = imul(hash, p3) >>> 0; return (hash ^ hash >>> 16) >>> 0; }
function randomBytes(length) { const output = new Uint8Array(length); crypto.getRandomValues(output); return output; }
function randomAscii(length) { return [...randomBytes(length)].map((value) => ALPHABET36[value % 36]).join(""); }
function bytesToBase64(bytes) { let binary = ""; for (const byte of bytes) binary += String.fromCharCode(byte); return btoa(binary); }
async function gzip(bytes) { const stream = new CompressionStream("gzip"); const output = new Response(stream.readable).arrayBuffer(); const writer = stream.writable.getWriter(); await writer.write(bytes); await writer.close(); return new Uint8Array(await output); }

export async function buildXhsRapParam(api, data) {
  const body = typeof data === "string" ? data : JSON.stringify(data || {});
  const url = /^https?:\/\//.test(api) ? api : `//edith.xiaohongshu.com${api}`;
  const mask = ALPHABET36.charCodeAt(randomBytes(1)[0] % 36);
  const xorKey = new TextEncoder().encode(randomAscii(16));
  const nonce = new TextEncoder().encode(randomAscii(4 + Math.floor(randomBytes(1)[0] / 86)));
  const fingerprint = Uint8Array.from(FINGERPRINT_HEX.match(/../g).map((value) => Number.parseInt(value, 16)));
  const payload = new Uint8Array(16 + fingerprint.length); const view = new DataView(payload.buffer); view.setUint16(0, 0x03e8); view.setBigUint64(2, BigInt(Date.now())); view.setUint16(10, 0x03e9); view.setUint32(12, xxHash32(Uint8Array.of(mask)));
  const bodyBytes = new Uint8Array(fingerprint); const requestHash = xxHash32(new TextEncoder().encode(url + body)); const marker = FINGERPRINT_HEX.indexOf("03eb") / 2; if (marker >= 0) new DataView(bodyBytes.buffer).setUint32(marker + 2, requestHash);
  for (let i = 0; i < bodyBytes.length; i += 1) payload[16 + i] = bodyBytes[i] ^ mask;
  const gzipBytes = await gzip(payload); const paddingLength = 16 - (gzipBytes.length % 16); const padded = new Uint8Array(gzipBytes.length + paddingLength); padded.set(gzipBytes); padded.set([...randomBytes(paddingLength)].map((value) => ALPHABET36.charCodeAt(value % 36)), gzipBytes.length);
  const xored = new Uint8Array(padded.length); for (let i = 0; i < padded.length; i += 1) xored[i] = padded[i] ^ xorKey[i % 16]; const encryptedBody = aesEcb(xored); const encryptedKey = aesEcb(xorKey); const envelope = new Uint8Array(60 + nonce.length + encryptedBody.length); const envelopeView = new DataView(envelope.buffer); envelope[0] = 7; envelope[1] = 36; envelope[2] = 1; envelope[3] = nonce.length; envelopeView.setUint32(4, 1); envelopeView.setUint32(8, 20); envelopeView.setUint32(12, encryptedBody.length + 4); envelopeView.setUint32(20, 0x283d); envelopeView.setUint32(24, 1); envelope.set(nonce, 36); envelope.set(encryptedKey, 36 + nonce.length); envelopeView.setUint32(52 + nonce.length, 16); envelope.set(encryptedBody, 56 + nonce.length); envelopeView.setUint32(56 + nonce.length + encryptedBody.length, gzipBytes.length); envelopeView.setUint32(16, xxHash32(envelope.slice(36))); return bytesToBase64(envelope);
}
