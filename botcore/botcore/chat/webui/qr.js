/* Tacitus web chat: a QR code encoder for the relay's pairing link
 * (docs/webchat-relay.md). Byte mode, error correction level M, versions 1
 * to 40 (ISO/IEC 18004); a pairing link of about 450 bytes needs version 17
 * or so. QR.encode(text) gives the grid of modules (true is dark);
 * QR.draw(canvas, text) paints it dark on light with the four-module
 * quiet zone. No library. */
'use strict';

const QR = (() => {
  // Level M, by version: error correction codewords per block, and blocks.
  const ECC = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
    26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28];
  const BLOCKS = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18,
    20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49];
  const LEVEL_BITS = 0; // level M in the format information

  // The modules left for data and error correction once the function
  // patterns are drawn.
  function rawModules(v) {
    let n = (16 * v + 128) * v + 64;
    if (v >= 2) {
      const a = Math.floor(v / 7) + 2;
      n -= (25 * a - 10) * a - 55;
      if (v >= 7) n -= 36;
    }
    return n;
  }
  const dataCodewords = (v) => Math.floor(rawModules(v) / 8) - ECC[v] * BLOCKS[v];

  // Reed-Solomon over GF(256), the polynomial x^8 + x^4 + x^3 + x^2 + 1.
  function mul(x, y) {
    let z = 0;
    for (let i = 7; i >= 0; i--) {
      z = (z << 1) ^ ((z >>> 7) * 0x11d);
      z ^= ((y >>> i) & 1) * x;
    }
    return z;
  }

  function divisor(degree) {
    const out = new Array(degree).fill(0);
    out[degree - 1] = 1;
    let root = 1;
    for (let i = 0; i < degree; i++) {
      for (let j = 0; j < degree; j++) {
        out[j] = mul(out[j], root);
        if (j + 1 < degree) out[j] ^= out[j + 1];
      }
      root = mul(root, 2);
    }
    return out;
  }

  function remainder(data, div) {
    const out = div.map(() => 0);
    for (const b of data) {
      const f = b ^ out.shift();
      out.push(0);
      div.forEach((c, i) => { out[i] ^= mul(c, f); });
    }
    return out;
  }

  // The data codewords split into blocks, each given its error correction
  // codewords, then interleaved column by column.
  function interleave(v, data) {
    const nb = BLOCKS[v];
    const ecc = ECC[v];
    const raw = Math.floor(rawModules(v) / 8);
    const short = nb - (raw % nb);
    const shortLen = Math.floor(raw / nb);
    const div = divisor(ecc);
    const blocks = [];
    for (let i = 0, k = 0; i < nb; i++) {
      const dat = data.slice(k, k + shortLen - ecc + (i < short ? 0 : 1));
      k += dat.length;
      const tail = remainder(dat, div);
      if (i < short) dat.push(0);
      blocks.push(dat.concat(tail));
    }
    const out = [];
    for (let i = 0; i < blocks[0].length; i++) {
      blocks.forEach((b, j) => { if (i !== shortLen - ecc || j >= short) out.push(b[i]); });
    }
    return out;
  }

  function alignment(v) {
    if (v === 1) return [];
    const n = Math.floor(v / 7) + 2;
    const step = Math.floor((v * 8 + n * 3 + 5) / (n * 4 - 4)) * 2;
    const out = [6];
    for (let pos = v * 4 + 10; out.length < n; pos -= step) out.splice(1, 0, pos);
    return out;
  }

  const MASKS = [
    (x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, (x) => x % 3 === 0,
    (x, y) => (x + y) % 3 === 0, (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0,
    (x, y) => (x * y) % 2 + (x * y) % 3 === 0, (x, y) => ((x * y) % 2 + (x * y) % 3) % 2 === 0,
    (x, y) => ((x + y) % 2 + (x * y) % 3) % 2 === 0,
  ];

  // The spec's penalty: runs of five or more, 2x2 blocks, finder-like
  // runs, and a dark share far from half. The lowest picks the mask.
  function penalty(grid) {
    const n = grid.length;
    const lines = [];
    for (let i = 0; i < n; i++) {
      lines.push(grid[i].map(Number).join(''));
      lines.push(grid.map((row) => Number(row[i])).join(''));
    }
    let p = 0;
    let dark = 0;
    for (const ln of lines) {
      for (const run of ln.match(/0+|1+/g)) if (run.length >= 5) p += run.length - 2;
      const padded = `0000${ln}0000`;
      for (const pat of ['10111010000', '00001011101']) {
        for (let at = padded.indexOf(pat); at >= 0; at = padded.indexOf(pat, at + 1)) p += 40;
      }
    }
    for (let y = 0; y < n; y++) {
      for (let x = 0; x < n; x++) {
        if (grid[y][x]) dark++;
        if (x < n - 1 && y < n - 1) {
          const c = grid[y][x];
          if (c === grid[y][x + 1] && c === grid[y + 1][x] && c === grid[y + 1][x + 1]) p += 3;
        }
      }
    }
    const total = n * n;
    return p + (Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * 10;
  }

  function place(v, codewords) {
    const size = v * 4 + 17;
    const grid = Array.from({ length: size }, () => new Array(size).fill(false));
    const fixed = Array.from({ length: size }, () => new Array(size).fill(false));
    const set = (x, y, on) => { grid[y][x] = on; fixed[y][x] = true; };
    for (let i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
    for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) {
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const x = cx + dx;
          const y = cy + dy;
          const d = Math.max(Math.abs(dx), Math.abs(dy));
          if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, d !== 2 && d !== 4);
        }
      }
    }
    const pos = alignment(v);
    const last = pos.length - 1;
    pos.forEach((y, i) => pos.forEach((x, j) => {
      if ((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0)) return;
      for (let dy = -2; dy <= 2; dy++) {
        for (let dx = -2; dx <= 2; dx++) set(x + dx, y + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
      }
    }));
    const format = (mask) => {
      const data = (LEVEL_BITS << 3) | mask;
      let rem = data;
      for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
      const bits = ((data << 10) | rem) ^ 0x5412;
      const bit = (i) => ((bits >>> i) & 1) === 1;
      for (let i = 0; i <= 5; i++) set(8, i, bit(i));
      set(8, 7, bit(6));
      set(8, 8, bit(7));
      set(7, 8, bit(8));
      for (let i = 9; i < 15; i++) set(14 - i, 8, bit(i));
      for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(i));
      for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(i));
      set(8, size - 8, true);
    };
    format(0); // reserves the format areas; drawn again with the mask
    if (v >= 7) {
      let rem = v;
      for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
      const bits = (v << 12) | rem;
      for (let i = 0; i < 18; i++) {
        const on = ((bits >>> i) & 1) === 1;
        set(size - 11 + (i % 3), Math.floor(i / 3), on);
        set(Math.floor(i / 3), size - 11 + (i % 3), on);
      }
    }
    // The codewords, in two-module columns zigzagging from the bottom right.
    let i = 0;
    for (let right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vert = 0; vert < size; vert++) {
        for (let j = 0; j < 2; j++) {
          const x = right - j;
          const y = ((right + 1) & 2) === 0 ? size - 1 - vert : vert;
          if (!fixed[y][x] && i < codewords.length * 8) {
            grid[y][x] = ((codewords[i >>> 3] >>> (7 - (i & 7))) & 1) === 1;
            i++;
          }
        }
      }
    }
    const flip = (mask) => {
      for (let y = 0; y < size; y++) {
        for (let x = 0; x < size; x++) if (!fixed[y][x] && MASKS[mask](x, y)) grid[y][x] = !grid[y][x];
      }
    };
    let best = 0;
    let bestScore = Infinity;
    for (let m = 0; m < 8; m++) {
      flip(m);
      format(m);
      const score = penalty(grid);
      if (score < bestScore) { best = m; bestScore = score; }
      flip(m);
    }
    flip(best);
    format(best);
    return grid;
  }

  function encode(text) {
    const bytes = new TextEncoder().encode(String(text));
    let v = 1;
    while (v <= 40 && 4 + (v < 10 ? 8 : 16) + bytes.length * 8 > dataCodewords(v) * 8) v++;
    if (v > 40) throw new RangeError('the text is too long for a QR code');
    const bits = [];
    const put = (val, n) => { for (let i = n - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    put(4, 4); // byte mode
    put(bytes.length, v < 10 ? 8 : 16);
    bytes.forEach((b) => put(b, 8));
    const cap = dataCodewords(v) * 8;
    put(0, Math.min(4, cap - bits.length));
    put(0, (8 - (bits.length % 8)) % 8);
    const data = [];
    for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((a, b) => (a << 1) | b, 0));
    for (let pad = 0xec; data.length < cap / 8; pad ^= 0xec ^ 0x11) data.push(pad);
    return place(v, interleave(v, data));
  }

  function draw(canvas, text, scale = 4) {
    const grid = encode(text);
    const n = grid.length + 8;
    canvas.width = canvas.height = n * scale;
    const g = canvas.getContext('2d');
    g.fillStyle = '#fff';
    g.fillRect(0, 0, n * scale, n * scale);
    g.fillStyle = '#000';
    grid.forEach((row, y) => row.forEach((on, x) => {
      if (on) g.fillRect((x + 4) * scale, (y + 4) * scale, scale, scale);
    }));
    return grid.length;
  }

  return { encode, draw };
})();
