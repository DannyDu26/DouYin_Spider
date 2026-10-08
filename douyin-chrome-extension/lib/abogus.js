import { sm3Hash } from "./sm3.js";

const SALT = "dhzx";
const FORTNIGHT_EPOCH = 1721836800000;
const DUMP_CLOSURE_TIME = 1720000000000;
const DUMP_COUNTER_INIT = 2;
const RC4_KEY_BYTE = 211;
const FIXED_NOW = 1720000000000;
const FIXED_RANDOM = 0.4142135623730951;
const ALPHABET_S3 = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe";
const ALPHABET_S4 = "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe";
const FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0";
const FIXED_GEO = [2560, 1297, 2560, 1392, 2560, 1400, 2560, 1440];
const A98_PERM = [
  34, 44, 56, 61, 73, 29, 70, 45, 35, 49, 38, 66, 51, 68, 28, 48, 64, 47,
  30, 71, 26, 55, 31, 69, 59, 40, 62, 63, 27, 72, 41, 74, 57, 52, 42, 39,
  33, 67, 53, 43, 65, 46, 36, 24, 60, 32, 79, 80, 84, 85,
];
const BROWSER_OFFSET = { Chrome: 0, Firefox: 40, Safari: 81, Edge: 125, Huawei: 170 };

function customBase64(data, alphabet) {
  const output = [];
  for (let offset = 0; offset < data.length; offset += 3) {
    const chunk = data.slice(offset, offset + 3);
    const bytes = [...chunk, 0, 0].slice(0, 3);
    const value = (bytes[0] << 16) | (bytes[1] << 8) | bytes[2];
    const indexes = [
      (value >>> 18) & 63,
      (value >>> 12) & 63,
      (value >>> 6) & 63,
      value & 63,
    ];
    if (chunk.length === 1) {
      output.push(alphabet[indexes[0]], alphabet[indexes[1]], "=", "=");
    } else if (chunk.length === 2) {
      output.push(alphabet[indexes[0]], alphabet[indexes[1]], alphabet[indexes[2]], "=");
    } else {
      output.push(...indexes.map((index) => alphabet[index]));
    }
  }
  return output.join("");
}

function rc4Variant(key, data) {
  const state = Array.from({ length: 256 }, (_, index) => 255 - index);
  let j = 0;
  for (let index = 0; index < 256; index += 1) {
    j = (j * state[index] + j + key[index % key.length]) % 256;
    [state[index], state[j]] = [state[j], state[index]];
  }

  const output = [];
  let i = 0;
  j = 0;
  for (const byte of data) {
    i = (i + 1) % 256;
    j = (j + state[i]) % 256;
    [state[i], state[j]] = [state[j], state[i]];
    output.push(byte ^ state[(state[i] + state[j]) % 256]);
  }
  return output;
}

function littleEndianBytes(value, count) {
  const number = BigInt(Math.trunc(value));
  return Array.from(
    { length: count },
    (_, index) => Number((number >> BigInt(index * 8)) & 0xffn),
  );
}

function stringToBytes(value) {
  const output = [];
  for (const character of value) {
    const code = character.codePointAt(0);
    if (code & 0xff00) {
      output.push((code >>> 8) & 255, code & 255);
    } else {
      output.push(code);
    }
  }
  return output;
}

function blend(c0, c1, r0, r1) {
  return [
    (r0 & 170) | (c0 & 85),
    (r0 & 85) | (c0 & 170),
    (r1 & 170) | (c1 & 85),
    (r1 & 85) | (c1 & 170),
  ];
}

function expand(input, random) {
  const output = [];
  for (let index = 0; index < input.length; index += 3) {
    if (index + 2 < input.length) {
      const rand = Math.trunc(random() * 1000) & 255;
      output.push(
        (rand & 145) | (input[index] & 110),
        (rand & 66) | (input[index + 1] & 189),
        (rand & 44) | (input[index + 2] & 211),
        (input[index] & 145) | (input[index + 1] & 66) | (input[index + 2] & 44),
      );
    } else {
      output.push(input[index]);
      if (index + 1 < input.length && input[index + 1]) {
        output.push(input[index + 1]);
      }
    }
  }
  return output;
}

function browserName(userAgent) {
  const patterns = [
    ["Huawei", [/\bhuawei\b/i]],
    ["Chrome", [/(chrome)\/([\w.]+)(?!.*chromium)/i]],
    ["Edge", [/(edg|edge)\/([\w.]+)/i]],
    ["Firefox", [/\bfocus\/([\w.]+)/i, /fxios\/([-\w.]+)/i, /(firefox)\/([\w.]+)/i]],
    ["IE", [/(msie |trident.*rv:)([\w.]+)/i]],
    ["Opera", [/(opera|opr)\/([\w.]+)/i]],
    ["Safari", [/(safari)\/([\w.]+)(?!.*chrome)/i]],
  ];
  return patterns.find(([, regexes]) => regexes.some((regex) => regex.test(userAgent)))?.[0] ?? "Other";
}

function escapeDigest(digest, index, reserved, fallback, force) {
  let value = digest[index] ?? fallback;
  let cursor = index;
  while (value === reserved) {
    cursor += 1;
    value = digest[cursor] ?? fallback;
  }
  return force ? reserved : value;
}

export class ABogusSigner {
  constructor({ fixed = false, userAgent, geometry, mainSite = false } = {}) {
    this.fixed = fixed;
    // 用户作品使用已验证的新主站参数，其余接口保留原签名配置。
    this.mainSite = mainSite;
    this.userAgent = userAgent || (fixed ? FIREFOX_UA : navigator.userAgent);
    this.geometry = geometry || (fixed ? (mainSite ? [2560, 1215, 2560, 1392, 2560, 1392, 2560, 1440] : FIXED_GEO) : [
      window.innerWidth,
      window.innerHeight,
      window.outerWidth,
      window.outerHeight,
      screen.availWidth,
      screen.availHeight,
      screen.width,
      screen.height,
    ]);
    this.offsetName = fixed ? "Firefox" : browserName(this.userAgent);
    this.counter = DUMP_COUNTER_INIT;
  }

