#!/usr/bin/env python3
"""
CBP Form 7501 Entry Summary Parser
Extracts structured JSON from CBP 7501 PDF files.

Usage:
    python parse_7501.py <input.pdf> [output.json]

If output path is omitted, prints JSON to stdout.
"""

import sys
import re
import json
from decimal import Decimal, InvalidOperation

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber not installed. Run: pip install pdfplumber --break-system-packages", file=sys.stderr)
    sys.exit(1)

# ── Form type ────────────────────────────────────────────────────────────────
# CbpFormType handled by this parser. consumer.py reports it in the output
# envelope next to ID / FileNumber, and uses it as the default when an inbound
# message omits the field.
CBP_FORM_TYPE = 10

# ── Commodity description lookup (HTSUS → human-readable label) ──────────────
# Extend this dict as you encounter new HTS codes across batches.
COMMODITY_DESCRIPTIONS: dict[str, str] = {
    "9506.91.0030": "GYM/PLAYGRND EXERC EQUIP;OTHER",
    "8303.00.0000": "ARMORED/REINFORCED SAFES, ETC.",
    "9506.99.6080": "OTHR GYM/SPORT EQUIP;PARTS&ACC",
    "8414.51.9090": "FANS N/PERMANENT, OUTPUT<=125W",
    "8210.00.0000": "HAND-OP MECH APPL,10KG OR LESS",
    "8516.60.6000": "COOK PLATES, GRILLERS,ETC, OTH",
    "8211.91.5030": "STEAK KNIVES W/HNDL OF RUB/PLA",
    "8419.81.9040": "OVENS ETC USED IN RESTAURANTS",
    "8509.80.5095": "ELEC DOM APPLIANCES, OTHER",
    "9401.49.0000": "CONVERTBLE SEATS N/GARDN/CAMPG",
    "9403.20.0048": "HSEHLD FURN,FOLD MAT,STATIONRY",
    "8302.41.6080": "MOUNTING ETC, OF IRON ETC,NSPF",
}

# ── Patterns ──────────────────────────────────────────────────────────────────
SKIP_LINES = {
    "N",
    "ENTRY SUMMARY CONTINUATION SHEET",
    "1.Filer Code/Entry Number",
}
SKIP_PREFIXES = ("32.", "27.", "Line", "29.", "A.HTSUS", "B.AD/CVD", "C $")

# 兼容两种格式：
#   新版：001 PRD ANY CTRY,EXC ...  /  007 IEEPA-RECIPROCAL EXCLUSION 232
#   旧版：001 HUMIDIFIERS,EVAPORATIVE  （无前缀，直接是商品描述）
# 匹配 001-999，后接大写字母开头的描述（排除 499/501 等 fee 行）
LINE_START_RE = re.compile(r"^(0\d{2}|[1-9]\d{2})\s+([A-Z].+)")
# A manifest row reads "887 CTN" / "65 CTN" / "365 CTNS" / "5133 KG". The unit may
# be plural, so an optional trailing "S" is accepted. "NO" (number of pieces, the
# HTSUS net unit) is deliberately NOT in this list: a "10 NO" line sits INSIDE a
# block as the net quantity, and matching it here would make the block parser skip
# that quantity -- which it did before this list was made explicit.
MANIFEST_UNITS = ("CTN", "CARTON", "PCS", "PKG", "PLT", "KG", "LB")
MANIFEST_RE = re.compile(
    r"^([\d,]+)\s+(" + "|".join(MANIFEST_UNITS) + r")S?$", re.I)
# 格式A（旧版）：HTSUS  grossKG  netQty  unit  $value  rate  $duty
# net_quantity 可能含逗号，如 3,465.00
COMMODITY_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{4})"
    r"\s+([\d,]+)\s+KG"
    r"\s+([\d,.]+)\s+(\w+)"
    r"\s+\$([\d,]+)"
    r"\s+(.+?)"
    r"\s+\$([\d.]+)$"
)
# 格式B（新版无gross weight）：HTSUS  netQty  unit  $value  rate  $duty
# net_quantity 可能含逗号，如 3,465.00
COMMODITY_RE_NOGROSS = re.compile(
    r"^(\d{4}\.\d{2}\.\d{4})"
    r"\s+([\d,.]+)\s+(\w+)"
    r"\s+\$([\d,]+)"
    r"\s+(.+?)"
    r"\s+\$([\d.]+)$"
)
# tariff 行：HTSUS  [grossKG]  rate  $amount
TARIFF_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{2,4})"
    r"\s+([\d,.]+\s*KG\s+)?"
    r"(FREE|\d+(?:\.\d+)?%)"
    r"\s+\$([\d.]+)"
)
FEE_RE  = re.compile(r"^(499|501)\s+-\s+(.+?)\s+([\d.]+%)\s+\$([\d.]+)")
# CHGS 行：C $80  或  C $80 999999999（末尾可能跟 Visa Number）
# CHGS 行："C $801" 或 "669 C $801"（Visa Number 紧跟 C $value）
CHGS_RE = re.compile(r"^(?:\d+\s+)?C\s+\$([\d,]+)")


# ── v2 Patterns (new CBP 7501 02/26 format) ──────────────────────────────
# ── Patterns (v2) ─────────────────────────────────────────────────────────────

ITEM_START_RE = re.compile(r"^(\d{3})\s+([A-Z].*)$")
_FEE_CODES = {499, 501}

# Commodity + tariff patterns — two layout variants
#   B-1:  9403.60.8093  150 NO  2,634  Free  0.00
#   C-1:  9403.60.8093 23,293 KG 379.00 NO $5,959 FREE $0.00
COMMODITY_V2_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{4})"
    r"\s+([\d,.]+)\s+([A-Z]{2,3})"  # net_quantity (allow comma+dot e.g. 2,016.00 / 290.00)
    r"\s+([\d,]+)"
    r"\s+(Free|FREE|[\d.]+%)\w*"     # rate (skip OCR noise after %, e.g. "25%n")
    r"\s+([\d,.]+)$"                 # duty_amount (allow comma)
)
# MRSU variant: net_quantity 为占位符 X，无 unit（unit 在下一行）
#   "9403.99.9061 X 2 Free 0.00"  -> htsus, X, entered_value, rate, duty
# MRSU: net_quantity is placeholder "X", no unit on same line (unit on next line)
#   "9403.99.9061 X 2 Free 0.00"
COMMODITY_V2_X_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{4})"
    r"\s+X"                          # net_quantity placeholder
    r"\s+([\d,]+)"                   # entered value
    r"\s+(Free|FREE|[\d.]+%)\w*"     # rate
    r"\s+([\d,.]+)$"                 # duty_amount
)
COMMODITY_V2_C1_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{4})"
    r"\s+([\d,]+)\s+KG"          # gross_weight_kg
    r"\s+([\d,.]+)\s+([A-Z]{2,3})" # net_quantity + unit (allow comma e.g. 2,016.00)
    r"\s+\$?([\d,]+)"             # entered value
    r"\s+((?:Free|FREE)\w*|[\d.]+%)"
    r"\s+\$?([\d,.]+)$"           # duty_amount (allow comma)
)

# Tariff subheading (8/9-digit): three formats
#   KG:    9903.76.04  6765 KG  0  Freen  0.00
#   No KG: 9903.03.01  X  0  10%  263.40
#   C-1:   9903.03.01 10% $595.90   (rate% $amount, no middle columns)
TARIFF_KG_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{2,3})"
    r"\s+([\d,]+)\s+KG"         # gross weight (KG)
    r"\s+[\d,]+"                 # entered value (ignored)
    r"\s+((?:Free|FREE)\w*|[\d.]+%)\w*"  # rate (skip OCR noise after %, e.g. "25%n")
    r"\s+([\d,.]+)$"             # amount (allow comma)
)
TARIFF_NOKG_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{2,3})"
    r"\s+([X\d,.]+)"             # qty or X (allow decimal e.g. 0.00)
    r"\s+[\d,]+"                 # entered value (ignored)
    r"\s+((?:Free|FREE)\w*|[\d.]+%)\w*"  # rate (skip OCR noise after %, e.g. "25%n")
    r"\s+([\d,.]+)$"             # amount (allow comma)
)
TARIFF_V2_C1_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{2,3})"
    r"\s+((?:Free|FREE)\w*|[\d.]+%)"   # rate
    r"\s+\$?([\d,.]+)$"                 # amount (allow comma, optional $ prefix)
)

# CHGS / Visa line: "C $1,500"
CHGS_RE = re.compile(r"^C\s+\$([\d,]+)")

# D-1 tariff subheading (8/9-digit): "9401.71.00 48NO Free 0.00"
TARIFF_D1_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{2,3})"
    r"\s+([\d,]+)\s+([A-Z]{2,3})"
    r"\s+((?:Free|FREE)\w*|[\d.]+%)"
    r"\s+([\d.]+)$"
)

# Fee line
#   B-1: 501 HARBOR MAINTENANCE FEE (HMF)  0.125%  3.29
#   C-1: 501 - Harbor Maintenance Fee 0.1250% $20.64
FEE_V2_RE = re.compile(r"^(499|501)\s+(.*?)\s+([\d.]+%)\s+\$?([\d.]+)$")

# ── D-1 Patterns (alternate format with joined qty+unit, different tariff layout) ──
# Tariff 9903.88.03 style: HTSUS gross_weight manifest_qty entered_value rate amount
#   9903.88.03 1035 0X 576 25% 144.00
TARIFF_D1_88_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{2,4})"
    r"\s+([\d,]+)"                # gross_weight
    r"\s+(\d+X|[\d,]+)"           # manifest quantity (e.g., "0X")
    r"\s+([\d,]+)"                # entered_value
    r"\s+((?:Free|FREE)\w*|[\d.]+%)"
    r"\s+\$?([\d.]+)$"
)

# Tariff 9903.03.01 / 9903.82.01 style: HTSUS qty rate amount (no gross weight)
#   9903.03.01 0X 10% 57.60
#   9903.82.01 0X Calculated 0.00
TARIFF_D1_0301_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{2,4})"
    r"\s+(\d+X|[\d,]+)"           # manifest quantity
    r"\s+((?:Free|FREE)\w*|Calculated|[\d.]+%)"
    r"\s+\$?([\d.]+)$"
)

