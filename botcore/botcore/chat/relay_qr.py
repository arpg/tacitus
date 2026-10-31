"""A QR code for the relay's pairing link in a terminal (docs/webchat-relay.md).

The same encoder as the web app's `webui/qr.js`, module for module: byte
mode, error correction level M, versions 1 to 40 (ISO/IEC 18004), the
mask with the lowest penalty. `encode(text)` gives the grid of modules
(True is dark); `terminal(text)` draws it with half blocks, two rows of
modules per line, dark on light whatever the terminal's colours, with
the four-module quiet zone. No library.
"""
from __future__ import annotations

import re

# Level M, by version: error correction codewords per block, and blocks.
ECC = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
       26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28]
BLOCKS = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18,
          20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49]
LEVEL_BITS = 0  # level M in the format information


def _raw_modules(v: int) -> int:
    """The modules left for data and error correction once the function
    patterns are drawn."""
    n = (16 * v + 128) * v + 64
    if v >= 2:
        a = v // 7 + 2
        n -= (25 * a - 10) * a - 55
        if v >= 7:
            n -= 36
    return n


def _data_codewords(v: int) -> int:
    return _raw_modules(v) // 8 - ECC[v] * BLOCKS[v]


def _mul(x: int, y: int) -> int:
    """Multiplication in GF(256), the polynomial x^8 + x^4 + x^3 + x^2 + 1."""
    z = 0
    for i in range(7, -1, -1):
        z = ((z << 1) ^ ((z >> 7) * 0x11D)) & 0xFF
        z ^= ((y >> i) & 1) * x
    return z


def _divisor(degree: int) -> list[int]:
    out = [0] * degree
    out[-1] = 1
    root = 1
    for _ in range(degree):
        for j in range(degree):
            out[j] = _mul(out[j], root)
            if j + 1 < degree:
                out[j] ^= out[j + 1]
        root = _mul(root, 2)
    return out


def _remainder(data: list[int], div: list[int]) -> list[int]:
    out = [0] * len(div)
    for b in data:
        f = b ^ out.pop(0)
        out.append(0)
        for i, c in enumerate(div):
            out[i] ^= _mul(c, f)
    return out


def _interleave(v: int, data: list[int]) -> list[int]:
    """The data codewords split into blocks, each given its error
    correction codewords, then interleaved column by column."""
    nb, ecc = BLOCKS[v], ECC[v]
    raw = _raw_modules(v) // 8
    short = nb - raw % nb
    short_len = raw // nb
    div = _divisor(ecc)
    blocks, k = [], 0
    for i in range(nb):
        dat = data[k:k + short_len - ecc + (0 if i < short else 1)]
        k += len(dat)
        tail = _remainder(dat, div)
        if i < short:
            dat = dat + [0]
        blocks.append(dat + tail)
    out = []
    for i in range(len(blocks[0])):
        for j, b in enumerate(blocks):
            if i != short_len - ecc or j >= short:
                out.append(b[i])
    return out


def _alignment(v: int) -> list[int]:
    if v == 1:
        return []
    n = v // 7 + 2
    step = (v * 8 + n * 3 + 5) // (n * 4 - 4) * 2
    out = [6]
    pos = v * 4 + 10
    while len(out) < n:
        out.insert(1, pos)
        pos -= step
    return out


MASKS = [
    lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: (x * y) % 2 + (x * y) % 3 == 0, lambda x, y: ((x * y) % 2 + (x * y) % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + (x * y) % 3) % 2 == 0,
]
_RUNS = re.compile(r"0{5,}|1{5,}")


