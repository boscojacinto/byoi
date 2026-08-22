#!/usr/bin/env python3
"""Before/after scan-margin report for the check-in QR.

Renders the check-in QR the way the slip used to (``legacy``) and the way it
does now (``shipped``), pushes both through the real print path, then degrades
each raster the way a phone in a dim cafe degrades it and counts how many
modules come back wrong.

What the module count means: a QR decoder that has locked onto the symbol reads
one bit per module and hands the bitstream to Reed-Solomon. If the wrong-module
count stays under the error-correction budget the symbol decodes; past it the
decode fails. So "modules wrong" vs "budget" is the scan margin.

What this does NOT model: finder-pattern acquisition, perspective, and the
scanner's own binarisation, all of which happen before module sampling. The
grid here is derived from the symbol's dark bounding box, which is a stand-in
for what a decoder recovers from the finder patterns. Both variants get the
identical harness, so the comparison is sound even though the absolute
thresholds are approximate.

    python scripts/qr-scan-report.py --out data/qr-report
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import qrcode
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from apps.api.slips import QR_QUIET_MODULES, render_qr  # noqa: E402
from peripage_a6.models import A6_304  # noqa: E402
from peripage_a6.raster import Dither, prepare_image  # noqa: E402

# A join URL of the shape the salon actually prints.
SAMPLE_JOIN = "https://10.201.175.216:8787/join?otp=31b65481"

# Reed-Solomon recovers roughly this share of codewords per level. Module-level
# damage maps onto codeword damage only loosely, so this is a budget, not a
# guarantee -- which is the point of leaving headroom.
EC_BUDGET = {"M": 0.15, "Q": 0.25}


def legacy_qr(data: str) -> tuple[Image.Image, dict]:
    """The pre-fix QR: 2-module quiet zone, EC M, bicubic rescale to 360px."""
    qr = qrcode.QRCode(border=2, box_size=6, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    grid = len(qr.get_matrix())
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    image = image.resize((360, 360))  # the defect: 360/33 = 10.909px per module
    return image, {
        "version": qr.version,
        "grid": grid,
        "modules": grid - 4,
        "px_per_module": 360 / grid,
        "quiet_modules": 2,
        "size_px": 360,
        "module_mm": round(360 / grid / A6_304.dpi * 25.4, 3),
        "error_correction": "M",
    }


def truth_matrix(data: str, ec: str) -> list[list[bool]]:
    """Ground-truth module matrix, quiet zone stripped."""
    level = {"M": qrcode.constants.ERROR_CORRECT_M, "Q": qrcode.constants.ERROR_CORRECT_Q}[ec]
    qr = qrcode.QRCode(border=0, box_size=1, error_correction=level)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


def to_head_raster(qr_img: Image.Image) -> Image.Image:
    """Run the QR through the same 1-bit conversion print_slip uses.

    The slip canvas is already 576px wide so prepare_image's resize is a no-op
    there; padding to row_width here reproduces that faithfully.
    """
    canvas = Image.new("RGB", (A6_304.row_width, qr_img.size[1] + 40), (255, 255, 255))
    canvas.paste(qr_img, ((A6_304.row_width - qr_img.size[0]) // 2, 20))
    prepared = prepare_image(canvas, dither=Dither.THRESHOLD)
    # prepare_image inverts for the print head (1 = burn); flip back so the
    # returned image is what lands on paper: black module = 0.
    return prepared.convert("L").point(lambda p: 0 if p else 255)


def degrade(paper: Image.Image, *, bleed: int, blur: float, light: float) -> Image.Image:
    """Model paper and camera between the print head and the decoder.

    bleed: thermal dots grow into white gaps; MinFilter spreads dark.
    blur:  phone camera at close focus in low light.
    light: dynamic range left after underexposure, 1.0 = studio, 0.35 = dim cafe.
    """
    out = paper.convert("L")
    if bleed:
        out = out.filter(ImageFilter.MinFilter(2 * bleed + 1))
    if blur:
        out = out.filter(ImageFilter.GaussianBlur(blur))
    if light < 1.0:
        floor = int(255 * (1.0 - light) * 0.5)
        span = 255 - 2 * floor
        out = out.point(lambda p: floor + int(p * span / 255))
    return out


def dark_bbox(gray: Image.Image) -> tuple[int, int, int, int]:
    """Symbol extent, from the finder patterns that pin all four edges."""
    lo, hi = gray.getextrema()
    mid = (lo + hi) // 2
    mask = gray.point(lambda p, t=mid: 255 if p < t else 0, mode="1")
    box = mask.getbbox()
    if box is None:
        raise ValueError("no dark pixels: symbol lost entirely")
    return box


def recover(gray: Image.Image, modules: int) -> list[list[bool]]:
    """Sample module centres off a grid interpolated across the symbol."""
    left, top, right, bottom = dark_bbox(gray)
    pitch_x = (right - left) / modules
    pitch_y = (bottom - top) / modules
    lo, hi = gray.getextrema()
    mid = (lo + hi) / 2
    px = gray.load()
    out = []
    for r in range(modules):
        row = []
        for c in range(modules):
            x = min(gray.size[0] - 1, int(left + (c + 0.5) * pitch_x))
            y = min(gray.size[1] - 1, int(top + (r + 0.5) * pitch_y))
            row.append(px[x, y] < mid)
        out.append(row)
    return out


def compare(truth: list[list[bool]], got: list[list[bool]]) -> int:
    return sum(t != g for trow, grow in zip(truth, got) for t, g in zip(trow, grow))


CONDITIONS = [
    ("studio            ", dict(bleed=0, blur=0.0, light=1.00)),
    ("bright counter    ", dict(bleed=1, blur=1.0, light=0.90)),
    ("normal cafe       ", dict(bleed=1, blur=2.0, light=0.65)),
    ("dim cafe          ", dict(bleed=2, blur=3.0, light=0.45)),
    ("dim + tired phone ", dict(bleed=2, blur=4.5, light=0.35)),
    ("candlelit corner  ", dict(bleed=3, blur=6.0, light=0.25)),
]


def run(name: str, qr_img: Image.Image, geo: dict, data: str, outdir: Path) -> list[dict]:
    truth = truth_matrix(data, geo["error_correction"])
    modules = geo["modules"]
    paper = to_head_raster(qr_img)
    paper.convert("L").save(outdir / f"{name}-paper.png")

    budget = int(modules * modules * EC_BUDGET[geo["error_correction"]])
    rows = []
    for label, kw in CONDITIONS:
        damaged = degrade(paper, **kw)
        damaged.save(outdir / f"{name}-{label.strip().replace(' ', '-')}.png")
        try:
            wrong = compare(truth, recover(damaged, modules))
        except ValueError:
            wrong = modules * modules
        rows.append(
            {
                "condition": label.strip(),
                "wrong": wrong,
                "total": modules * modules,
                "budget": budget,
                "ok": wrong <= budget,
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/qr-report", type=Path)
    ap.add_argument("--join", default=SAMPLE_JOIN)
    args = ap.parse_args()
    outdir = (ROOT / args.out) if not args.out.is_absolute() else args.out
    outdir.mkdir(parents=True, exist_ok=True)

    variants = [("before", *legacy_qr(args.join)), ("after", *render_qr(args.join))]

    lines = [f"# check-in QR scan margin\n", f"join URL: `{args.join}`\n"]
    lines.append("## geometry\n")
    lines.append("| | before | after |")
    lines.append("|---|---|---|")
    keys = [
        ("error_correction", "error correction"),
        ("version", "QR version"),
        ("modules", "modules"),
        ("quiet_modules", "quiet zone (modules)"),
        ("px_per_module", "px per module"),
        ("module_mm", "module size (mm)"),
        ("size_px", "block size (px)"),
    ]
    geos = {n: g for n, _, g in variants}
    for key, label in keys:
        b, a = geos["before"][key], geos["after"][key]
        fmt = lambda v: f"{v:.3f}" if isinstance(v, float) else str(v)
        lines.append(f"| {label} | {fmt(b)} | {fmt(a)} |")

    results = {}
    for name, img, geo in variants:
        results[name] = run(name, img, geo, args.join, outdir)

    lines.append("\n## modules misread (lower is better; must stay under budget)\n")
    lines.append("| condition | before | after |")
    lines.append("|---|---|---|")
    for i, (label, _) in enumerate(CONDITIONS):
        b, a = results["before"][i], results["after"][i]
        bs = f"{b['wrong']}/{b['budget']} {'PASS' if b['ok'] else 'FAIL'}"
        as_ = f"{a['wrong']}/{a['budget']} {'PASS' if a['ok'] else 'FAIL'}"
        lines.append(f"| {label.strip()} | {bs} | {as_} |")

    b_pass = sum(r["ok"] for r in results["before"])
    a_pass = sum(r["ok"] for r in results["after"])
    lines.append(f"\nconditions passed: before {b_pass}/{len(CONDITIONS)}, after {a_pass}/{len(CONDITIONS)}\n")

    report = "\n".join(lines)
    (outdir / "report.md").write_text(report)
    print(report)
    print(f"\nPNGs and report.md written to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