# D-1 fee: FORMAT_MERCHANDISE_PROCESSING_FEE(499) 0.3464% 2.00
FEE_D1_RE = re.compile(
    r"^([A-Z_]+)\((\d{3})\)\s+([\d.]+%)\s+\$?([\d.]+)$"
)
FEE_D1_NAME_MAP: dict[str, str] = {
    "FORMAL_MERCHANDISE_PROCESSING_FEE": "Merchandise Processing Fee",
    "HARBOR_MAINTENANCE_FEE":            "Harbor Maintenance Fee (HMF)",
}

# D-1 commodity: HTSUS qty+unit rate amount (qty and unit joined without space)
#   9401.49.0000 48NO Free 0.00
#   8302.41.6080 5456KG 3.9% 46.92
COMMODITY_D1_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{4})"
    r"\s+([\d,]+)(NO|KG)"         # net qty + unit (no space)
    r"\s+((?:Free|FREE)\w*|[\d.]+%)"
    r"\s+([\d.]+)$"
)

_SKIP_LINES = {"N", "ENTRY SUMMARY CONTINUATION SHEET", "1.Filer Code/Entry Number"}
_SKIP_PREFIXES = (
    "Invoice Number", "Invoice Value", "Total Entered Value",
    "Other Fee Summary", "CBP Form 7501",
)

# Old-format marker: PRD ANY / IEEPA
_OLD_FORMAT_RE = re.compile(r"^\d{3}\s+(PRD ANY|IEEPA)", re.MULTILINE)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _invoice_currency(text: str) -> str:
    match = re.search(r"\bInvoice Value[ \t]+([A-Z]{3})[ \t]+[\d,]+(?:\.\d+)?\b", text, re.I)
    if not match:
        match = re.search(
            r"(?m)^Totals for Invoice[^\n]*\n[^\n]*?[ \t]+[\d,]+(?:\.\d+)?[ \t]+([A-Z]{3})\b",
            text,
            re.I,
        )
    return match.group(1).upper() if match else "USD"


def _line_conversion_fields(text: str, line_item_count: int) -> dict:
    fields = {
        "value_for_currency_conversion": None,
        "conversion_currency": "",
        "exchange_rate": None,
    }
    summaries = list(re.finditer(
        r"(?mi)^Totals[ \t]+for[ \t]+Invoice[^\n]*\n"
        r"[ \t]*\S+[ \t]+([\d,]+(?:\.\d+)?)[ \t]+([A-Z]{3})"
        r"(?:[ \t]+[+-]?[\d,]+(?:\.\d+)?)?"
        r"[ \t]+(\d+(?:\.\d+)?)[ \t]+[\d,]+(?:\.\d+)?[ \t]+[A-Z]{3}[ \t]*$",
        text,
    ))
    if not summaries:
        summaries = list(re.finditer(
            r"\bInvoice Value[ \t]+([A-Z]{3})[ \t]+([\d,]+(?:\.\d+)?)\b",
            text,
            re.I,
        ))
        if len(summaries) == 1:
            fields["conversion_currency"] = summaries[0].group(1).upper()
            if line_item_count == 1:
                fields["value_for_currency_conversion"] = _money(summaries[0].group(2))
    elif len(summaries) == 1:
        fields["conversion_currency"] = summaries[0].group(2).upper()
        fields["exchange_rate"] = _money(summaries[0].group(3))
        if line_item_count == 1:
            fields["value_for_currency_conversion"] = _money(summaries[0].group(1))
    return fields


def _money(s: str | None) -> float | str | None:
    if not s:
        return None
    s = str(s).strip().lstrip("$").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return s


def format_net_quantity(quantity, unit) -> str | None:
    """Render a line's Net Quantity in HTSUS Units the way the form prints it.

    The declaration prints this column as "420.00 NO" / "41.33 DOZ" / "477.36 KG":
    two decimals and the HTSUS unit code. line_items[].manifest_note carries this
    string, so downstream code can read the per-line quantity without joining two
    fields itself.

    Returns None when either half is missing -- a line whose quantity could not be
    read must not borrow a neighbour's value or invent "0.00".
    """
    if quantity is None or not unit:
        return None
    try:
        return f"{Decimal(str(quantity)):.2f} {str(unit).strip()}"
    except (InvalidOperation, ValueError):
        return None


def normalise_manifest_quantity(raw: str | None) -> str:
    """Normalise a printed package count to "<qty> CTN".

    Declarations spell the same figure several ways: the carton unit as
    "CT" (B-1), "CTN" (A-1, H-1, ...) or "CTNS" (C-1, K-1, ...), and the same
    count as "PCS" (HAMU2014380) or "PKG" on other batches. The quantity is what
    matters downstream, so every package unit is canonicalised to CTN instead of
    leaving the printer's abbreviation in the field.

    The digits are kept exactly as printed, including any thousands separator
    ("1,320 CTN"), and an empty/absent value stays "". A unit this function does
    not know ("KG", a weight) passes through untouched rather than being relabelled
    as cartons.
    """
    if not raw:
        return ""
    match = re.match(r"\s*(\d[\d,]*)\s*(?:CT|CTN|CTNS|PCS|PKG|PKGS)\s*$",
                     str(raw), re.I)
    return f"{match.group(1)} CTN" if match else str(raw).strip()


def _extract_text(pdf_path: str) -> list[str]:
    """Return all lines from every page of the PDF.

    Fallback: some PDFs (e.g. K-1.pdf) have a broken ToUnicode map on some
    pages, making pdfplumber emit garbled text (repeated letter runs).
    PyMuPDF reads those correctly, so use it for the whole document when
    any page's pdfplumber output lacks recognizable form markers.
    """
    page_texts: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_texts.append(page.extract_text(x_tolerance=3, y_tolerance=3) or "")

    if all(_has_form_markers(t) for t in page_texts):
        return [l for t in page_texts for l in t.splitlines()]

    alt = _extract_text_pymupdf(pdf_path)
    if alt and _has_form_markers("\n".join(alt)):
        return alt

    return [l for t in page_texts for l in t.splitlines()]


# Markers that identify a readable CBP 7501 text layer
_FORM_MARKERS = ("ENTRY SUMMARY", "Filer Code/Entry Number", "CBP Form 7501")


def _has_form_markers(text: str) -> bool:
    """True if text contains recognizable CBP 7501 form markers."""
    return sum(1 for m in _FORM_MARKERS if m in text) >= 2


def _extract_text_pymupdf(pdf_path: str) -> list[str]:
    """Extract lines via PyMuPDF, grouping words into rows by y-coordinate."""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            return []
    out: list[str] = []
    try:
        doc = pymupdf.open(pdf_path)
    except Exception:
        return []
    try:
        for page in doc:
            out.extend(_pymupdf_rows(page))
    finally:
        doc.close()
    return out


def _pymupdf_rows(page) -> list[str]:
    """Group PyMuPDF words into visual rows by y-coordinate."""
    words = page.get_text("words")
    rows: dict[float, list] = {}
    for w in words:
        y = round(w[1], 1)
        key = None
        for k in rows:
            if abs(k - y) <= 2.5:
                key = k
                break
        if key is None:
            key = y
            rows[key] = []
        rows[key].append(w)
    out: list[str] = []
    for y in sorted(rows):
        ws = sorted(rows[y], key=lambda w: w[0])
        out.append(" ".join(w[4] for w in ws))
    return out


# ── Block splitter ────────────────────────────────────────────────────────────
# 排除表头噪音（字段标签行）
_HEADER_NOISE = re.compile(
    r"^(0\d{2}|[1-9]\d{2})\s+"
    r"(A\.HTSUS|B\.AD|B\.ADA|Line\s+No|A\.Gross|Net\s+Quantity|"
    r"28\.|29\.|30\.|31\.|32\.|33\.|34\.)"
)

def _is_real_line_start(line: str) -> bool:
    """True only for genuine line-item start rows.
    Valid: '001 HUMIDIFIERS,EVAPORATIVE'  /  '001 PRD ANY...'  /  '007 IEEPA...'
    Invalid: '499 - MPF ...'  /  '707 CTN'  /  '669 C $801'  /  header label rows
    """
    s = line.strip()
    # manifest 行（如 "887 CTN"）优先排除
    if MANIFEST_RE.match(s):
        return False
    # CHGS 行（如 "669 C $801"，Visa Number + CHGS 同行）优先排除
    if re.match(r"^\d+\s+C\s+\$[\d,]+", s):
        return False
    if not LINE_START_RE.match(s):
        return False
    if _HEADER_NOISE.match(s):
        return False
    return True


def _split_blocks(all_lines: list[str]) -> list[list[str]]:
    """
    Slide through lines and group them into per-line-item blocks.
    A manifest row (e.g. "887 CTN") immediately before a LINE_START row
    is included at the top of that block.
    """
    blocks: list[list[str]] = []
    i = 0
    while i < len(all_lines):
        if _is_real_line_start(all_lines[i]):
            start = (i - 1
                     if i > 0 and MANIFEST_RE.match(all_lines[i - 1].strip())
                     else i)
            j = i + 1
            while j < len(all_lines) and not _is_real_line_start(all_lines[j]):
                j += 1
            blocks.append(all_lines[start:j])
            i = j
        else:
            i += 1
    return blocks


# PRD ANY / IEEPA 前缀（新版 7501）
_PRD_IEEPA_RE = re.compile(r"^(PRD ANY|IEEPA)")