def _penalty(grid: list[list[bool]]) -> int:
    """The spec's penalty: runs of five or more, 2x2 blocks, finder-like
    runs, and a dark share far from half. The lowest picks the mask."""
    n = len(grid)
    lines = []
    for i in range(n):
        lines.append("".join("1" if c else "0" for c in grid[i]))
        lines.append("".join("1" if row[i] else "0" for row in grid))
    p = 0
    for ln in lines:
        p += sum(len(run) - 2 for run in _RUNS.findall(ln))
        padded = f"0000{ln}0000"
        for pat in ("10111010000", "00001011101"):
            at = padded.find(pat)
            while at >= 0:
                p += 40
                at = padded.find(pat, at + 1)
    dark = 0
    for y in range(n):
        for x in range(n):
            c = grid[y][x]
            dark += c
            if x < n - 1 and y < n - 1 and c == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                p += 3
    total = n * n
    return p + (-(-abs(dark * 20 - total * 10) // total) - 1) * 10


def _place(v: int, codewords: list[int]) -> list[list[bool]]:
    size = v * 4 + 17
    grid = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x: int, y: int, on: bool) -> None:
        grid[y][x] = on
        fixed[y][x] = True

    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                d = max(abs(dx), abs(dy))
                if 0 <= x < size and 0 <= y < size:
                    put(x, y, d not in (2, 4))
    pos = _alignment(v)
    last = len(pos) - 1
    for i, y in enumerate(pos):
        for j, x in enumerate(pos):
            if (i, j) in ((0, 0), (0, last), (last, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(x + dx, y + dy, max(abs(dx), abs(dy)) != 1)

    def fmt(mask: int) -> None:
        data = (LEVEL_BITS << 3) | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6))
        put(8, 8, bit(7))
        put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, size - 15 + i, bit(i))
        put(8, size - 8, True)

    fmt(0)  # reserves the format areas; drawn again with the mask
    if v >= 7:
        rem = v
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (v << 12) | rem
        for i in range(18):
            on = (bits >> i) & 1 == 1
            put(size - 11 + i % 3, i // 3, on)
            put(i // 3, size - 11 + i % 3, on)
    # The codewords, in two-module columns zigzagging from the bottom right.
    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not fixed[y][x] and i < len(codewords) * 8:
                    grid[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2

    def flip(mask: int) -> None:
        test = MASKS[mask]
        for y in range(size):
            for x in range(size):
                if not fixed[y][x] and test(x, y):
                    grid[y][x] = not grid[y][x]

    best, best_score = 0, None
    for m in range(8):
        flip(m)
        fmt(m)
        score = _penalty(grid)
        if best_score is None or score < best_score:
            best, best_score = m, score
        flip(m)
    flip(best)
    fmt(best)
    return grid


def encode(text: str) -> list[list[bool]]:
    """The QR code of `text` (UTF-8, byte mode, level M) as rows of
    modules, True for dark."""
    data_bytes = str(text).encode("utf-8")
    v = 1
    while v <= 40 and 4 + (8 if v < 10 else 16) + len(data_bytes) * 8 > _data_codewords(v) * 8:
        v += 1
    if v > 40:
        raise ValueError("the text is too long for a QR code")
    bits: list[int] = []

    def put(val: int, n: int) -> None:
        bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))
    put(4, 4)  # byte mode
    put(len(data_bytes), 8 if v < 10 else 16)
    for b in data_bytes:
        put(b, 8)
    cap = _data_codewords(v) * 8
    put(0, min(4, cap - len(bits)))
    put(0, (8 - len(bits) % 8) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < cap // 8:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    return _place(v, _interleave(v, data))


def terminal(text: str) -> str:
    """The code as text for a terminal: two rows of modules per line in
    half blocks, black on a white background set by ANSI codes, so it
    scans on a dark terminal and a light one alike."""
    grid = encode(text)
    n = len(grid) + 8
    rows = [[False] * n for _ in range(4)] + \
        [[False] * 4 + list(r) + [False] * 4 for r in grid] + [[False] * n for _ in range(4)]
    rows.append([False] * n)  # an even number of rows
    glyph = {(False, False): " ", (True, False): "▀", (False, True): "▄",
             (True, True): "█"}
    lines = []
    for y in range(0, len(rows) - 1, 2):
        cells = "".join(glyph[(rows[y][x], rows[y + 1][x])] for x in range(n))
        lines.append(f"\x1b[30;107m{cells}\x1b[0m")
    return "\n".join(lines)