  now() {
    return this.fixed ? FIXED_NOW : Date.now();
  }

  random() {
    return this.fixed ? FIXED_RANDOM : Math.random();
  }

  signQuery(query, body = "") {
    const values = {};
    this.counter += 1;
    values[12] = 3;
    const timestamp = this.now();
    values[14] = timestamp;
    const h1 = sm3Hash(sm3Hash(new TextEncoder().encode(query + SALT)));
    const h2 = sm3Hash(sm3Hash(new TextEncoder().encode(body + SALT)));
    const sdkVersion = this.mainSite ? 1 : 129;
    const sdkMinor = this.mainSite ? 8 : 14;
    const uaCipher = rc4Variant([0, sdkVersion, sdkMinor], [...this.userAgent.trim()].map((char) => char.charCodeAt(0)));
    const hUa = sm3Hash(new TextEncoder().encode(customBase64(uaCipher, ALPHABET_S3)));
    const flags = 1 | (this.mainSite ? 0 : (1 << 1)) | (Number(this.fixed) << 2)
      | (Number(browserName(this.userAgent) === "Firefox") << 5);

    values[23] = [3, 82];
    values[24] = 41;
    values[25] = [1, 0, 1, 0, 1];
    values[26] = Math.trunc((timestamp - FORTNIGHT_EPOCH) / 1000 / 60 / 60 / 24 / 14);
    values[27] = this.counter > 10745 ? 3 : this.counter > 1283 ? 4 : this.counter > 139 ? 5 : 6;
    const closureTime = this.fixed ? DUMP_CLOSURE_TIME : timestamp;
    values[28] = closureTime > 0 ? (timestamp - closureTime + 3) & 255 : 2;
    littleEndianBytes(timestamp, 6).forEach((byte, index) => { values[29 + index] = byte; });
    [values[35], values[36]] = littleEndianBytes(sdkVersion, 2);
    values[37] = [0, 0, 0, 0, flags];
    [values[38], values[39]] = littleEndianBytes(flags, 2);
    for (let index = 40; index < 44; index += 1) values[index] = 0;
    if (this.mainSite) [values[40], values[41], values[42]] = [132, 1, 1];
    littleEndianBytes(sdkMinor, 4).forEach((byte, index) => { values[44 + index] = byte; });
    values[48] = h1[9];
    values[49] = h1[18];
    values[51] = escapeDigest(h1, 3, 11, 12, Boolean(flags & 2));
    values[52] = h2[10];
    values[53] = h2[19];
    values[55] = escapeDigest(h2, 4, 8, 9, Boolean(flags & 4));
    values[56] = hUa[11];
    values[57] = hUa[21];
    values[59] = escapeDigest(hUa, 5, 12, 13, Boolean(flags & 8));
    littleEndianBytes(timestamp - 1, 6).forEach((byte, index) => { values[60 + index] = byte; });
    values[66] = values[12];
    littleEndianBytes(this.mainSite ? 11881 : 6383, 4).forEach((byte, index) => { values[67 + index] = byte; });
    littleEndianBytes(6383, 4).forEach((byte, index) => { values[71 + index] = byte; });

    values[77] = stringToBytes([...this.geometry, "Win32"].join("|"));
    values[78] = values[77].length;
    [values[79], values[80]] = littleEndianBytes(values[78], 2);
    values[81] = `${(timestamp + 3) & 255},`;
    values[82] = stringToBytes(values[81]);
    values[83] = values[82].length;
    [values[84], values[85]] = littleEndianBytes(values[83], 2);

    const random16 = this.random() * 65535;
    let a8 = blend(values[25][0], values[25][1], Math.trunc(random16) & 255, (Math.trunc(random16) >>> 8) & 255);
    this.random();
    const randomCheck = Math.trunc(this.random() * (flags & 64 ? 109 : 240));
    const check = flags & 64
      ? randomCheck + 110 + (randomCheck % 2)
      : randomCheck > 109 ? randomCheck + (randomCheck % 2) + 1 : randomCheck;
    const permissions = (Math.trunc(this.random() * 255) & 77) | 2 | 16 | 32 | 128;
    a8 = a8.concat(blend(values[25][2], values[25][3], check, permissions));
    values[86] = a8;

    const checksumIndexes = [
      24, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 38, 39, 40, 41, 42, 43,
      44, 45, 46, 47, 48, 49, 51, 52, 53, 55, 56, 57, 59, 60, 61, 62, 63, 64,
      65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 79, 80, 84, 85,
    ];
    values[87] = [...a8, ...checksumIndexes.map((index) => values[index])]
      .reduce((checksum, byte) => checksum ^ byte, 0) | 0;
    const a98 = A98_PERM.map((index) => values[index])
      .concat(values[77], values[82], values[87]);
    const headerRandom = Math.trunc(this.random() * 65535) & 255;
    const offset = Math.trunc(this.random() * 40) + (BROWSER_OFFSET[this.offsetName] ?? 210);
    values[89] = blend(3, 82, headerRandom, offset);
    const plain = a8.concat(expand(a98, () => this.random()));
    const cipher = rc4Variant([RC4_KEY_BYTE], plain.map((value) => value & 0xffff));
    return customBase64(values[89].concat(cipher.map((value) => value & 255)), ALPHABET_S4);
  }
}