# ── Single-block parser ───────────────────────────────────────────────────────
def _parse_block(lines: list[str]) -> dict | None:
    # Find the actual LINE_START row within the block
    ls_idx = next(
        (i for i, l in enumerate(lines) if _is_real_line_start(l)),
        None,
    )
    if ls_idx is None:
        return None

    manifest_note = (
        lines[ls_idx - 1].strip()
        if ls_idx > 0 and MANIFEST_RE.match(lines[ls_idx - 1].strip())
        else None
    )

    m = re.match(r"^(\d{3})\s+(.*)", lines[ls_idx].strip())
    line_no_str = m.group(1)          # keep "001", "007", etc. as-is
    first_desc  = m.group(2).strip()
    is_ieepa    = "IEEPA-RECIPROCAL" in first_desc

    # 新版：first_desc 是 "PRD ANY..." / "IEEPA..."，作为 exclusion_note
    # 旧版：first_desc 是商品描述，exclusion_note 置 None，描述留给 commodity.description
    has_prefix = bool(_PRD_IEEPA_RE.match(first_desc))

    entry: dict = {
        "line_no":             line_no_str,
        "line_type":           "IEEPA_EXCLUSION_232" if is_ieepa else "STANDARD",
        "manifest_note":       manifest_note,
        "exclusion_note":      first_desc if has_prefix else None,
        "tariff_subheadings":  [],
        "commodity":           {"description": first_desc if not has_prefix else ""},
        "fees":                [],
    }

    for raw in lines[ls_idx + 1:]:
        line = raw.strip()
        if not line or line in SKIP_LINES:
            continue
        if any(line.startswith(p) for p in SKIP_PREFIXES):
            continue
        if MANIFEST_RE.match(line):
            continue

        # 499/501 fee rows
        fm = FEE_RE.match(line)
        if fm:
            entry["fees"].append({
                "code":        fm.group(1),
                "description": fm.group(2).strip(),
                "rate":        fm.group(3),
                "amount":      _money(fm.group(4)),
            })
            continue

        # CHGS row
        cm = CHGS_RE.match(line)
        if cm:
            entry["commodity"]["chgs"] = _money(cm.group(1))
            continue

        # Main commodity row — 格式A（含 gross weight）
        com = COMMODITY_RE.match(line)
        if com:
            htsus = com.group(1)
            entry["commodity"].update({
                "htsus":             htsus,
                "description":       entry["commodity"].get("description") or COMMODITY_DESCRIPTIONS.get(htsus, ""),
                "gross_weight_kg":   float(com.group(2).replace(",", "")),
                "net_quantity":      float(com.group(3).replace(",", "")),
                "net_quantity_unit": com.group(4),
                "entered_value":     _money(com.group(5)),
                "htsus_rate":        com.group(6),
                "duty_amount":       _money(com.group(7)),
            })
            continue

        # Main commodity row — 格式B（无 gross weight，如 "9403.20.0040 36.00 NO $455 FREE $0.00"）
        com2 = COMMODITY_RE_NOGROSS.match(line)
        if com2:
            htsus = com2.group(1)
            # gross_weight 从 tariff_subheadings 里取（已解析的最后一条带 KG 的行）
            gross_kg = next(
                (s["steel_weight_kg"] for s in reversed(entry["tariff_subheadings"]) if s["steel_weight_kg"]),
                None
            )
            entry["commodity"].update({
                "htsus":             htsus,
                "description":       entry["commodity"].get("description") or COMMODITY_DESCRIPTIONS.get(htsus, ""),
                "gross_weight_kg":   gross_kg,
                "net_quantity":      float(com2.group(2).replace(",", "")),
                "net_quantity_unit": com2.group(3),
                "entered_value":     _money(com2.group(4)),
                "htsus_rate":        com2.group(5),
                "duty_amount":       _money(com2.group(6)),
            })
            # gross weight 已归入 commodity，从 tariff_subheadings 清掉该字段避免重复
            for s in entry["tariff_subheadings"]:
                if s["steel_weight_kg"] == gross_kg:
                    s["gross_weight_kg"] = s.pop("steel_weight_kg")
                    break
            continue

        # Tariff sub-heading rows (9903.xx.xx)
        tm = TARIFF_RE.match(line)
        if tm:
            steel_kg = (
                float(tm.group(2).replace(" KG", "").replace(",", "").strip())
                if tm.group(2)
                else None
            )
            entry["tariff_subheadings"].append({
                "htsus":           tm.group(1),
                "steel_weight_kg": steel_kg,
                "rate":            tm.group(3),
                "amount":          _money(tm.group(4)),
            })
            continue

        # Annotation rows: append to exclusion_note (新版格式专用)
        if entry["exclusion_note"] and re.match(r"^(CN/HK|ARTICLE OF|DERIV STL)", line):
            entry["exclusion_note"] += " | " + line

    return entry


