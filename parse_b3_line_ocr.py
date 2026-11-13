#!/usr/bin/env python3
"""OCR fallback parser for RASTER (scan/screenshot) CBSA BSF864 line-item bands.

This is the raster sibling of ``parse_cad.py``. ``parse_cad.py`` reads the PDF
text layer with pdfplumber and needs no OCR; use it whenever the source is a
text PDF (it is exact and ~50x faster). Reach for this script only when the
input is an image or an image-only PDF -- i.e. when there is no text layer.

Pipeline -- no LLM at any step:
  1. binarise, then detect the horizontal rules         -> band structure
  2. detect the vertical rules inside each band         -> field columns
  3. run one local OCR pass (RapidOCR/ONNX, no network) -> text boxes
  4. assign each text box to a cell by geometry         -> header vs value band
  5. read the field number from the header cell text    -> self-describing,
     so the same code works for any BSF864 revision
  6. re-OCR tiny value cells at high magnification      -> single digits that a
     whole-page pass drops (empirically: field 70 "2")
  7. reconcile the printed numbers against the form's own arithmetic

Requirements (a separate venv is easiest; onnxruntime ships cp314 wheels):
    py -m venv .venv-ocr
    .venv-ocr\\Scripts\\python -m pip install rapidocr-onnxruntime pillow numpy

The output is the same schema_version 2.0 dict ``parse_cad.parse()`` returns
(see ``parse_b3_line_schema``), so a screenshot can be fed to the same consumer.
Header fields 1-22, the invoice block 36-43 and declaration totals 113-130 are
absent from a line-item crop: those come back null with the same warning codes
``parse_cad`` uses.

Usage:
    python parse_b3_line_ocr.py <image-or-image-pdf> [out.json] [--raw raw.json]
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from rapidocr_onnxruntime import RapidOCR

INK_THRESHOLD = 128
H_MIN_SPAN = 0.5      # a horizontal rule covers >=50% of the image width
V_MIN_SPAN = 0.6      # a vertical rule covers >=60% of the band height
LINE_GAP = 2          # pixels this close belong to the same rule
MIN_CELL_W = 8        # narrower "cells" are rule slivers, not fields
TINY_CELL_INK = 40    # cells with fewer ink pixels are candidate lone digits
MAGNIFY = 12
TARGET_WIDTH = 1200   # upscale inputs narrower than this; small glyphs defeat OCR
UPSCALE_BELOW = 1000  # leave anything at or above this resolution untouched
SKEW_RANGE = 2.0      # max rotation corrected, in degrees
SKEW_STEP = 0.25
PDF_DPI = 200         # rasterisation resolution for image-only PDF pages

engine = RapidOCR()


def group_lines(indexes: list[int], gap: int = LINE_GAP) -> list[int]:
    """Collapse runs of adjacent pixel indices into one averaged coordinate."""
    groups: list[list[int]] = []
    for i in indexes:
        if groups and i - groups[-1][-1] <= gap:
            groups[-1].append(i)
        else:
            groups.append([i])
    return [int(round(sum(g) / len(g))) for g in groups]


def upscale(img: Image.Image) -> Image.Image:
    """Bring small screenshots up to a size the OCR models were trained on.

    Upscaling a large image as well measurably hurts (the detector starts
    merging neighbouring cells), so only genuinely small inputs are scaled.
    """
    if img.width >= UPSCALE_BELOW:
        return img
    factor = TARGET_WIDTH / img.width
    return img.resize((int(img.width * factor), int(img.height * factor)), Image.LANCZOS)


def deskew(img: Image.Image) -> Image.Image:
    """Straighten a slightly rotated scan.

    A grid of rules produces an extremely peaky horizontal ink profile only when
    it is axis-aligned, so the angle maximising the sum of squared row sums is
    the skew. This matters because the band detector needs whole-pixel rules:
    a 0.5 degree tilt already breaks it.
    """
    ink = (np.array(img) < INK_THRESHOLD).astype(np.float32)
    best_score, best_angle = -1.0, 0.0
    for angle in np.arange(-SKEW_RANGE, SKEW_RANGE + SKEW_STEP / 2, SKEW_STEP):
        rotated = ink
        if angle:
            raw = Image.fromarray((ink * 255).astype(np.uint8))
            rotated = np.array(raw.rotate(angle, resample=Image.BILINEAR,
                                          fillcolor=0, expand=True)) > 127
        profile = rotated.sum(1).astype(np.float64)
        score = float((profile ** 2).sum())
        if score > best_score:
            best_score, best_angle = score, float(angle)
    if abs(best_angle) < SKEW_STEP / 2:
        return img
    return img.rotate(best_angle, resample=Image.BICUBIC,
                      fillcolor=255, expand=True)


def detect_bands(ink: np.ndarray) -> tuple[list[int], list[list[int]]]:
    """Return (horizontal rule y's, [vertical rule x's per band])."""
    height, width = ink.shape
    rows = group_lines([i for i in range(height) if ink[i].sum() > H_MIN_SPAN * width])
    bands: list[list[int]] = []
    for y0, y1 in zip(rows, rows[1:]):
        strip = ink[y0 + 2:y1 - 1]
        if strip.size == 0:
            bands.append([])
            continue
        sums = strip.sum(0)
        bands.append(group_lines(
            [j for j in range(width) if sums[j] > V_MIN_SPAN * strip.shape[0]]
        ))
    return rows, bands


def ocr_page(img: Image.Image) -> list[dict]:
    """One OCR pass. Returns boxes in image pixels with text and confidence."""
    result, _ = engine(np.array(img))
    boxes = []
    for box, text, score in (result or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        boxes.append({"x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys),
                      "text": text, "score": float(score)})
    return boxes


def text_in(boxes: list[dict], y0: float, y1: float, x0: float, x1: float) -> str:
    """Concatenate the OCR boxes whose centre falls inside the given cell."""
    hits = [b for b in boxes
            if y0 <= (b["y0"] + b["y1"]) / 2 <= y1
            and x0 <= (b["x0"] + b["x1"]) / 2 <= x1]
    hits.sort(key=lambda b: (round(b["y0"] / 6), b["x0"]))
    return re.sub(r"\s+", " ", " ".join(b["text"] for b in hits)).strip()


def magnify_ocr(img: Image.Image, x0: int, x1: int, y0: int, y1: int) -> str | None:
    """Recognise a small cell that the whole-page pass missed.

    A lone character has too little context for the detector, so crop it tight,
    pad it, and call the recogniser directly.
    """
    cell = np.array(img.crop((x0 + 2, y0 + 2, x1 - 2, y1 - 2)))
    ink = cell < 160
    if not ink.any():
        return None
    # Drop rule remnants: a column/row that is almost entirely ink is a border
    # rather than a glyph, and it would otherwise dominate the ink profile and
    # stop the tiny-cell heuristic from firing.
    ink[:, ink.sum(0) > 0.7 * ink.shape[0]] = False
    ink[ink.sum(1) > 0.7 * ink.shape[1], :] = False
    if ink.sum() < 5 or ink.sum() > TINY_CELL_INK:
        return None
    ys, xs = np.where(ink)
    glyph = Image.fromarray(cell).crop(
        (int(xs.min()) - 1, int(ys.min()) - 1, int(xs.max()) + 2, int(ys.max()) + 2)
    )
    big = ImageOps.expand(glyph, 8, 255)
    big = big.resize((big.width * MAGNIFY, big.height * MAGNIFY), Image.LANCZOS)
    text, score = engine.text_recognizer([np.array(big.convert("RGB"))])[0][0]
    if not text or float(score) < 0.2:
        return None
    # the recogniser emits fullwidth digits for tiny glyphs; normalise them
    return unicodedata.normalize("NFKC", text).strip() or None


def to_float(text: str | None) -> float | None:
    """Numeric value of a printed cell, tolerating OCR noise in separators."""
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text.replace(",", ""))
    try:
        if cleaned.count(".") <= 1:
            return float(cleaned)
        head, *rest = cleaned.split(".")
        return float(f"{head}.{''.join(rest)}")
    except ValueError:
        return None


def load_page(path: str) -> Image.Image:
    """Open the first page of an image or a PDF as grayscale.

    An image-only PDF (a scan, or a "print to PDF" of a screenshot) has no text
    layer, so it is rasterised through PyMuPDF at 200 dpi. A text PDF should go
    to ``parse_cad.py`` instead: this function deliberately ignores the text.
    """
    if path.lower().endswith(".pdf"):
        try:
            import fitz  # PyMuPDF; only needed for PDF inputs
        except ImportError:
            raise SystemExit(
                "PDF input needs PyMuPDF for rasterisation: "
                "pip install pymupdf  (or export the page as PNG/JPG first)"
            ) from None
        with fitz.open(path) as doc:
            pix = doc[0].get_pixmap(dpi=PDF_DPI)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    return Image.open(path).convert("L")


def parse(image_path: str) -> dict:
    img = upscale(deskew(load_page(image_path)))
    ink = np.array(img) < INK_THRESHOLD
    rows, bands = detect_bands(ink)
    boxes = ocr_page(img)

    fields: dict[int, dict] = {}
    # Bands alternate header/value, so only every second band holds labels. The
    # final header band still has a value band below it, hence len(bands) - 1.
    for band in range(0, len(bands) - 1, 2):
        header_edges = bands[band]
        if len(header_edges) < 3:
            continue
        y_head = (rows[band] + 1, rows[band + 1] - 1)
        y_val = (rows[band + 1] + 1, rows[band + 2] - 1)
        for x0, x1 in zip(header_edges, header_edges[1:]):
            if x1 - x0 < MIN_CELL_W:
                continue
            label = text_in(boxes, *y_head, x0, x1)
            match = re.match(r"^(\d{1,3})\b\s*(.*)$", label)
            if not match:
                continue
            number = int(match.group(1))
            # the value box is inset inside the header cell: use the value-band
            # rules that fall within it, else fall back to the header edges
            inner = [e for e in bands[band + 1] if x0 <= e <= x1]
            vx0, vx1 = (inner[0], inner[-1]) if len(inner) >= 2 else (x0, x1)
            value = text_in(boxes, *y_val, vx0, vx1)
            if not value:
                value = magnify_ocr(img, vx0, vx1, int(y_val[0]), int(y_val[1]))
            fields[number] = {
                "label": match.group(2).strip(),
                "value": value or None,
            }
    return fields


def reconcile(fields: dict) -> list[tuple[str, float, float, bool]]:
    """Re-check the printed numbers against BSF864's own formulas, so a misread
    digit surfaces as a failed check instead of silent bad data."""
    def num(n: int) -> float | None:
        return to_float(fields.get(n, {}).get("value"))

    conv, rate, vfd = num(75), num(77), num(78)
    duty, gst, total = num(82), num(90), num(100)
    checks = []
    if None not in (conv, rate, vfd):
        checks.append(("78 = 75 x 77", conv * rate, vfd))
    if None not in (vfd, duty, gst):
        checks.append(("90 = 5% x (78 + 82)", 0.05 * (vfd + duty), gst))
    if None not in (duty, gst, total):
        checks.append(("100 = 82 + 90", duty + gst, total))
    return [(name, round(expect, 2), printed,
             abs(round(expect, 2) - printed) <= 0.02)
            for name, expect, printed in checks]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    # --raw keeps the untouched field dump for auditing; the positional output is
    # the parse_cad-shaped result. --source overrides the name recorded in the
    # IMAGE_ONLY_SOURCE warning (the attachment filename is a content hash).
    args = [a for a in argv[1:] if not a.startswith("--")]
    raw_out = source = None
    for flag in ("--raw", "--source"):
        if flag in argv:
            value = argv[argv.index(flag) + 1]
            args = [a for a in args if a != value]
            if flag == "--raw":
                raw_out = value
            else:
                source = value

    image_path = args[0]
    fields = parse(image_path)

    if raw_out:
        with open(raw_out, "w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in fields.items()}, fh,
                      ensure_ascii=False, indent=2)
        print(f"raw fields -> {raw_out}")

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from parse_b3_line_schema import build_result

    result = build_result(fields, source=source or Path(image_path).name)

    print("\narithmetic reconciliation:")
    for name, expect, printed, ok in reconcile(fields):
        print(f"  {name:22s} expect {expect:>9.2f}  printed {printed:>9.2f}  "
              f"{'OK' if ok else 'MISMATCH'}")

    if len(args) > 1:
        with open(args[1], "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"\nwrote {args[1]}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
