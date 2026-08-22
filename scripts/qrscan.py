"""Minimal QR front-end: binarise, find the finder patterns, sample the grid.

This is the part of a decoder that decides whether a printed symbol is legible.
It is deliberately not a full decoder -- there is no Reed-Solomon here -- but
the grid it recovers is the same grid a real scanner recovers, so comparing the
sampled modules against the known-good matrix says whether the symbol survived
the paper and the camera.

Finder location follows the standard 1:1:3:1:1 dark/light run-ratio scan
(ISO/IEC 18004 annex). That ratio is what makes it robust to thermal bleed: ink
spreading outward moves every edge of the pattern by the same amount and leaves
its centre where it was.
"""

from __future__ import annotations

from PIL import Image

# How far a run of five may stray from the ideal 1:1:3:1:1 and still count.
RATIO_TOLERANCE = 0.5


def otsu_threshold(gray: Image.Image) -> int:
    """Global threshold by Otsu's method, as most scanners' binarisers approximate."""
    hist = gray.histogram()[:256]
    total = sum(hist)
    if total == 0:
        return 128
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_bg = 0.0
    w_bg = 0
    best_var, best_t = -1.0, 128
    for t in range(256):
        w_bg += hist[t]
        if w_bg == 0:
            continue
        w_fg = total - w_bg
        if w_fg == 0:
            break
        sum_bg += t * hist[t]
        mean_bg = sum_bg / w_bg
        mean_fg = (sum_all - sum_bg) / w_fg
        var = w_bg * w_fg * (mean_bg - mean_fg) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def binarise(gray: Image.Image) -> tuple[list[list[bool]], int, int]:
    """Return a dark/light bitmap (True = dark) plus its dimensions."""
    # Otsu returns the last level of the dark class, so the test is inclusive.
    # With `<` a pure 0/255 raster binarises to all-light and nothing is found.
    t = otsu_threshold(gray)
    w, h = gray.size
    px = gray.load()
    return [[px[x, y] <= t for x in range(w)] for y in range(h)], w, h


def _ratio_ok(runs: list[int]) -> bool:
    """Does a dark-light-dark-light-dark run match 1:1:3:1:1?"""
    total = sum(runs)
    if total < 7:
        return False
    unit = total / 7.0
    tol = unit * RATIO_TOLERANCE
    want = (1, 1, 3, 1, 1)
    return all(abs(run - w * unit) < tol * (w if w == 3 else 1) for run, w in zip(runs, want))


def _runs(line: list[bool]) -> list[tuple[bool, int, int]]:
    """Split a scan line into (value, start, length) runs."""
    out = []
    start = 0
    for i in range(1, len(line) + 1):
        if i == len(line) or line[i] != line[start]:
            out.append((line[start], start, i - start))
            start = i
    return out


def _scan_line(line: list[bool]) -> list[tuple[float, float]]:
    """Find 1:1:3:1:1 centres along one row/column. Returns (centre, module_px)."""
    hits = []
    runs = _runs(line)
    for i in range(len(runs) - 4):
        window = runs[i : i + 5]
        if not window[0][0]:  # must start on a dark run
            continue
        lengths = [r[2] for r in window]
        if _ratio_ok(lengths):
            middle = window[2]
            hits.append((middle[1] + middle[2] / 2.0, sum(lengths) / 7.0))
    return hits


def find_finders(bitmap: list[list[bool]], w: int, h: int) -> list[tuple[float, float, float]]:
    """Locate finder-pattern centres. Returns up to three (x, y, module_px)."""
    candidates: list[tuple[float, float, float]] = []
    step = max(1, h // 400)
    for y in range(0, h, step):
        for cx, unit in _scan_line(bitmap[y]):
            # confirm vertically through the candidate centre
            col = [bitmap[yy][int(cx)] for yy in range(h)] if 0 <= int(cx) < w else []
            if not col:
                continue
            for cy, vunit in _scan_line(col):
                if abs(cy - y) <= unit * 2 and abs(vunit - unit) < unit * 0.6:
                    candidates.append((cx, cy, (unit + vunit) / 2))
                    break

    # cluster candidates that sit within a couple of modules of each other
    clusters: list[list[tuple[float, float, float]]] = []
    for c in candidates:
        for cl in clusters:
            if abs(cl[0][0] - c[0]) < c[2] * 3 and abs(cl[0][1] - c[1]) < c[2] * 3:
                cl.append(c)
                break
        else:
            clusters.append([c])
    clusters.sort(key=len, reverse=True)
    out = []
    for cl in clusters[:3]:
        n = len(cl)
        out.append((sum(c[0] for c in cl) / n, sum(c[1] for c in cl) / n, sum(c[2] for c in cl) / n))
    return out


def _orient(finders: list[tuple[float, float, float]]):
    """Pick which finder is top-left, and return its two axis neighbours."""

    def d2(a, b):
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    # the top-left finder is the one opposite the longest side (the diagonal)
    a, b, c = finders
    pairs = [(d2(b, c), a, b, c), (d2(a, c), b, a, c), (d2(a, b), c, a, b)]
    _, tl, p, q = max(pairs)
    # cross product decides which of p/q is along the top edge
    vp = (p[0] - tl[0], p[1] - tl[1])
    vq = (q[0] - tl[0], q[1] - tl[1])
    if vp[0] * vq[1] - vp[1] * vq[0] > 0:
        return tl, p, q  # p = top-right, q = bottom-left
    return tl, q, p


def sample_modules(gray: Image.Image, modules: int) -> list[list[bool]] | None:
    """Recover the module matrix. Returns None if the symbol could not be found."""
    bitmap, w, h = binarise(gray)
    finders = find_finders(bitmap, w, h)
    if len(finders) < 3:
        return None
    tl, tr, bl = _orient(finders)

    # finder centres sit on module (3.5, 3.5) and (3.5, modules-3.5)
    span = modules - 7
    if span <= 0:
        return None
    ux = ((tr[0] - tl[0]) / span, (tr[1] - tl[1]) / span)  # one module along +x
    uy = ((bl[0] - tl[0]) / span, (bl[1] - tl[1]) / span)  # one module along +y

    out = []
    for r in range(modules):
        row = []
        for c in range(modules):
            dc, dr = c - 3.5, r - 3.5
            x = tl[0] + dc * ux[0] + dr * uy[0]
            y = tl[1] + dc * ux[1] + dr * uy[1]
            xi = min(w - 1, max(0, int(round(x))))
            yi = min(h - 1, max(0, int(round(y))))
            row.append(bitmap[yi][xi])
        out.append(row)
    return out