# ── Header extractor ──────────────────────────────────────────────────────────
def _parse_header(all_lines: list[str], _page0_words: list = None) -> dict:
    """
    Extract fixed header fields by scanning lines directly.
    Each field targets the specific data line where it appears in the PDF,
    avoiding cross-field contamination from full-text regex.
    """
    text = "\n".join(all_lines)

    def _search(pattern: str, default: str = "") -> str:
        m = re.search(pattern, text, re.MULTILINE)
        return m.group(1).strip() if m else default

    def _line_match(pattern: str, lines: list[str], default: str = "") -> str:
        """Search each line individually; return first match group(1)."""
        for l in lines:
            m = re.search(pattern, l)
            if m:
                return m.group(1).strip()
        return default

    # ── Filer Code/Entry Number：格式如 8G8-5538712-9 或 8S2-0153809-5
    filer_m = re.search(r"\b([A-Z0-9]{2,4}-\d{5,}-\d)\b", text)
    filer_code = filer_m.group(1) if filer_m else ""

    # ── 行5：filer_code? 01 ABI/A 日期 surety bond port entry_date
    # 兼容两种格式：
    #   有 filer_code：NXU-9997461-8 01 ABI/A 12/19/25 036 8 2704 12/09/25
    #   无 filer_code：01 ABI/A 02/26/26 856 8 2704 02/14/26
    row5_m = re.search(
        r"(?:[A-Z0-9]+-\d+-\d\s+)?(\d{2})\s+(ABI/\w+)\s+(\d{2}/\d{2}/\d{2})(?:\s+HBA)?\s+(\d+)\s+(\d)\s+(\d{4})\s+(\d{2}/\d{2}/\d{2})",
        text
    )
    entry_type    = row5_m.group(2)  if row5_m else ""
    summary_date  = _fix_year(row5_m.group(3)) if row5_m else ""
    surety_number = row5_m.group(4)  if row5_m else ""
    bond_type     = row5_m.group(5)  if row5_m else ""
    port_code     = row5_m.group(6)  if row5_m else ""
    entry_date    = _fix_year(row5_m.group(7)) if row5_m else ""

    # ── 行7：<carrier> <mode> <country_of_origin> <import_date>
    # 格式：NESTOS 11 VN 12/09/25  或  CMA CGM FORT DIAMANT 11 CN 02/14/26
    row7_m = re.search(
        r"^(.+?)\s+(\d{2})\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{2})\s*$",
        text, re.MULTILINE
    )
    importing_carrier  = row7_m.group(1) if row7_m else ""
    country_of_origin  = row7_m.group(3) if row7_m else ""
    import_date        = _fix_year(row7_m.group(4)) if row7_m else ""

    # ── 行9：<bl_number> <manufacturer_id> <exporting_country> <export_date>
    # 格式A：CMDU GGZ2832805 CNGUAMAR4021ZHO CN 01/22/26
    # 格式B：ZIMU HAI80183820, LAX250603307 VNGRELTD9191HAN VN 11/22/25
    # 通用：最后三列固定是 manufacturer_id(无空格) country(2字母) date
    row9_m = re.search(
        r"^(.+?)\s+(\S+)\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{2})\s*$",
        text, re.MULTILINE
    )
    # row9 可能也匹配到 row7，需排除：确认 bl_number 行包含数字
    if row9_m and re.search(r"\d{6,}", row9_m.group(1)):
        bl_number         = row9_m.group(1).strip()
        manufacturer_id   = row9_m.group(2)
        exporting_country = row9_m.group(3)
        export_date       = _fix_year(row9_m.group(4))
    else:
        # 精确匹配：找紧跟 "13. Manufacturer ID" 标签行的下一数据行
        bl_m = re.search(r"13\..*?\n(.+?)\s+(\S+)\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{2})", text, re.DOTALL)
        bl_number         = bl_m.group(1).strip() if bl_m else ""
        manufacturer_id   = bl_m.group(2)         if bl_m else ""
        exporting_country = bl_m.group(3)         if bl_m else ""
        export_date       = _fix_year(bl_m.group(4)) if bl_m else ""

    # ── 行11：57078 2704
    ports_m = re.search(r"^(\d{5})\s+(\d{4})\s*$", text, re.MULTILINE)
    foreign_port    = ports_m.group(1) if ports_m else ""
    us_port         = ports_m.group(2) if ports_m else ""

    # ── location / reference
    location_m  = re.search(r"(Y\d+\s+Voyage:\s+\S+)", text)
    location    = location_m.group(1) if location_m else ""
    # Customer Reference # 属于 consignee 地址块，映射到 consignee.customer_reference
    ref_m         = re.search(r"Customer Reference #\s*(\S+)", text)
    customer_ref  = ref_m.group(1) if ref_m else ""
    # field 24 Reference Number 通常为空（A-1 该字段无数据）
    reference     = ""

    # ── manifest quantity
    # Same rule as the v2 path: any digit count, optional thousands separator, and
    # any package unit (CT / CTN / CTNS / PCS / PKG) normalised to CTN.
    manifest_m  = re.search(r"(\d[\d,]*\s+(?:CTN?S?|CT|PKGS?|PCS))\b", text)
    manifest    = normalise_manifest_quantity(manifest_m.group(1)) if manifest_m else ""

    # ── 行15：KBJ TRADING LLC  WUHAN JIU ZHOU TONG TRADING CO,.
    # 行16：Street: LIN LI...  Street: NO.95...
    # 行19：City: FONTANA State: C A Z ip: 92337-6970 City: WUHAN State: CNZip: 43000
    # consignee name：25. 标签行下一行，左半部分（到第二个公司名之前）
    # 格式：KBJ TRADING LLC  WUHAN JIU ZHOU TONG TRADING CO,.
    #   或：MWEKRE INC  MWEKRE INC
    # 用 pdfplumber 坐标精确切分 consignee / importer name
    # 第二个 "Street:" 的 x0 即为 importer 列起点
    _words = _page0_words  # 由调用方传入
    street_xs = sorted(set(
        round(w["x0"]) for w in _words if w["text"] in ("Street:", "Street")
    ))
    # 找第一行 name（紧跟 "25." 标签行）
    name_line_m = re.search(r"25\..*?\n([^\n]+)", text, re.DOTALL)
    name_line = name_line_m.group(1).strip() if name_line_m else ""

    # 策略1：完全重复（同名公司），逐字符扫
    consignee_name = ""
    importer_name  = ""
    for i in range(1, len(name_line)):
        l, r = name_line[:i].strip(), name_line[i:].strip()
        if l and r and l == r:
            consignee_name = l
            importer_name  = r
            break

    if not consignee_name:
        # 策略2：用坐标切分——找 name 行中 x0 >= importer_col_x 的第一个词
        importer_col_x = street_xs[1] if len(street_xs) >= 2 else None
        if importer_col_x:
            name_words_sorted = sorted(
                [w for w in _words if name_line and w["text"] in name_line.split()
                 and w["top"] < 250],   # 限定在页面上半部分
                key=lambda w: (round(w["top"]), w["x0"])
            )
            # 找同一行中 x0 >= importer_col_x 的第一个词，作为 importer name 起点
            left_parts, right_parts = [], []
            for w in name_words_sorted:
                if w["x0"] >= importer_col_x - 5:
                    right_parts.append(w["text"])
                else:
                    left_parts.append(w["text"])
            consignee_name = " ".join(left_parts).strip()
            importer_name  = " ".join(right_parts).strip()

    if not consignee_name:
        # 兜底：多空格切，或整行
        parts = re.split(r"\s{2,}", name_line)
        consignee_name = parts[0].strip()
        importer_name  = parts[1].strip() if len(parts) > 1 else parts[0].strip()

    # consignee street：第一个 "Street:" 到第二个 "Street:" 之间
    consignee_street_m = re.search(r"Street:\s*(.*?)\s+Street:", text)
    consignee_street = consignee_street_m.group(1).strip() if consignee_street_m else ""

    # importer street：第二个 "Street:" 之后
    importer_street_m = re.search(r"Street:.*?Street:\s*([^\n]+)", text)
    importer_street = importer_street_m.group(1).strip() if importer_street_m else ""

    # City 可能是多词（如 MORENO VALLEY），匹配到 State: 为止
    consignee_city_m = re.search(r"City:\s*([A-Z][A-Z\s]+?)\s+State:", text)
    consignee_city   = consignee_city_m.group(1).strip() if consignee_city_m else ""

    # State/Zip：兼容 "State: C A Z ip:" 和 "State: COZip:" 两种格式
    state_zip_m = re.search(
        r"State:\s*([A-Z](?:\s*[A-Z]?)*)\s*Z\s*ip:\s*([\d]{5}-?[\d]{0,4})", text
    )
    consignee_state = re.sub(r"\s+", "", state_zip_m.group(1)) if state_zip_m else ""
    consignee_zip   = state_zip_m.group(2) if state_zip_m else ""

    # importer city / zip：第二组 City/Zip
    # City 匹配多词（到 State: 或 Zip: 为止）
    city_all   = re.findall(r"City:\s*([A-Z][A-Z\s]+?)\s+(?:State:|Zip:)", text)
    zip_all    = re.findall(r"Zip:\s*([\d]{5}-?[\d]{0,4})", text)
    importer_city = city_all[1]  if len(city_all) > 1  else (city_all[0]  if city_all  else "")
    importer_zip  = zip_all[1]   if len(zip_all)  > 1  else (zip_all[0]   if zip_all   else "")

    # ── totals
    # entered_value：$ 8,051  （在 "501 - HMF $10.07 $ 8,051" 行末）
    # entered_value 在 "501 - HMF $xx.xx $ 8,051" 行末
    ev_m = re.search(r"501\s*-\s*HMF\s+\$[\d.]+\s+\$\s*([\d,]+)", text)
    if not ev_m:
        ev_m = re.search(r"35\.Total Entered Value.*?\$\s*([\d,]+)", text, re.DOTALL)
    entered_value = _money(ev_m.group(1)) if ev_m else None

    # duty：$2,568.06  单独一行紧跟 TOTALS 区
    duty_m = re.search(r"37\.Duty\s*\n\s*\$([\d,]+\.\d{2})", text)
    if not duty_m:
        duty_m = re.search(r"Ascertained Duty\s*37\.Duty\s*\n\s*\$([\d,]+\.\d{2})", text)
    if not duty_m:
        # 直接找 $2,568.06 格式独立行
        duty_m = re.search(r"^\$([\d,]+\.\d{2})\s*$", text, re.MULTILINE)
    duty = _money(duty_m.group(1)) if duty_m else None

    # other_fees：Total Other Fees 下一个 $ 值
    # other_fees 在 "Total Other Fees" 后 1~2 行，格式 "$ 43.65"
    other_m = re.search(r"Total Other Fees\s*\n\s*\$\s*([\d,]+\.[\d]{2})", text)
    if not other_m:
        other_m = re.search(r"39\.Other\s*\n\s*\$([\d.]+)", text)
    if not other_m:
        # 兜底：找独立行 "$ 43.65"
        other_m = re.search(r"^\$\s*([\d]+\.[\d]{2})\s*$", text, re.MULTILINE)
    other_fees = _money(other_m.group(1)) if other_m else None

    # grand_total：40. Total 右侧或下一行
    gt_m = re.search(r"40\.\s*Total\s*\n\s*\$([\d,]+\.\d{2})", text)
    if not gt_m:
        gt_m = re.search(r"OR owner\s+\$([\d,]+\.\d{2})", text)
    grand_total = _money(gt_m.group(1)) if gt_m else None

    mpf_m = re.search(r"499\s*-\s*MPF\s*\$([\d.]+)", text)
    hmf_m = re.search(r"501\s*-\s*HMF\s*\$([\d.]+)", text)

    return {
        "form":                   "CBP 7501",
        "currency":               "USD",
        "invoice_currency":       _invoice_currency(text),
        "filer_code_entry_number":  filer_code,
        "entry_type":             entry_type,
        "summary_date":           summary_date,
        "surety_number":          surety_number,
        "bond_type":              bond_type,
        "port_code":              port_code,
        "entry_date":             entry_date,
        "importing_carrier":      importing_carrier,
        "country_of_origin":      country_of_origin,
        "import_date":            import_date,
        "bl_number":              bl_number,
        "manufacturer_id":        manufacturer_id,
        "exporting_country":      exporting_country,
        "export_date":            export_date,
        "foreign_port_of_lading": foreign_port,
        "us_port_of_unlading":    us_port,
        "location_of_goods":      location,
        "reference_number":       reference,
        "manifest_quantity":      manifest,
        "consignee": {
            "name":   consignee_name,
            "street": consignee_street,
            "city":   consignee_city,
            "state":  consignee_state,
            "zip":    consignee_zip,
            "customer_reference": customer_ref,
        },
        "importer_of_record": {
            "name":    importer_name,
            "street":  importer_street,
            "city":    importer_city,
            "country": exporting_country,
            "zip":     importer_zip,
        },
        "totals": {
            "entered_value_usd": entered_value,
            "duty_usd":          duty,
            "other_fees_usd":    other_fees,
            "grand_total_usd":   grand_total,
            "mpf_usd":           _money(mpf_m.group(1)) if mpf_m else None,
            "hmf_usd":           _money(hmf_m.group(1)) if hmf_m else None,
        },
    }


# ── v2 Helpers ─────────────────────────────────────────────────────────────
def _fix_year(d):
    """Normalize dates to ISO yyyy-MM-dd.

    Accepts MM/DD/YY, MM/DD/YYYY, or already-ISO yyyy-MM-dd.
    Returns "" for empty input; leaves unrecognized values unchanged.
    """
    if not d:
        return d
    s = d.strip()
    # Already ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    m = re.match(r"^(\d{2})/(\d{2})/(\d{2}|\d{4})$", s)
    if not m:
        return s
    mm, dd, yy = m.group(1), m.group(2), m.group(3)
    if len(yy) == 2:
        yy = "20" + yy
    return f"{yy}-{mm}-{dd}"


def _extract_text_lines(pdf_path):
    """Return flat list of text lines from all pages."""
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            lines.extend(text.splitlines())
    return lines


