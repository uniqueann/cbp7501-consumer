#!/usr/bin/env python3
"""OCR parser for CBP Form 7501 line-item detail screenshots (US-LCL-*).

Sibling of ``parse_b3_line_ocr.py`` (which handles the Canadian BSF864 band).
The 7501 line-item block has full-height vertical rules but only dashed lines
between items, so the grid strategy used for BSF864 does not transfer. Instead:

  1. detect the **vertical** rules only     -> the five column bands
  2. OCR the page, drop watermark boxes     -> light CJK stamps are not data
  3. re-OCR the description column at 3x    -> restores word spacing the
     whole-page pass loses ("ARTICLEOFCHINA" -> "ARTICLE OF CHINA")
  4. cluster per column by y                -> logical rows
  5. line numbers (001, 002, ...)           -> item block boundaries
  6. pair each rate cell with the nearest   -> (rate, amount) rows
     amount cell by y
  7. attach each description line to the    -> which text belongs to which code
     next code printed below it
  8. re-derive every amount from the        -> catches OCR slips
     printed value and rate

Output keeps ``parse_7501.py``'s line-item shape (line_no / line_type /
exclusion_note / tariff_subheadings[] / commodity{} / fees[]) so the same
downstream code can consume it. Header-only fields (entry number, dates,
carrier, consignee, importer) come back null: a line-item crop lacks them.

Usage:
    python parse_7501_line_ocr.py <image-or-image-pdf> [out.json]
        [--raw raw.json] [--source NAME] [--debug]

Requirements (same venv as the BSF864 OCR path):
    py -m venv .venv-ocr
    .venv-ocr\\Scripts\\python -m pip install rapidocr-onnxruntime pillow numpy
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

INK_THRESHOLD = 160       # grey level below which a pixel counts as ink
V_MIN_SPAN = 0.25         # a vertical rule covers >=25% of the height
LINE_GAP = 2              # pixels this close belong to the same rule
MIN_COL_W = 40            # two column edges are at least this far apart
INTERNAL_RULES = 4        # the band always has 5 columns -> 4 inner rules
UPSCALE_BELOW = 1000      # upscale genuinely small inputs
TARGET_WIDTH = 1400
WATERMARK_MIN_GREY = 200  # a box whose darkest pixel is this light has no ink
Y_TOLERANCE = 30          # max y distance when pairing a rate with its amount
BLOCK_SLACK = 12          # a block's first text line may precede its line no
SPACE_REPAD = 12          # right padding when re-reading a line for word spaces
STRIP_PAD = 15            # horizontal padding when re-reading a text line
DESC_SCALE = 3            # magnification for the description column re-OCR
PDF_DPI = 200

# CbpFormType handled by this parser: 15 = U.S. 7501 line-item screenshot (OCR).
# consumer.py reads this constant instead of hardcoding the number.
CBP_FORM_TYPE = 15

# Column roles, left to right. The 7501 line-item band prints them in this
# order; only the x positions move between batches.
COLUMNS = ("line_no", "description", "value", "rate", "amount")

HTS_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2,4})$")
FEE_RE = re.compile(r"^(499|501)\s*-?\s*(.+)$")
VALUE_RE = re.compile(r"^\$?\s*([\d,]+(?:\.\d{2})?)$")
CHGS_RE = re.compile(r"^C\s*\$?\s*([\d,]+(?:\.\d{2})?)$")

engine = RapidOCR()


# ── geometry ────────────────────────────────────────────────────────────────
def group_lines(indexes: list[int], gap: int = LINE_GAP) -> list[int]:
    """Collapse runs of adjacent pixel indices into one averaged coordinate."""
    groups: list[list[int]] = []
    for i in indexes:
        if groups and i - groups[-1][-1] <= gap:
            groups[-1].append(i)
        else:
            groups.append([i])
    return [int(round(sum(g) / len(g))) for g in groups]


def detect_columns(ink: np.ndarray) -> list[int]:
    """Return the column edges (x) of the line-item band.

    Only the vertical rules are usable: they run the full height of the crop,
    whereas the row separators are dashes and never form a solid line.

    The band always has five columns (line no / description / value / rate /
    amount), so exactly four inner rules are kept: the ones with the highest
    vertical coverage, in left-to-right order. Ranking by coverage rather than
    plain thresholding is what stops a thin stray mark (a digit stroke, a
    bracket) from being mistaken for a rule and shifting every column index.
    """
    height, width = ink.shape
    sums = ink.sum(0)
    candidates = [x for x in range(width) if sums[x] > V_MIN_SPAN * height]
    groups = group_lines(candidates)

    rules: list[tuple[int, int]] = []
    for centre in groups:
        span = range(max(centre - 2, 0), min(centre + 3, width))
        rules.append((centre, max(int(sums[x]) for x in span)))

    chosen: list[int] = []
    for centre, _coverage in sorted(rules, key=lambda r: -r[1]):
        if 0 < centre < width and all(abs(centre - c) >= MIN_COL_W for c in chosen):
            chosen.append(centre)
        if len(chosen) == INTERNAL_RULES:
            break
    chosen.sort()

    if len(chosen) != INTERNAL_RULES:
        # Fall back to even fifths so a damaged crop still yields usable columns.
        return [int(width * i / 5) for i in range(5)] + [width]
    return [0] + chosen + [width]


# ── OCR ─────────────────────────────────────────────────────────────────────
def load_page(path: str) -> Image.Image:
    """Open an image, or rasterise the first page of an image-only PDF."""
    if path.lower().endswith(".pdf"):
        try:
            import pymupdf as fitz
        except ImportError:
            try:
                import fitz
            except ImportError:
                raise SystemExit(
                    "PDF input needs PyMuPDF: pip install pymupdf "
                    "(or export the page as PNG/JPG first)"
                ) from None
        with fitz.open(path) as doc:
            pix = doc[0].get_pixmap(dpi=PDF_DPI)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")

    img = Image.open(path).convert("L")
    if img.width < UPSCALE_BELOW:
        factor = TARGET_WIDTH / img.width
        img = img.resize((int(img.width * factor), int(img.height * factor)),
                         Image.LANCZOS)
    return img


def is_watermark(text: str) -> bool:
    """True for the light CJK stamps overlaid on these screenshots."""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _boxes_from_result(result, grey: np.ndarray, scale: float = 1.0,
                       x_offset: int = 0) -> list[dict]:
    """Convert engine output to pixel-space boxes, dropping watermark tokens."""
    boxes = []
    for box, text, score in (result or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        text = unicodedata.normalize("NFKC", text).strip()
        if not text or is_watermark(text):
            continue
        x0 = x_offset + int(min(xs) / scale)
        x1 = x_offset + int(max(xs) / scale)
        y0 = int(min(ys) / scale)
        y1 = int(max(ys) / scale)
        patch = grey[max(y0, 0):y1, max(x0, 0):x1]
        if patch.size == 0 or int(patch.min()) > WATERMARK_MIN_GREY:
            continue                      # nothing actually printed in this box
        boxes.append({"x0": x0, "x1": x1, "y0": y0, "y1": y1,
                      "cy": (y0 + y1) / 2, "text": text, "score": float(score)})
    return boxes


def ocr_page(img: Image.Image) -> list[dict]:
    """One whole-page OCR pass."""
    result, _ = engine(np.array(img.convert("RGB")))
    return _boxes_from_result(result, np.array(img))


def refine_text_columns(img: Image.Image, edges: list[int],
                        boxes: list[dict]) -> list[dict]:
    """Re-OCR the line-number and description columns at higher magnification.

    Two things go wrong at the natural screenshot scale: the recogniser drops
    inter-word spaces ("PULLY/HOIST,NTSKIP/VEHIL,ELEC"), and the line number
    gets glued onto the description ("001 ARTICLE OF CHINA,..."), which hides
    the block boundary. Re-reading those two columns at 3x fixes both. The
    value/rate/amount columns keep their original coordinates, so pairing a
    rate with its amount is unaffected.
    """
    if len(edges) < 3:
        return boxes

    x_start, x_end = edges[0], edges[2]        # line-no + description
    if x_end - x_start < MIN_COL_W:
        return boxes

    crop = img.crop((x_start, 0, x_end, img.height))
    big = crop.resize((crop.width * DESC_SCALE, crop.height * DESC_SCALE),
                      Image.LANCZOS)
    result, _ = engine(np.array(big.convert("RGB")))
    refined = _boxes_from_result(result, np.array(img), DESC_SCALE, x_start)
    if not refined:
        return boxes

    # The detector drops inter-word spaces when the crop is much wider than the
    # text it contains, but keeps them when the crop hugs the line. So every text
    # box is re-read from a tight strip around its own glyphs. Cropping from the
    # box edge (not the column edge) also keeps the column rule, and the line
    # number in the previous column, out of the strip.
    for box in refined:
        if not re.search(r"[A-Za-z]{4,}", box["text"]):
            continue                      # codes and numbers need no repair
        strip = img.crop((max(box["x0"] - STRIP_PAD, x_start + 2),
                          max(box["y0"] - 3, 0),
                          min(box["x1"] + STRIP_PAD, x_end),
                          min(box["y1"] + 4, img.height)))
        strip = strip.resize((strip.width * DESC_SCALE, strip.height * DESC_SCALE),
                             Image.LANCZOS)
        again, _ = engine(np.array(strip.convert("RGB")))
        fixed = " ".join(t for _b, t, _s in (again or [])).strip()
        if fixed and fixed.count(" ") > box["text"].count(" "):
            box["text"] = fixed

    # Drop every original box whose centre falls in these two columns; merged
    # boxes are re-read from the crop so the line number comes back separately.
    kept = [b for b in boxes if not (x_start <= (b["x0"] + b["x1"]) / 2 < x_end)]
    return kept + refined


def column_of(box: dict, edges: list[int]) -> int:
    """Index of the column band containing the box centre."""
    centre = (box["x0"] + box["x1"]) / 2
    for index in range(len(edges) - 1):
        if edges[index] <= centre < edges[index + 1]:
            return index
    return len(edges) - 2


# ── value helpers ───────────────────────────────────────────────────────────
def money(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(text).replace(",", ""))
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        return float(Decimal(cleaned).quantize(Decimal("0.01"), ROUND_HALF_UP))
    except Exception:
        return None


def quantity(text: str) -> tuple[float | None, str | None]:
    """Split a '984 KG' / '5.00 NO' cell into (value, unit)."""
    match = re.match(r"^([\d,.]+)\s*([A-Z]{2,3})?\s*$", text.strip(), re.I)
    if not match:
        return None, None
    unit = (match.group(2) or "").upper()
    if not unit:
        return None, None
    return money(match.group(1)), unit


def normalise_rate(text: str) -> str | None:
    """Return 'FREE' or 'NN%' from a rate cell."""
    token = text.strip().upper().replace(" ", "")
    if token.startswith("FREE"):
        return "FREE"
    match = re.fullmatch(r"([\d.]+)%", token)
    return f"{match.group(1)}%" if match else None


# ── block parsing ───────────────────────────────────────────────────────────
def split_blocks(boxes: list[dict], edges: list[int]) -> list[dict]:
    """Group boxes into line-item blocks keyed by their printed line number."""
    starts: list[tuple[float, str]] = []
    for box in boxes:
        col = column_of(box, edges)
        if col == 0:
            digits = "".join(ch for ch in box["text"] if ch.isdigit())
            if len(digits) == 3:
                starts.append((box["y0"], digits))
        elif col == 1:
            # A merged "001 ARTICLE OF CHINA..." box spans the first two bands.
            head = re.match(r"^(\d{3})\s+\S", box["text"])
            if head and box["x0"] < edges[1] + 40:
                starts.append((box["y0"], head.group(1)))

    starts.sort()
    blocks = []
    for index, (top, line_no) in enumerate(starts):
        next_top = starts[index + 1][0] if index + 1 < len(starts) else float("inf")
        # A block's first description line often sits a couple of pixels above
        # its printed line number, so both boundaries are nudged up by the same
        # slack; otherwise that first line is attributed to the previous block.
        blocks.append({"line_no": line_no,
                       "top": top - BLOCK_SLACK,
                       "bottom": next_top - BLOCK_SLACK})
    return blocks


def collect(blocks: list[dict], boxes: list[dict], edges: list[int], role: str) -> None:
    """Attach the boxes of one column role to their block."""
    col = COLUMNS.index(role)
    for box in boxes:
        if column_of(box, edges) != col:
            continue
        for block in blocks:
            if block["top"] <= box["y0"] < block["bottom"]:
                block.setdefault(role, []).append(box)
                break


def pair_rate_amount(rates: list[dict], amounts: list[dict]) -> list[dict]:
    """Match every rate cell to its amount cell by nearest y centre."""
    pairs = []
    used: set[int] = set()
    for rate in sorted(rates, key=lambda b: b["cy"]):
        rate_text = normalise_rate(rate["text"])
        if rate_text is None:
            continue
        best, best_delta = None, Y_TOLERANCE + 1
        for index, amount in enumerate(amounts):
            if index in used or money(amount["text"]) is None:
                continue
            delta = abs(amount["cy"] - rate["cy"])
            if delta < best_delta:
                best, best_delta = index, delta
        if best is None:
            continue
        used.add(best)
        pairs.append({"cy": rate["cy"], "rate": rate_text,
                      "amount": money(amounts[best]["text"]),
                      "amount_text": amounts[best]["text"]})
    return pairs


def nearest_pair(pairs: list[dict], cy: float) -> dict | None:
    """The rate/amount row closest to a given y, or None beyond tolerance."""
    best, best_delta = None, Y_TOLERANCE + 1
    for pair in pairs:
        delta = abs(pair["cy"] - cy)
        if delta < best_delta:
            best, best_delta = pair, delta
    return best


def split_hts(code: str) -> tuple[str, str | None]:
    """Return (full code, 6-digit parent) for a dotted classification number."""
    digits = re.sub(r"\D", "", code)
    if len(digits) < 10:
        return code, None
    return code, f"{digits[:4]}.{digits[4:6]}"


def parse_block(block: dict) -> dict:
    """Turn one block's column boxes into a parse_7501-shaped line item.

    Layout rule on the 7501 line-item band: every free-text line sits directly
    ABOVE the HTS code it describes, while the description column also carries
    the quantity and fee lines. So codes, quantities and fees are classified
    first, then each remaining text line is attached to the next code below it.
    """
    desc_boxes = sorted(block.get("description", []), key=lambda b: (b["y0"], b["x0"]))
    pairs = pair_rate_amount(block.get("rate", []), block.get("amount", []))

    codes: list[tuple[float, str, dict]] = []     # (y0, code, subheading row)
    texts: list[tuple[float, str]] = []           # free-text candidates
    fees: list[dict] = []
    weights: list[tuple[float, str]] = []
    commodity: dict = {}

    # A magnified re-OCR sometimes splits "447.00 KG" into two boxes; glue a
    # bare unit token back onto the number on the same line before classifying.
    merged: list[dict] = []
    for box in desc_boxes:
        unit_only = re.fullmatch(r"(KG|NO|PCS|CTN|LB|M|M2|M3)",
                                 box["text"].strip(), re.I)
        if unit_only:
            host = next((m for m in merged
                         if abs(m["cy"] - box["cy"]) <= 8
                         and re.fullmatch(r"[\d,.]+", m["text"].strip())), None)
            if host is not None:
                host["text"] = f"{host['text']} {box['text'].strip()}"
                host["x1"] = max(host["x1"], box["x1"])
                continue
        merged.append(dict(box))
    desc_boxes = merged

    for box in desc_boxes:
        text = box["text"].strip()

        # Order matters: a fee row starts with "499"/"501" and a quantity row
        # starts with a number, so both look like a leading line number. They
        # must be classified before any line-number stripping.
        fee = FEE_RE.match(text)
        if fee:
            match = nearest_pair(pairs, box["cy"])
            fees.append({"code": fee.group(1),
                         "description": fee.group(2).strip(" -").strip(),
                         "rate": match["rate"] if match else None,
                         "amount": match["amount"] if match else None})
            continue

        if HTS_RE.match(text.replace(" ", "")):
            code = text.replace(" ", "")
            match = nearest_pair(pairs, box["cy"])
            row = {"htsus": code, "steel_weight_kg": None,
                   "rate": match["rate"] if match else None,
                   "amount": match["amount"] if match else None}
            codes.append((box["y0"], code, row))
            continue

        value, unit = quantity(text)
        if value is not None and unit:
            weights.append((value, unit))
            continue

        # A merged "001 ARTICLE OF CHINA..." box carries the item number in
        # front of the description; drop it now that the numeric rows are gone.
        head = re.match(r"^(\d{3})\s+(.+)$", text)
        if head and head.group(1) not in ("499", "501"):
            text = head.group(2).strip()
        texts.append((box["y0"], text))

    # Split codes into the 10-digit primary and the shorter 9903 subheadings.
    subheadings: list[dict] = []
    primary_code = None
    for _y0, code, row in codes:
        if len(re.sub(r"\D", "", code)) == 10:
            primary_code = code
            commodity["htsus"] = code
            commodity["htsus_rate"] = row["rate"]
            commodity["duty_amount"] = row["amount"]
        else:
            subheadings.append(row)

    # Each free-text line describes the next code printed below it.
    code_rows = sorted(codes, key=lambda c: c[0])
    exclusion_note = None
    for y0, text in sorted(texts):
        target = next((c for c in code_rows if c[0] > y0), None)
        if target is None:
            commodity.setdefault("description", text)
            continue
        if target[1] == primary_code:
            commodity["description"] = text
        else:
            target[2]["description"] = text
            if exclusion_note is None:
                exclusion_note = text

    # Quantities: with two KG cells the left one is gross weight and the right
    # one the net quantity; a NO cell is the net quantity instead.
    kgs = [w for w in weights if w[1] == "KG"]
    others = [w for w in weights if w[1] != "KG"]
    if len(kgs) >= 2:
        commodity.setdefault("gross_weight_kg", kgs[0][0])
        commodity.setdefault("net_quantity", kgs[1][0])
        commodity.setdefault("net_quantity_unit", "KG")
    elif kgs and others:
        commodity.setdefault("gross_weight_kg", kgs[0][0])
        commodity.setdefault("net_quantity", others[0][0])
        commodity.setdefault("net_quantity_unit", others[0][1])
    elif kgs:
        commodity.setdefault("gross_weight_kg", kgs[0][0])
        commodity.setdefault("net_quantity", kgs[0][0])
        commodity.setdefault("net_quantity_unit", "KG")
    elif others:
        commodity.setdefault("net_quantity", others[0][0])
        commodity.setdefault("net_quantity_unit", others[0][1])

    # Value column: the plain $ figure is the entered value, "C $<n>" is CHGS,
    # a lone "N"/"Y" is the relationship indicator.
    entered_value = None
    chgs = None
    relationship = None
    for box in sorted(block.get("value", []), key=lambda b: b["cy"]):
        text = box["text"].strip()
        token = text.replace(" ", "")
        if re.fullmatch(r"[NY]", token, re.I):
            relationship = token.upper()
            continue
        cm = CHGS_RE.match(token)
        if cm:
            chgs = money(cm.group(1))
            continue
        plain = VALUE_RE.match(token.lstrip("$"))
        if plain:
            entered_value = money(plain.group(1))

    htsus, htsus_6 = split_hts(commodity.get("htsus") or "")
    item = {
        "line_no": block["line_no"],
        "line_type": "STANDARD",
        "manifest_note": None,
        "exclusion_note": exclusion_note,
        "relationship": relationship,
        "tariff_subheadings": subheadings,
        "commodity": {
            "description": commodity.get("description"),
            "htsus": htsus or None,
            "htsus_6": htsus_6,
            "gross_weight_kg": commodity.get("gross_weight_kg"),
            "net_quantity": commodity.get("net_quantity"),
            "net_quantity_unit": commodity.get("net_quantity_unit"),
            "entered_value": entered_value,
            "htsus_rate": commodity.get("htsus_rate"),
            "duty_amount": commodity.get("duty_amount"),
            "entered_value_chgs": chgs,
        },
        "fees": fees,
    }
    # A subheading printed FREE still counts as a duty row of 0.00.
    for sub in item["tariff_subheadings"]:
        if sub["rate"] == "FREE" and sub["amount"] is None:
            sub["amount"] = 0.0
    return item


def parse(image_path: str, debug: bool = False) -> dict:
    img = load_page(image_path)
    ink = np.array(img) < INK_THRESHOLD
    edges = detect_columns(ink)
    boxes = ocr_page(img)
    boxes = refine_text_columns(img, edges, boxes)
    blocks = split_blocks(boxes, edges)
    for role in COLUMNS[1:]:
        collect(blocks, boxes, edges, role)

    if debug:
        print(f"image {img.width}x{img.height}")
        print("column edges:", edges)
        print("line-item starts:", [(b["line_no"], int(b["top"])) for b in blocks])

    return {
        "document_image": f"{img.width}x{img.height}",
        "columns": edges,
        "line_items": [parse_block(b) for b in blocks],
    }


def reconcile(items: list[dict]) -> list[dict]:
    """Re-derive every printed amount from its value and rate."""
    checks = []
    for item in items:
        value = item["commodity"]["entered_value"]
        rows = [(s["htsus"], s["rate"], s["amount"]) for s in item["tariff_subheadings"]]
        rows.append((item["commodity"]["htsus"], item["commodity"]["htsus_rate"],
                     item["commodity"]["duty_amount"]))
        rows += [(f["code"], f["rate"], f["amount"]) for f in item["fees"]]
        for code, rate, printed in rows:
            if value is None or rate is None or printed is None:
                continue
            if rate == "FREE":
                expected = 0.0
            else:
                try:
                    expected = (Decimal(str(value)) * Decimal(rate.rstrip("%"))
                                / Decimal(100))
                except Exception:
                    continue
                expected = float(expected.quantize(Decimal("0.01"), ROUND_HALF_UP))
            checks.append({"line": item["line_no"], "base": value, "target": code,
                           "rate": rate, "expected": expected, "printed": printed,
                           "ok": abs(expected - printed) <= 0.01})
    return checks


def summarise(items: list[dict]) -> dict:
    duty = 0.0
    for item in items:
        duty += sum(s["amount"] or 0.0 for s in item["tariff_subheadings"])
        duty += item["commodity"]["duty_amount"] or 0.0
    flat_fees = [(f["code"], f["amount"] or 0.0) for item in items for f in item["fees"]]
    total_fees = sum(amount for _, amount in flat_fees)
    return {
        "currency": "USD",
        "entered_value_usd": round(sum(i["commodity"]["entered_value"] or 0.0
                                       for i in items), 2),
        "duty_usd": round(duty, 2),
        "mpf_usd": round(sum(a for c, a in flat_fees if c == "499"), 2),
        "hmf_usd": round(sum(a for c, a in flat_fees if c == "501"), 2),
        "other_fees_usd": round(total_fees, 2),
        "grand_total_usd": round(duty + total_fees, 2),
    }


def build_result(parsed: dict, source: str | None = None) -> dict:
    items = parsed["line_items"]
    checks = reconcile(items)
    warnings = [
        {"code": "PARTIAL_DOCUMENT",
         "detail": "Only the 7501 line-item band is present; the header block "
                   "(entry number, dates, carrier, consignee, importer) and the "
                   "declaration totals are not in the image, so header is null."},
        {"code": "IMAGE_ONLY_SOURCE",
         "detail": "Raster screenshot with no PDF text layer: values come from "
                   "local OCR, not from parse_7501.py.",
         "parser": "parse_7501_line_ocr.py", "source": source},
        {"code": "DERIVED_VALUE",
         "field": "totals",
         "method": "sum(line_items[].tariff_subheadings[].amount + "
                   "commodity.duty_amount + fees[])",
         "detail": "No declaration-level totals exist in this crop; the totals "
                   "block is summed from the line items."},
    ]
    for check in checks:
        if not check["ok"]:
            warnings.append({
                "code": "ROUNDING_DIFF",
                "field": f"line {check['line']} {check['target']}",
                "detail": f"{check['rate']} of {check['base']} = "
                          f"{check['expected']}, but the form prints "
                          f"{check['printed']}. The printed value is kept.",
            })
    return {
        "schema_version": "2.0-ocr",
        "document": {
            "form": "CBP Form 7501 - line item detail",
            "pages": 1,
            "printed_at": None,
            "notes": None,
            "source": source,
            "source_type": "raster screenshot (no PDF text layer)",
            "parser": "parse_7501_line_ocr.py",
        },
        "header": None,
        "invoices": [],
        "totals": summarise(items),
        "line_items": items,
        "line_item_count": len(items),
        "arithmetic_checks": checks,
        "warnings": warnings,
    }


def parse_result(image_path: str, source: str | None = None) -> dict:
    """One-call entry point for callers that just want the final result.

    ``parse()`` returns the intermediate column/line-item structure; this wraps
    it into the parse_7501-shaped result. consumer.py uses this so it does not
    have to know about the two-step split.
    """
    return build_result(parse(image_path), source=source or Path(image_path).name)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    debug = "--debug" in argv
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
    parsed = parse(image_path, debug=debug)
    if raw_out:
        with open(raw_out, "w", encoding="utf-8") as fh:
            json.dump(parsed, fh, ensure_ascii=False, indent=2)
        print(f"raw -> {raw_out}")

    result = build_result(parsed, source=source or Path(image_path).name)

    print(f"line items: {result['line_item_count']}")
    for item in result["line_items"]:
        com = item["commodity"]
        subs = "/".join(f"{s['rate']}:{s['amount']}" for s in item["tariff_subheadings"])
        fees = "/".join(f"{f['code']}:{f['amount']}" for f in item["fees"])
        print(f"  {item['line_no']}  {com['htsus']}  value={com['entered_value']}"
              f"  chgs={com['entered_value_chgs']}  [{subs}]"
              f"  base={com['htsus_rate']}:{com['duty_amount']}  [{fees}]")
        print(f"      desc={com['description']!r}")

    bad = [c for c in result["arithmetic_checks"] if not c["ok"]]
    print(f"\narithmetic checks: {len(result['arithmetic_checks'])}  failed: {len(bad)}")
    for check in bad:
        print(f"  line {check['line']} {check['target']}: {check['rate']} of "
              f"{check['base']} = {check['expected']}, printed {check['printed']}")
    print(f"totals: {result['totals']}")

    if len(args) > 1:
        with open(args[1], "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"\nwrote {args[1]}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
