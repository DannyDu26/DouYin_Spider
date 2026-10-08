const IV = [
  0x7380166f, 0x4914b2b9, 0x172442d7, 0xda8a0600,
  0xa96f30bc, 0x163138aa, 0xe38dee4d, 0xb0fb0e4e,
];

function rotl(value, bits) {
  const shift = bits & 31;
  return ((value << shift) | (value >>> ((32 - shift) & 31))) >>> 0;
}

function p0(value) {
  return (value ^ rotl(value, 9) ^ rotl(value, 17)) >>> 0;
}

function p1(value) {
  return (value ^ rotl(value, 15) ^ rotl(value, 23)) >>> 0;
}

function ff(x, y, z, index) {
  if (index < 16) {
    return (x ^ y ^ z) >>> 0;
  }
  return ((x & y) | (x & z) | (y & z)) >>> 0;
}

function gg(x, y, z, index) {
  if (index < 16) {
    return (x ^ y ^ z) >>> 0;
  }
  return ((x & y) | ((~x) & z)) >>> 0;
}

function padMessage(input) {
  const bitLength = BigInt(input.length) * 8n;
  const paddedLength = Math.ceil((input.length + 9) / 64) * 64;
  const output = new Uint8Array(paddedLength);
  output.set(input);
  output[input.length] = 0x80;
  for (let index = 0; index < 8; index += 1) {
    output[paddedLength - 1 - index] = Number((bitLength >> BigInt(index * 8)) & 0xffn);
  }
  return output;
}

function compress(vector, block) {
  const words = new Uint32Array(68);
  const expanded = new Uint32Array(64);
  const view = new DataView(block.buffer, block.byteOffset, block.byteLength);

  for (let index = 0; index < 16; index += 1) {
    words[index] = view.getUint32(index * 4, false);
  }
  for (let index = 16; index < 68; index += 1) {
    words[index] = (
      p1(words[index - 16] ^ words[index - 9] ^ rotl(words[index - 3], 15))
      ^ rotl(words[index - 13], 7)
      ^ words[index - 6]
    ) >>> 0;
  }
  for (let index = 0; index < 64; index += 1) {
    expanded[index] = (words[index] ^ words[index + 4]) >>> 0;
  }

  let [a, b, c, d, e, f, g, h] = vector;
  for (let index = 0; index < 64; index += 1) {
    const constant = index < 16 ? 0x79cc4519 : 0x7a879d8a;
    const ss1 = rotl((rotl(a, 12) + e + rotl(constant, index)) >>> 0, 7);
    const ss2 = (ss1 ^ rotl(a, 12)) >>> 0;
    const tt1 = (ff(a, b, c, index) + d + ss2 + expanded[index]) >>> 0;
    const tt2 = (gg(e, f, g, index) + h + ss1 + words[index]) >>> 0;
    d = c;
    c = rotl(b, 9);
    b = a;
    a = tt1;
    h = g;
    g = rotl(f, 19);
    f = e;
    e = p0(tt2);
  }

  return [a, b, c, d, e, f, g, h].map(
    (value, index) => (value ^ vector[index]) >>> 0,
  );
}

export function sm3Hash(input) {
  const bytes = typeof input === "string" ? new TextEncoder().encode(input) : input;
  const padded = padMessage(bytes);
  let vector = [...IV];
  for (let offset = 0; offset < padded.length; offset += 64) {
    vector = compress(vector, padded.subarray(offset, offset + 64));
  }

  const output = new Uint8Array(32);
  const view = new DataView(output.buffer);
  vector.forEach((value, index) => view.setUint32(index * 4, value, false));
  return output;
}

export function sm3Hex(input) {
  return [...sm3Hash(input)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}