def _extract_two_col_fields(pdf_path):
    """
    Use word-level x-coordinates to split the two-column consignee/importer block.
    Returns (consignee_name, consignee_street, importer_name, importer_street).
    Left column: x0 < mid_x; Right column: x0 >= mid_x.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[0]
            mid_x = page.width / 2
            words = page.extract_words(x_tolerance=3, y_tolerance=3)

            # Find the y of the "29.Ultimate Consignee" label
            label_y = None
            for w in words:
                if "29." in w["text"] or ("Ultimate" in w["text"]):
                    label_y = w["top"]
                    break
            if label_y is None:
                return _extract_two_col_fields_pymupdf(pdf_path)

            # Collect words in the 3 data rows below the label (name, street, city)
            data_words = [
                w for w in words
                if label_y + 8 < w["top"] < label_y + 60
            ]

            # Group by rounded y (row)
            rows = {}
            for w in data_words:
                ry = round(w["top"])
                rows.setdefault(ry, []).append(w)

            sorted_rows = [words for _, words in sorted(rows.items())]

            def join_col(row_words, right_col):
                side = [w["text"] for w in row_words
                        if (w["x0"] >= mid_x) == right_col]
                return " ".join(side)

            consignee_name   = join_col(sorted_rows[0], False) if len(sorted_rows) > 0 else ""
            importer_name    = join_col(sorted_rows[0], True)  if len(sorted_rows) > 0 else ""
            consignee_street = join_col(sorted_rows[1], False) if len(sorted_rows) > 1 else ""
            importer_street  = join_col(sorted_rows[1], True)  if len(sorted_rows) > 1 else ""

            return consignee_name, consignee_street, importer_name, importer_street
    except Exception:
        return _extract_two_col_fields_pymupdf(pdf_path)


def _extract_two_col_fields_pymupdf(pdf_path):
    """PyMuPDF fallback for the consignee/importer two-column block.

    Used when the pdfplumber text layer is broken (e.g. K-1.pdf).
    """
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            return "", "", "", ""
    try:
        doc = pymupdf.open(pdf_path)
        page = doc[0]
        mid_x = page.rect.width / 2
        words = page.get_text("words")
        doc.close()
    except Exception:
        return "", "", "", ""

    label_y = None
    for w in words:
        if w[4].startswith("29.") or "Ultimate" in w[4]:
            label_y = w[1]
            break
    if label_y is None:
        return "", "", "", ""

    data_words = [w for w in words if label_y + 8 < w[1] < label_y + 60]

    rows: dict[float, list] = {}
    for w in data_words:
        ry = round(w[1])
        rows.setdefault(ry, []).append(w)

    sorted_rows = [ws for _, ws in sorted(rows.items())]

    def join_col(row_words, right_col):
        side = [w[4] for w in row_words if (w[0] >= mid_x) == right_col]
        return " ".join(side)

    consignee_name   = join_col(sorted_rows[0], False) if len(sorted_rows) > 0 else ""
    importer_name    = join_col(sorted_rows[0], True)  if len(sorted_rows) > 0 else ""
    consignee_street = join_col(sorted_rows[1], False) if len(sorted_rows) > 1 else ""
    importer_street  = join_col(sorted_rows[1], True)  if len(sorted_rows) > 1 else ""

    return consignee_name, consignee_street, importer_name, importer_street


_NUMBERED_LABEL_RE = re.compile(r"^(\d{1,2})\.$")


def _extract_numbered_cells(pdf_path, numbers):
    """Read consecutive numbered fields by geometry, not by line text.

    Fields 25-28 (or 21-24 on older layouts) share one row whose cells contain
    a mix of identifiers and free text. A plain line regex cannot split them:
    ``WAC8 Voyage: 085E`` under field 25 looks exactly like three values, so the
    voyage number ends up in field 28. Reading the row by x-coordinate keeps each
    cell separate regardless of what the cells contain.

    pdfplumber supplies the words, matching ``_extract_text``'s preference (it
    decodes free text better); PyMuPDF is the fallback for PDFs whose pdfplumber
    text layer is broken (K-1 / L-1).

    Returns ``{field_number: text}``, or None when the labels are not found.
    """
    for reader in (_pdfplumber_words, _pymupdf_words):
        pages = reader(pdf_path)
        if not pages:
            continue
        cells = _cells_from_pages(pages, numbers)
        if cells is not None:
            return cells
    return None


def _pdfplumber_words(pdf_path):
    """Word boxes per page via pdfplumber, in the same shape as PyMuPDF's."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return [[(w["x0"], w["top"], w["x1"], w["bottom"], w["text"])
                     for w in page.extract_words(x_tolerance=2, y_tolerance=2)]
                    for page in pdf.pages]
    except Exception:
        return None


def _pymupdf_words(pdf_path):
    """Word boxes per page via PyMuPDF (x0, top, x1, bottom, text)."""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            return None
    try:
        doc = pymupdf.open(pdf_path)
    except Exception:
        return None
    try:
        return [[(w[0], w[1], w[2], w[3], w[4]) for w in page.get_text("words")]
                for page in doc]
    except Exception:
        return None
    finally:
        doc.close()


def _cells_from_pages(pages, numbers):
    """Split the label row for ``numbers`` into cells by label x-coordinate."""
    first, last = numbers[0], numbers[-1]
    labels = {f"{n}." for n in numbers}

    for words in pages:
        # Locate the label row: the two outer labels sharing one baseline.
        starts = sorted((w[0], w[1]) for w in words if w[4] == f"{first}.")
        ends = sorted((w[0], w[1]) for w in words if w[4] == f"{last}.")
        row = next((ya for xa, ya in starts for xb, yb in ends
                    if abs(ya - yb) <= 2 and xb > xa), None)
        if row is None:
            continue
        top = row

        lefts = {}
        for w in words:
            if w[4] in labels and abs(w[1] - top) <= 2:
                lefts[w[4]] = min(lefts.get(w[4], w[0]), w[0])
        if len(lefts) != len(numbers):
            continue
        columns = [lefts[f"{n}."] for n in numbers]

        # The values sit on the line(s) directly below, so stop at the next
        # numbered label instead of guessing a fixed row height.
        bottom = min(
            (w[1] for w in words
             if w[1] > top + 2 and _NUMBERED_LABEL_RE.match(w[4])
             and int(w[4][:-1]) > last),
            default=top + 20,
        )
        bottom = min(bottom, top + 20)

        cells = {n: [] for n in numbers}
        for w in words:
            if not top + 2 < w[1] < bottom:
                continue
            centre = (w[0] + w[2]) / 2
            for i, number in enumerate(numbers):
                right = columns[i + 1] if i + 1 < len(columns) else float("inf")
                if columns[i] <= centre < right:
                    cells[number].append((w[0], w[4]))
                    break
        return {n: _value_or_blank(" ".join(text for _, text in sorted(cells[n])))
                for n in numbers}
    return None


def _value_or_blank(text):
    """Drop the placeholder glyphs a form prints to mean "field not used"."""
    return "" if text.strip() in _PLACEHOLDER_VALUES else text


_PLACEHOLDER_VALUES = {"/", "-", "--", "N/A", "NA"}


# ── Header v2 ─────────────────────────────────────────────────────────────────

def _parse_header_v2(all_lines, pdf_path):
    text = "\n".join(all_lines)

    def _find(pattern, default="", flags=0):
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else default

    # ── Fields 1-7 ────────────────────────────────────────────────────────────
    # 兼容三种格式:
    #   C-1: 8S2-0237708-9 01 ABI/P 04/24/26 036 8 2704 04/19/26
    #   B-1: 9H3-0051130-4 01 036 8 2704
    #   D-1: INL-12037930 01 ABI/P 06/09/2026  (split: next line 036 8 3002 05/28/2026)
    # D-1 / E-1 split format: filer/type/summary on one line, surety/bond/port/date on next
    # E-1: "ENTRY SUMMARY INL-12043045 11 ABI/P" (no summary_date, no entry_date)
    hdr7 = re.search(
        r"(?:ENTRY\s+SUMMARY\s+)?"  # E-1 prefix
        r"([\w]+-\d{5,}(?:-\d)?)\s+(\d{2})\s+ABI/\w"
        r"(?:\s+(\d{2}/\d{2}/\d{2,4}))?\s*"    # summary_date optional
        r"(?:\n(?:[^\n]*\n)?)?"                   # optionally skip label line (may be single-line)
        r"\s*(\d{3})\s+(\d)\s+(\d{4})"
        r"(?:\s+(\d{2}/\d{2}/\d{2,4}))?",       # entry_date optional
        text
    )
    if hdr7:
        filer_entry  = hdr7.group(1)
        entry_type   = hdr7.group(2)
        summary_date = _fix_year(hdr7.group(3) or "")
        surety       = hdr7.group(4)
        bond_type    = hdr7.group(5)
        port_code    = hdr7.group(6)
        entry_date   = _fix_year(hdr7.group(7) or "")
    else:
        # C-1 single-line: 8S2-0244691-8 01 ABI/P 06/11/26 054 8 2704 06/04/26
        hdr7 = re.search(
            r"([\w]+-\d{5,}(?:-\d)?)\s+(\d{2})\s+ABI/\w"
            r"(?:\s+(\d{2}/\d{2}/\d{2,4}))?\s*"
            r"(\d{3})\s+(\d)\s+(\d{4})"
            r"(?:\s+(\d{2}/\d{2}/\d{2,4}))?",
            text
        )
        if hdr7:
            filer_entry  = hdr7.group(1)
            entry_type   = hdr7.group(2)
            summary_date = _fix_year(hdr7.group(3) or "")
            surety       = hdr7.group(4)
            bond_type    = hdr7.group(5)
            port_code    = hdr7.group(6)
            entry_date   = _fix_year(hdr7.group(7) or "")
        else:
            # B-1: 9H3-0051130-4 01 036 8 2704 (no ABI/P, no summary_date)
            hdr7 = re.search(
                r"([\w]+-\d{5,}(?:-\d)?)\s+(\d{2})\s+(\d{3})\s+(\d)\s+(\d{4})"
                r"(?:\s+(\d{2}/\d{2}/\d{2,4}))?",
                text
            )
            if hdr7:
                filer_entry  = hdr7.group(1)
                entry_type   = hdr7.group(2)
                surety       = hdr7.group(3)
                bond_type    = hdr7.group(4)
                port_code    = hdr7.group(5)
                entry_date   = _fix_year(hdr7.group(6)) if hdr7.group(6) else ""
                summary_date = _fix_year(
                    _find(r"(\d{2}/\d{2}/\d{2,4})\s+\d{3}\s+\d\s+\d{4}")
                )
            else:
                # H-1: filer code 为空，数据行直接以 entry_type 开头
                #   "01 ABI/A 05/28/26 054 8 2704 05/17/26"
                # 锚定在字段 1-7 标签行之后
                hdr7 = re.search(
                    r"7\.\s*Entry Date[^\n]*\n"
                    r"\s*(\d{2})\s+ABI/\w"
                    r"(?:\s+(\d{2}/\d{2}/\d{2,4}))?\s+"
                    r"(\d{3})\s+(\d)\s+(\d{4})"
                    r"(?:\s+(\d{2}/\d{2}/\d{2,4}))?",
                    text
                )
                filer_entry  = ""
                entry_type   = hdr7.group(1) if hdr7 else ""
                summary_date = _fix_year(hdr7.group(2) or "") if hdr7 else ""
                surety       = hdr7.group(3) if hdr7 else ""
                bond_type    = hdr7.group(4) if hdr7 else ""
                port_code    = hdr7.group(5) if hdr7 else ""
                entry_date   = _fix_year(hdr7.group(6)) if hdr7 and hdr7.group(6) else ""

                # MRSU 格式：精简 4 字段 "9H3-0056078-0 01 054 8"（无 summary/port/entry date）
                if not hdr7:
                    hdr7 = re.search(
                        r"7\.\s*Entry Date[^\n]*\n"
                        r"\s*([\w]+-\d{5,}(?:-\d)?)\s+(\d{2})\s+(\d{3})\s+(\d)",
                        text
                    )
                    if hdr7:
                        filer_entry  = hdr7.group(1)
                        entry_type   = hdr7.group(2)
                        surety       = hdr7.group(3)
                        bond_type    = hdr7.group(4)
                        # port_code、summary_date、entry_date 从其他字段推导（后续补充）
                        port_code    = ""
                        summary_date = ""
                        entry_date   = ""

    # ── Fields 8-11 ───────────────────────────────────────────────────────────
    # 兼容: "8.Importing Carrier" 和 "8. Importing Carrier"
    cl = re.search(
        r"8\.\s*Importing Carrier[^\n]*\n([^\n]+?)\s+(\d{2})\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{2,4})",
        text, re.S
    )
    importing_carrier = cl.group(1).strip() if cl else ""
    country_of_origin = cl.group(3)         if cl else ""
    import_date       = _fix_year(cl.group(4)) if cl else ""

    # ── Fields 12-15 ──────────────────────────────────────────────────────────
    # 4-field: BL manufacturer_id country date (C-1: CMDU GGZ2832805 CNGUAMAR4021ZHO CN 01/22/26)
    # 3-field: BL country date (D-1: CNHUAHON117HUA CN 05/06/2026, no manufacturer ID)
    bl_4f = re.search(
        r"12\.\s*B/L or AWB N(?:o|umber)[^\n]*\n([^\n]+?)\s+(\S+)\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{2,4})",
        text, re.S
    )
    if bl_4f:
        bl_number         = bl_4f.group(1).strip()
        manufacturer_id   = bl_4f.group(2)
        exporting_country = bl_4f.group(3)
        export_date       = _fix_year(bl_4f.group(4))
    else:
        # D-1: 3 fields (no manufacturer ID)
        bl_3f = re.search(
            r"12\.\s*B/L or AWB N(?:o|umber)[^\n]*\n([^\n]+?)\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{4})",
            text, re.S
        )
        if bl_3f:
            bl_number         = bl_3f.group(1).strip()
            manufacturer_id   = ""
            exporting_country = bl_3f.group(2)
            export_date       = _fix_year(bl_3f.group(3))
        else:
            # MRSU: BL manufacturer_id country (no export_date)
            #   "MAEU270212301 CNHUIXINHUI CN"
            bl_3f_ned = re.search(
                r"12\.\s*B/L or AWB N(?:o|umber)[^\n]*\n([^\n]+?)\s+(\S+)\s+([A-Z]{2})\s*$",
                text, re.S | re.M
            )
            if bl_3f_ned:
                bl_number         = bl_3f_ned.group(1).strip()
                manufacturer_id   = bl_3f_ned.group(2)
                exporting_country = bl_3f_ned.group(3)
                export_date       = ""
            else:
                bl_number = manufacturer_id = exporting_country = export_date = ""

    # ── Fields 19-20 ──────────────────────────────────────────────────────────
    ports = re.search(
        r"19\.\s*Foreign Port[^\n]*20\.\s*U\.S\. Port[^\n]*\n\s*(\d{4,5})\s+(\d{4})",
        text, re.S
    )
    foreign_port = ports.group(1) if ports else ""
    us_port      = ports.group(2) if ports else ""

    # 如果 port_code 仍为空（MRSU 格式），使用 us_port 作为 port_code
    if not port_code and us_port:
        port_code = us_port

    # ── Fields 25-28 ──────────────────────────────────────────────────────────
    # 兼容两套编号: 新版 25-28 (25.Location / 26.Consignee / 27.Importer /
    # 28.Reference) 和旧版 21-24。这些格内既有号码也有自由文本 (如
    # "WAC8 Voyage: 085E")，纯文本正则会把字段 25 的内容当成 26/27 号，
    # 把航次号错填到 28.Reference Number，所以按标签横坐标分格读取。
    cells = _extract_numbered_cells(pdf_path, (25, 26, 27, 28)) \
        or _extract_numbered_cells(pdf_path, (21, 22, 23, 24)) \
        or {}
    consignee_number = cells.get(26, cells.get(22, ""))
    importer_number  = cells.get(27, cells.get(23, ""))
    location_of_goods = cells.get(25, cells.get(21, ""))
    raw_ref          = cells.get(28, cells.get(24, ""))
    # 过滤误匹配（如 "29." 标签）
    reference_number = raw_ref if raw_ref and re.match(r'\d', raw_ref) else ""

    # ── Fields 29-30: two-column consignee / importer ─────────────────────────
    consignee_name, consignee_street, importer_name, importer_street = \
        _extract_two_col_fields(pdf_path)

    # City / State / Zip (two-column on one line)
    # 兼容三种格式:
    #   B-1: City LOUISVILLE State CO Zip 80027-2932 City NINGBO State ZJ Zip 315000
    #   C-1: City: LOUISVILLE State: CO Zip: 80027 City: NINGBO State: Zip: 315000 CN
    #   (C-1 importer 无 State，Zip 后有 CN 后缀)
    city_m = re.search(
        r"City:?\s+([\w\s]+?)\s+State:?\s+([A-Z]{2})\s+Zip:?\s+(\S+)"
        r"\s+City:?\s+([\w\s]+?)\s+State:?\s*([A-Z]{2})?\s*Zip:?\s*(\S+)",
        text
    )
    consignee_city  = city_m.group(1).strip() if city_m else ""
    consignee_state = city_m.group(2)         if city_m else ""
    consignee_zip   = city_m.group(3)         if city_m else ""
    importer_city   = city_m.group(4).strip() if city_m else ""
    importer_state  = city_m.group(5) or ""   if city_m else ""
    importer_zip    = city_m.group(6)         if city_m else ""

    # ── Customer Reference ──────────────────────────────────────────────────────
    # 提取 Consignee 的 Customer Reference（如 "Destination: CA Customer Reference # CAAU8232594"）
    customer_ref = _find(r"Customer Reference\s*#\s*(\S+)")

    # ── Manifest Quantity ─────────────────────────────────────────────────────
    # Package counts can be one digit ("29 CTN") or thousands ("1,320 CTN"), so the
    # digits are matched with an optional thousands separator. An earlier form of
    # this pattern required three plain digits, which dropped two-box-count
    # declarations and truncated "1,320 CTN" to "320 CTN".
    # The unit may be CT / CTN / CTNS / PCS / PKG on different batches; all of them
    # are canonicalised to CTN by normalise_manifest_quantity(). Weights (KG) are
    # deliberately not in this list.
    manifest_qty = normalise_manifest_quantity(
        _find(r"\b(\d[\d,]*\s+(?:CTN?S?|CT|PKGS?|PCS))\b"))

    # ── Totals ────────────────────────────────────────────────────────────────
    # Entered value
    # B-1: $8,911.00  A.LIQ
    # C-1: Invoice Value +/- MMV Exchange Entered Value\nSGN2602APFV304062 5,959.00 USD
    # D-1: "Ent Value : $8317" or "501 10.39 $ 8317" line
    entered_value = _money(_find(r"\$([\d,]+\.\d{2})[ \t]+A\.LIQ"))
    if not entered_value:
        ev_m = re.search(r"Invoice Value[^\n]*\n\S+\s+([\d,]+\.\d+)\s+USD", text)
        entered_value = _money(ev_m.group(1)) if ev_m else None
    if not entered_value:
        ev_m = re.search(r"Ent Value\s*:\s*\$([\d,]+)", text)
        entered_value = _money(ev_m.group(1)) if ev_m else None
    if not entered_value:
        # D-1: "501 10.39 $ 8317" — entered value follows fee amounts
        ev_m = re.search(r"501\s+[\d.]+\s+\$\s*([\d,]+)", text)
        entered_value = _money(ev_m.group(1)) if ev_m else None

    # Duty
    # B-1: Total Other Fees 891.10
    # C-1: "41. Duty\n501 - HMF $7.45 $ 5,959\n$595.90"
    # D-1: "37.Duty\n...\n3035.56" (different field number)
    duty_m = re.search(r"Total Other Fees\s+([\d,]+(?:\.\d{2})?)", text)
    if not duty_m:
        duty_m = re.search(r"41\.\s*Duty\s*\n[^\n]*\n\$([\d,]+\.\d{2})", text)
    if not duty_m:
        duty_m = re.search(r"37\.\s*Duty(?:.|\n)*?\n\s*([\d,]+\.[\d]{2})", text)
    duty = _money(duty_m.group(1)) if duty_m else None

    # Tax (field 42 / 38)
    # B-1: "REASON CODE\n$11.14  0.00"  -- 0.00 is Tax
    # C-1: "42. Tax\n$ 41.03"
    tax_m = re.search(r"REASON CODE[^\n]*\n\$[\d.]+\s+([\d.]+)", text)
    if not tax_m:
        tax_m = re.search(r"42\.\s*Tax[^\n]*\n\$?\s*([\d]+\.[\d]{2})", text)
    if not tax_m:
        tax_m = re.search(r"38\.\s*Tax(?:.|\n)*?\n\$?\s*([\d]+\.[\d]{2})", text)
    tax = _money(tax_m.group(1)) if tax_m else None

    # Other fees (field 43 / 39)
    # B-1: 43.Other\n[extra]\n11.14
    # C-1: 43. Other\n[declaration header]\nAuthorized Agent $41.03
    other_fees_m = re.search(r"43\.\s*Other\s*\n(?:[^\n]*\n)?[^\n]*\$([\d]+\.[\d]{2})", text)
    if not other_fees_m:
        other_fees_m = re.search(r"39\.\s*Other(?:.|\n)*?\n[^\n]*\$?\s*([\d]+\.[\d]{2})", text)
    other_fees = _money(other_fees_m.group(1)) if other_fees_m else None

    # Grand total (field 44 / 40)
    # B-1: 44.Total\n[purchaser line] 902.24 (金额在下一行行尾)
    # C-1: 44. Total\nI declare... $636.93 (金额在下一行)
    # H-1: 44.Total\n[declare line]\n$3,716.49 (金额在下面第2行)
    grand_total_m = re.search(r"44\.\s*Total\s*\n(?:.*?\n){0,2}.*?\$?([\d,]+\.[\d]{2})\s*$", text, re.MULTILINE)
    if not grand_total_m:
        grand_total_m = re.search(r"40\.\s*Total(?:.|\n)*?\n.*?\$?([\d,]+\.[\d]{2})", text)
    grand_total = _money(grand_total_m.group(1)) if grand_total_m else None

    # HMF / MPF
    fee_summary_m = re.search(r"Other Fee Summary.*?(?=Total Other Fees|\Z)", text, re.DOTALL)
    fee_summary = fee_summary_m.group(0) if fee_summary_m else ""
    hmf_m = re.search(
        r"(?:501\s*-\s*HMF|Harbor Maintenance Fee)\s*(?:[\d.]+%\s*)?\$\s*([\d,]+(?:\.\d{2})?)",
        fee_summary
    )
    mpf_m = re.search(
        r"(?:499\s*-\s*MPF|Merchandise Processing Fee)\s*(?:[\d.]+%\s*)?\$\s*([\d,]+(?:\.\d{2})?)",
        fee_summary
    )
    hmf = _money(hmf_m.group(1)) if hmf_m else None
    mpf = _money(mpf_m.group(1)) if mpf_m else None
    if hmf is None:
        hmf = _money(_find(r"501\s*-\s*HMF\s*\$\s*([\d,]+(?:\.\d{2})?)"))
    if mpf is None:
        mpf = _money(_find(r"499\s*-\s*MPF\s*\$\s*([\d,]+(?:\.\d{2})?)"))
    # B-1: "Harbor Maintenance Fee  $11.14"
    # C-1: "Harbor Maintenance Fee 0.1250% $7.45"
    if hmf is None:
        hmf = _money(_find(r"Harbor Maintenance Fee\s+(?:[\d.]+%\s+)?\$([\d.]+)"))
    if mpf is None:
        mpf = _money(_find(r"Merchandise Processing Fee\s+(?:[\d.]+%\s+)?\$([\d.]+)"))
    # D-1: "HARBOR_MAINTENANCE_FEE(501) 0.125% 0.72"
    if hmf is None:
        hmf = _money(_find(r"HARBOR_MAINTENANCE_FEE\(501\)\s+[\d.]+%\s+\$?([\d.]+)"))
    if mpf is None:
        mpf = _money(_find(r"FORMAL_MERCHANDISE_PROCESSING_FEE\(499\)\s+[\d.]+%\s+\$?([\d.]+)"))

    return {
        "form":                    "CBP 7501",
        "currency":                "USD",
        "invoice_currency":        _invoice_currency(text),
        "filer_code_entry_number": filer_entry,
        "entry_type":              entry_type,
        "summary_date":            summary_date,
        "surety_number":           surety,
        "bond_type":               bond_type,
        "port_code":               port_code,
        "entry_date":              entry_date,
        "importing_carrier":       importing_carrier,
        "country_of_origin":       country_of_origin,
        "import_date":             import_date,
        "bl_number":               bl_number,
        "manufacturer_id":         manufacturer_id,
        "exporting_country":       exporting_country,
        "export_date":             export_date,
        "foreign_port_of_lading":  foreign_port,
        "us_port_of_unlading":     us_port,
        "location_of_goods":       location_of_goods,
        "reference_number":        reference_number,
        "manifest_quantity":       manifest_qty,
        "consignee": {
            "name":    consignee_name,
            "street":  consignee_street,
            "city":    consignee_city,
            "state":   consignee_state,
            "zip":     consignee_zip,
            "customer_reference": customer_ref,
        },
        "importer_of_record": {
            "name":    importer_name,
            "street":  importer_street,
            "city":    importer_city,
            "country": exporting_country,
            "zip":     importer_zip,
        },
        "totals": {
            "entered_value_usd": entered_value,
            "duty_usd":          duty,
            "other_fees_usd":    other_fees,
            "grand_total_usd":   grand_total,
            "mpf_usd":           mpf,
            "hmf_usd":           hmf,
        },
    }



# ── Block splitter v2 ─────────────────────────────────────────────────────────

def _split_blocks_v2(all_lines):
    """Group lines into per-line-item blocks; 499/501 absorbed into preceding block."""
    start_idx = 0
    for i, line in enumerate(all_lines):
        if "MASTER BILL" in line or "I.T. DATE" in line or "I.T. Date" in line or "--MBL--" in line:
            start_idx = i + 1
            break

    # Address-like lines to skip (street numbers that look like 3-digit line items)
    _ADDRESS_RE = re.compile(
        r"^(0\d{2}|[1-9]\d{2})\s+"
        r"(Terry|Avenue|Street|Road|Drive|Blvd|Lane|Way|North|South|East|West|"
        r"Seattle|City|State|Zip|Fulfillment|FONTANA|LOUISVILLE|MORENO|"
        r"\d+\s+(Terry|Avenue|Street|Road|Drive|Blvd|Lane|Way))",
        re.I
    )
    # Street address fragments that appear on blocks after address lines
    _ADDRESS_FRAG = re.compile(
        r"^(Terry|Avenue|Street|Road|Drive|Blvd|Lane|Way|North|South|East|West|"
        r"Seattle|City|State)\s+",
        re.I
    )

    blocks = []
    i = start_idx
    while i < len(all_lines):
        line = all_lines[i].strip()
        m = ITEM_START_RE.match(line)
        if m and int(m.group(1)) not in _FEE_CODES:
            # 跳过 manifest 行 (如 "379 CTNS", "520 CTN")
            if re.match(r'^\d{3,}\s+CT', line):
                i += 1
                continue
            # 跳过地址行 (如 "410 Terry Avenue North")
            if _ADDRESS_RE.match(line):
                i += 1
                continue
            # A manifest row (“65 CTN” / “772 CTN”) prints directly ABOVE the line it
            # belongs to, so pull it into the block. Without this it sits outside
            # every block and the box count is silently dropped -- which is what
            # happened on TGBU8030959, CMAU4603291, J-1, H-1 and G-1.
            start = i
            if i > start_idx and MANIFEST_RE.match(all_lines[i - 1].strip()):
                start = i - 1
            j = i + 1
            while j < len(all_lines):
                nxt = all_lines[j].strip()
                nm = ITEM_START_RE.match(nxt)
                if nm and int(nm.group(1)) not in _FEE_CODES:
                    # 同样跳过 manifest 行
                    if re.match(r'^\d{3,}\s+CT', nxt):
                        j += 1
                        continue
                    # 同样跳过地址行
                    if _ADDRESS_RE.match(nxt):
                        j += 1
                        continue
                    break
                j += 1
            blocks.append(all_lines[start:j])
            i = j
        else:
            i += 1
    return blocks


# ── Block parser v2 ───────────────────────────────────────────────────────────

def _parse_block_v2(lines):
    # A manifest row ("65 CTN" / "772 CTN") may lead the block after
    # _split_blocks_v2 pulled it in. It has to be recognised before ITEM_START_RE
    # gets a chance to read it as a line item: "772 CTN" matches that pattern too
    # (3 digits + text), which is how a 3-digit box count used to be mistaken for
    # line 772. The row is consumed only when the line after it really is an item,
    # so a stray manifest row left alone in a block cannot swallow the block.
    manifest_note = None
    first = lines[0].strip()
    body_from = 1
    if MANIFEST_RE.match(first) and len(lines) > 1:
        candidate = lines[1].strip()
        cm = ITEM_START_RE.match(candidate)
        if cm is not None and int(cm.group(1)) not in _FEE_CODES:
            # Same canonical form as header.manifest_quantity, so the two never
            # disagree on the unit for one declaration.
            manifest_note = normalise_manifest_quantity(first)
            first = candidate
            body_from = 2

    m = ITEM_START_RE.match(first)
    if not m:
        return None

    desc = re.sub(r"\s+[a-zA-Z]$", "", m.group(2)).strip()
    entry = {
        "line_no":            m.group(1),
        "line_type":          "STANDARD",
        "manifest_note":      manifest_note,
        "exclusion_note":     desc,
        "tariff_subheadings": [],
        "commodity":          {},
        "fees":               [],
    }
    commodity_desc_candidate = ""

    for raw in lines[body_from:]:
        line = raw.strip()
        if not line:
            continue
        if line in _SKIP_LINES:
            continue
        if any(line.startswith(p) for p in _SKIP_PREFIXES):
            continue
        if re.match(r"^[a-zA-Z]$", line):  # single stray char
            continue
        if re.match(r"^C\d{3,}$", line):   # C739, C887 codes
            continue

        # A leading manifest row (already recorded as manifest_note) must not be
        # re-read as merchandise text.
        if MANIFEST_RE.match(line):
            continue

        # D-1: skip "Invoice XXX" lines, "Not Related", BL/HBL lines, annotation lines
        if re.match(r"^Invoice\s+\d{3}", line):
            continue
        if line.strip() == "Not Related":
            continue
        # D-1 BL/HBL line: "CNHUAHON117HUA C139"
        if re.match(r"^[A-Z]{2,4}[A-Z0-9]{8,}\s+C\d{2,4}$", line):
            entry["hbl_number"] = line.split()[0]
            continue
        # D-1 PN# line: "PN# lounge chair"
        if re.match(r"^PN#\s+", line, re.I):
            entry["product_name"] = re.sub(r"^PN#\s+", "", line, flags=re.I)
            continue

        # D-1 fee: FORMAT_MERCHANDISE_PROCESSING_FEE(499) 0.3464% 2.00
        fd = FEE_D1_RE.match(line)
        if fd:
            code = fd.group(2)
            name = FEE_D1_NAME_MAP.get(fd.group(1), fd.group(1))
            entry["fees"].append({
                "code":        code,
                "description": name,
                "rate":        fd.group(3),
                "amount":      _money(fd.group(4)),
            })
            continue

        # Fee line
        fm = FEE_V2_RE.match(line)
        if fm:
            entry["fees"].append({
                "code":        fm.group(1),
                "description": fm.group(2).strip(),
                "rate":        fm.group(3),
                "amount":      _money(fm.group(4)),
            })
            continue

        # CHGS / Visa: "C $1,500"
        chgs_m = CHGS_RE.match(line)
        if chgs_m:
            entry["commodity"]["entered_value_chgs"] = _money(chgs_m.group(1))
            continue

        # D-1 commodity: HTSUS qty+unit rate amount (e.g. "9401.49.0000 48NO Free 0.00")
        cd = COMMODITY_D1_RE.match(line)
        if cd:
            htsus = cd.group(1)
            gross_kg = None
            if cd.group(3) == "KG":
                gross_kg = _money(cd.group(2))
                net_qty = _money(cd.group(2))
            else:
                net_qty = _money(cd.group(2))
            entry["commodity"] = {
                "description":       commodity_desc_candidate or COMMODITY_DESCRIPTIONS.get(htsus, ""),
                "htsus":             htsus,
                "gross_weight_kg":   gross_kg,
                "net_quantity":      net_qty,
                "net_quantity_unit": cd.group(3),
                "entered_value":     None,
                "htsus_rate":        cd.group(4),
                "duty_amount":       _money(cd.group(5)),
            }
            continue

        # Main HTSUS commodity (10-digit)
        # Try C-1 layout first (with KG, $), then B-1 layout
        cm_c1 = COMMODITY_V2_C1_RE.match(line)
        if cm_c1:
            htsus = cm_c1.group(1)
            entry["commodity"] = {
                "description":       commodity_desc_candidate or COMMODITY_DESCRIPTIONS.get(htsus, ""),
                "htsus":             htsus,
                "gross_weight_kg":   _money(cm_c1.group(2)),
                "net_quantity":      _money(cm_c1.group(3)),
                "net_quantity_unit": cm_c1.group(4),
                "entered_value":     _money(cm_c1.group(5)),
                "htsus_rate":        cm_c1.group(6),
                "duty_amount":       _money(cm_c1.group(7)),
            }
            continue

        cm = COMMODITY_V2_RE.match(line)
        if cm:
            htsus = cm.group(1)
            entry["commodity"] = {
                "description":       commodity_desc_candidate or COMMODITY_DESCRIPTIONS.get(htsus, ""),
                "htsus":             htsus,
                "gross_weight_kg":   None,
                "net_quantity":      _money(cm.group(2)),
                "net_quantity_unit": cm.group(3),
                "entered_value":     _money(cm.group(4)),
                "htsus_rate":        cm.group(5),
                "duty_amount":       _money(cm.group(6)),
            }
            continue

        # MRSU commodity with X placeholder (unit on next line)
        #   "9403.99.9061 X 2 Free 0.00"
        cm_x = COMMODITY_V2_X_RE.match(line)
        if cm_x:
            htsus = cm_x.group(1)
            entry["commodity"] = {
                "description":       commodity_desc_candidate or COMMODITY_DESCRIPTIONS.get(htsus, ""),
                "htsus":             htsus,
                "gross_weight_kg":   None,
                "net_quantity":      None,  # X placeholder, actual value unknown
                "net_quantity_unit": "",    # will parse from next line if present
                "entered_value":     _money(cm_x.group(2)),
                "htsus_rate":        cm_x.group(3),
                "duty_amount":       _money(cm_x.group(4)),
            }
            continue

        # D-1 / E-1 tariff subheading with gross weight + manifest qty
        #   E-1: "9903.03.01 878 0X 1154 10% 115.40"
        #   D-1: "9903.88.03 1035 0X 576 25% 144.00"
        td88 = TARIFF_D1_88_RE.match(line)
        if td88:
            entry["tariff_subheadings"].append({
                "htsus":           td88.group(1),
                "steel_weight_kg": _money(td88.group(2)),
                "rate":            td88.group(5),
                "amount":          _money(td88.group(6)),
            })
            continue

        # D-1 tariff subheading (8/9-digit): e.g. "9401.71.00 48NO Free 0.00"
        td = TARIFF_D1_RE.match(line)
        if td:
            qty_val = td.group(3)
            qty_num = _money(td.group(2))
            unit = td.group(3)
            rate_raw = td.group(4)
            rate = "Free" if rate_raw.upper().startswith("FREE") else rate_raw
            entry["tariff_subheadings"].append({
                "htsus":           td.group(1),
                "steel_weight_kg": qty_num if unit == "KG" else None,
                "rate":            rate,
                "amount":          _money(td.group(5)),
            })
            continue

        # D-1: skip "DOES NOT CONT..." / "ARTICLE OF CHINA..." / "NTE" annotation lines
        if re.match(r"^(DOES NOT CONT|ARTICLE OF|NTE\s)", line):
            continue

        # Tariff subheading (8/9-digit) — try C-1 first, then KG, then No-KG
        c1_m = TARIFF_V2_C1_RE.match(line)
        if c1_m:
            raw_rate = c1_m.group(2)
            rate = "Free" if raw_rate.upper().startswith("FREE") else raw_rate
            entry["tariff_subheadings"].append({
                "htsus":           c1_m.group(1),
                "steel_weight_kg": None,
                "rate":            rate,
                "amount":          _money(c1_m.group(3)),
            })
            continue

        kg_m = TARIFF_KG_RE.match(line)
        if kg_m:
            raw_rate = kg_m.group(3)
            rate = "Free" if raw_rate.upper().startswith("FREE") else raw_rate
            entry["tariff_subheadings"].append({
                "htsus":           kg_m.group(1),
                "steel_weight_kg": _money(kg_m.group(2)),
                "rate":            rate,
                "amount":          _money(kg_m.group(4)),
            })
            continue

        nokg_m = TARIFF_NOKG_RE.match(line)
        if nokg_m:
            raw_rate = nokg_m.group(3)
            rate = "Free" if raw_rate.upper().startswith("FREE") else raw_rate
            entry["tariff_subheadings"].append({
                "htsus":           nokg_m.group(1),
                "steel_weight_kg": None,
                "rate":            rate,
                "amount":          _money(nokg_m.group(4)),
            })
            continue

        if re.match(r"^SECTION\s+", line):
            continue

        # Standalone quantity line (e.g. "10 NO", "20 NO") — backfill net_quantity
        # when commodity qty was a placeholder "X" (MRSU format)
        qty_m = re.match(r"^([\d,]+)\s+(NO|PCS|PR|DOZ|KG|SET|EA)$", line)
        if qty_m and entry["commodity"] and entry["commodity"].get("net_quantity") is None:
            entry["commodity"]["net_quantity"]      = _money(qty_m.group(1))
            entry["commodity"]["net_quantity_unit"] = qty_m.group(2)
            continue

        # Commodity label rows (e.g. "OTH, WOOD, FURNITRE, PRTS, OTH").
        # Semicolons and ampersands occur too ("GYM/PLAYGRND EQUIP;EXER. CYCLE").
        if re.match(r"^[A-Z][A-Z0-9,/;&\s\-\.]+$", line) and len(line) > 4:
            commodity_desc_candidate = line
            continue

    # Skip phantom blocks: 3-digit line_no with fees but no commodity/tariff data
    # (e.g. "669 C $3,500" from CBP USE ONLY TOTALS section)
    if (entry["line_no"].isdigit() and int(entry["line_no"]) >= 100
        and not entry["commodity"] and not entry["tariff_subheadings"]):
        return None

    return entry

# ── Main ──────────────────────────────────────────────────────────────────────
def parse(pdf_path: str) -> dict:
    """Parse a CBP 7501 PDF and return structured data as a dict."""
    all_lines = _extract_text(pdf_path)
    text = "\n".join(all_lines)

    # Format detection: PRD ANY / IEEPA line items -> old format
    if _OLD_FORMAT_RE.search(text):
        # Old format (PRD ANY/IEEPA line items)
        _page0_words = []
        with pdfplumber.open(pdf_path) as _pdf:
            _page0_words = _pdf.pages[0].extract_words(x_tolerance=3, y_tolerance=3)
        header     = _parse_header(all_lines, _page0_words)
        blocks     = _split_blocks(all_lines)
        line_items = [p for b in blocks for p in [_parse_block(b)] if p]
    else:
        # New format (02/26 layout)
        header     = _parse_header_v2(all_lines, pdf_path)
        blocks     = _split_blocks_v2(all_lines)
        line_items = [p for b in blocks for p in [_parse_block_v2(b)] if p]

    conversion_fields = _line_conversion_fields(text, len(line_items))
    for item in line_items:
        item.update(conversion_fields)
        if (item["value_for_currency_conversion"] is None
            and item["conversion_currency"] == header["currency"]
            and item["exchange_rate"] in (None, 1.0)):
            item["value_for_currency_conversion"] = item["commodity"].get("entered_value")
        # manifest_note carries the line's Net Quantity in HTSUS Units exactly as
        # printed ("420.00 NO", "41.33 DOZ"), which is what downstream reads per
        # line. It is derived from the parsed quantity rather than from a loose
        # text match so v1 and v2 agree, and so a line whose quantity could not be
        # read reports None instead of borrowing another line's value.
        item["manifest_note"] = format_net_quantity(
            item["commodity"].get("net_quantity"),
            item["commodity"].get("net_quantity_unit"),
        )

    # 新判定规则：只要 grand_total_usd 不为 None 就算成功
    totals = header.get("totals", {})
    if totals.get("grand_total_usd") is None:
        raise ValueError("No grand_total_usd could be parsed from the PDF")

    return {
        "header":          header,
        "line_items":      line_items,
        "line_item_count": len(line_items),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input.pdf> [output.json]", file=sys.stderr)
        sys.exit(1)

    pdf_path = sys.argv[1]
    result   = parse(pdf_path)

    if len(sys.argv) >= 3:
        out_path = sys.argv[2]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"Saved → {out_path}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
