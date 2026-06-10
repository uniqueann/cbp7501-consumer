#!/usr/bin/env python3
"""
CBP Form 7501 Entry Summary Parser - v2
Generic version that works with any CBP 7501 regardless of importer/carrier.
Extends parse_7501.py without modifying it -- safe to import alongside it.

Usage:
    python parse_7501_v2.py <input.pdf> [output.json]
"""

import sys
import re
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parse_7501 import _money, COMMODITY_DESCRIPTIONS, parse as _parse_v1

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber not installed. Run: pip install pdfplumber", file=sys.stderr)
    sys.exit(1)


# ── Patterns (v2) ─────────────────────────────────────────────────────────────

ITEM_START_RE = re.compile(r"^(\d{3})\s+([A-Z].*)$")
_FEE_CODES = {499, 501}

# Main HTSUS commodity (10-digit): "9403.60.8093  150 NO  2,634  Free  0.00"
COMMODITY_V2_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{4})"
    r"\s+([\d,]+)\s+([A-Z]{2,3})"
    r"\s+([\d,]+)"
    r"\s+(Free|FREE|[\d.]+%)"
    r"\s+([\d.]+)$"
)

# Tariff subheading (8/9-digit): two formats
# With KG:    "9903.76.04  6765 KG  0  Free[n]  0.00"  -> code, weight_kg, qty/entered, rate, amount
# Without KG: "9903.03.01  X  0  10%  263.40"          -> code, qty_or_X, entered, rate, amount
TARIFF_KG_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{2,3})"
    r"\s+([\d,]+)\s+KG"         # gross weight (KG)
    r"\s+[\d,]+"                 # entered value (ignored)
    r"\s+((?:Free|FREE)\w*|[\d.]+%)"
    r"\s+([\d.]+)$"
)
TARIFF_NOKG_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{2,3})"
    r"\s+([X\d,]+)"              # qty or X
    r"\s+[\d,]+"                 # entered value (ignored)
    r"\s+((?:Free|FREE)\w*|[\d.]+%)"
    r"\s+([\d.]+)$"
)

# Fee line: "501 HARBOR MAINTENANCE FEE (HMF)  0.125%  3.29"
FEE_V2_RE = re.compile(r"^(499|501)\s+(.*?)\s+([\d.]+%)\s+([\d.]+)$")

_SKIP_LINES = {"N", "ENTRY SUMMARY CONTINUATION SHEET", "1.Filer Code/Entry Number"}
_SKIP_PREFIXES = (
    "Invoice Number", "Invoice Value", "Total Entered Value",
    "Other Fee Summary", "CBP Form 7501",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fix_year(d):
    """Expand 2-digit year: 03/18/26 -> 03/18/2026."""
    if not d:
        return d
    m = re.match(r"^(\d{2}/\d{2}/)(\d{2})$", d.strip())
    return (m.group(1) + "20" + m.group(2)) if m else d.strip()


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
                return "", "", "", ""

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
        return "", "", "", ""


# ── Header v2 ─────────────────────────────────────────────────────────────────

def _parse_header_v2(all_lines, pdf_path):
    text = "\n".join(all_lines)

    def _find(pattern, default="", flags=0):
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else default

    # ── Fields 1-7 ────────────────────────────────────────────────────────────
    hdr7 = re.search(
        r"([\w]+-\d{5,}-\d)\s+(\d{2})\s+(\d{3})\s+(\d)\s+(\d{4})"
        r"(?:\s+(\d{2}/\d{2}/\d{2,4}))?",
        text
    )
    filer_entry = hdr7.group(1) if hdr7 else ""
    entry_type  = hdr7.group(2) if hdr7 else ""
    surety      = hdr7.group(3) if hdr7 else ""
    bond_type   = hdr7.group(4) if hdr7 else ""
    port_code   = hdr7.group(5) if hdr7 else ""
    entry_date  = _fix_year(hdr7.group(6)) if hdr7 and hdr7.group(6) else ""

    summary_date = _fix_year(
        _find(r"(\d{2}/\d{2}/\d{2,4})\s+\d{3}\s+\d\s+\d{4}")
    )

    # ── Fields 8-11 ───────────────────────────────────────────────────────────
    cl = re.search(
        r"8\.Importing Carrier[^\n]*\n([^\n]+?)\s+(\d{2})\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{2,4})",
        text, re.S
    )
    importing_carrier = cl.group(1).strip() if cl else ""
    country_of_origin = cl.group(3)         if cl else ""
    import_date       = _fix_year(cl.group(4)) if cl else ""

    # ── Fields 12-15 ──────────────────────────────────────────────────────────
    bl = re.search(
        r"12\.B/L or AWB Number[^\n]*\n([^\n]+?)\s+(\S+)\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{2,4})",
        text, re.S
    )
    bl_number         = bl.group(1).strip() if bl else ""
    manufacturer_id   = bl.group(2)         if bl else ""
    exporting_country = bl.group(3)         if bl else ""
    export_date       = _fix_year(bl.group(4)) if bl else ""

    # ── Fields 19-20 ──────────────────────────────────────────────────────────
    ports = re.search(
        r"19\.Foreign Port[^\n]*20\.U\.S\. Port[^\n]*\n\s*(\d{4,5})\s+(\d{4})",
        text, re.S
    )
    foreign_port = ports.group(1) if ports else ""
    us_port      = ports.group(2) if ports else ""

    # ── Fields 26-28 ──────────────────────────────────────────────────────────
    nums = re.search(
        r"26\.Consignee Number\s+27\.Importer Number\s+28\.Reference Number\s*\n"
        r"\s*(\S+)\s+(\S+)\s+(\S+)",
        text
    )
    consignee_number = nums.group(1) if nums else ""
    importer_number  = nums.group(2) if nums else ""
    reference_number = nums.group(3) if nums else ""

    # ── Fields 29-30: two-column consignee / importer ─────────────────────────
    consignee_name, consignee_street, importer_name, importer_street = \
        _extract_two_col_fields(pdf_path)

    # City / State / Zip (two-column on one line)
    city_m = re.search(
        r"City\s+([\w\s]+?)\s+State\s+([A-Z]{2})\s+Zip\s+(\S+)"
        r"\s+City\s+([\w\s]+?)\s+State\s+([A-Z]{2})\s+Zip\s+(\S+)",
        text
    )
    consignee_city  = city_m.group(1).strip() if city_m else ""
    consignee_state = city_m.group(2)         if city_m else ""
    consignee_zip   = city_m.group(3)         if city_m else ""
    importer_city   = city_m.group(4).strip() if city_m else ""
    importer_state  = city_m.group(5)         if city_m else ""
    importer_zip    = city_m.group(6)         if city_m else ""

    # ── Manifest Quantity ─────────────────────────────────────────────────────
    manifest_qty = _find(r"\b(\d{3,}\s+CT(?:N|S)?)\b")

    # ── Totals ────────────────────────────────────────────────────────────────
    # Entered value: "$8,911.00  A.LIQ"
    entered_value = _money(_find(r"\$([\d,]+\.\d{2})\s+A\.LIQ"))
    if not entered_value:
        entered_value = _money(_find(r"Invoice Value USD\s+([\d,]+(?:\.\d+)?)"))

    # Duty (field 41): "Total Other Fees  891.10"
    duty = _money(_find(r"Total Other Fees\s+([\d,]+(?:\.\d{2})?)"))

    # Tax (field 42): "$11.14  0.00"  -- 0.00 is Tax
    tax_m = re.search(r"REASON CODE[^\n]*\n\$[\d.]+\s+([\d.]+)", text)
    tax = _money(tax_m.group(1)) if tax_m else None

    # Other fees (field 43): standalone number on line after "43.Other" label
    other_fees_m = re.search(r"43\.Other\s*\n([^\n]+?)\n([\d.]+)", text)
    if other_fees_m:
        other_fees = _money(other_fees_m.group(2))
    else:
        other_fees = _money(_find(r"43\.Other\s*\n\s*([\d.]+)"))

    # Grand total (field 44): number at end of line containing "44.Total"
    grand_total_m = re.search(r"44\.Total\s*\n[^\n]*?([\d,]+\.\d{2})\s*$", text, re.M)
    if not grand_total_m:
        # It appears on the NEXT line after "44.Total" label
        grand_total_m = re.search(
            r"E\.Ascertained Total\s+44\.Total\s*\n[^\n]*([\d,]+\.\d{2})", text
        )
    grand_total = _money(grand_total_m.group(1)) if grand_total_m else None

    # HMF: "Harbor Maintenance Fee  $11.14"
    hmf = _money(_find(r"Harbor Maintenance Fee\s+\$([\d.]+)"))
    # MPF: "Merchandise Processing Fee  $xx.xx"
    mpf = _money(_find(r"Merchandise Processing Fee\s+\$([\d.]+)"))

    return {
        "form":                    "CBP 7501",
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
        "location_of_goods":       "",
        "reference_number":        reference_number,
        "manifest_quantity":       manifest_qty,
        "consignee": {
            "name":   consignee_name,
            "street": consignee_street,
            "city":   consignee_city,
            "state":  consignee_state,
            "zip":    consignee_zip,
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
        if "MASTER BILL" in line or "I.T. DATE" in line:
            start_idx = i + 1
            break

    blocks = []
    i = start_idx
    while i < len(all_lines):
        line = all_lines[i].strip()
        m = ITEM_START_RE.match(line)
        if m and int(m.group(1)) not in _FEE_CODES:
            j = i + 1
            while j < len(all_lines):
                nxt = all_lines[j].strip()
                nm = ITEM_START_RE.match(nxt)
                if nm and int(nm.group(1)) not in _FEE_CODES:
                    break
                j += 1
            blocks.append(all_lines[i:j])
            i = j
        else:
            i += 1
    return blocks


# ── Block parser v2 ───────────────────────────────────────────────────────────

def _parse_block_v2(lines):
    first = lines[0].strip()
    m = ITEM_START_RE.match(first)
    if not m:
        return None

    desc = re.sub(r"\s+[a-zA-Z]$", "", m.group(2)).strip()
    entry = {
        "line_no":            m.group(1),
        "line_type":          "STANDARD",
        "manifest_note":      None,
        "exclusion_note":     desc,
        "tariff_subheadings": [],
        "commodity":          {},
        "fees":               [],
    }
    commodity_desc_candidate = ""

    for raw in lines[1:]:
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

        # Main HTSUS commodity (10-digit)
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

        # Tariff subheading (8/9-digit)
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

        # Commodity label rows (e.g. "OTH, WOOD, FURNITRE, PRTS, OTH")
        if re.match(r"^[A-Z][A-Z0-9,/\s\-\.]+$", line) and len(line) > 4:
            commodity_desc_candidate = line
            continue

    return entry


# ── Public API ────────────────────────────────────────────────────────────────

# Old-format marker: line items start with "PRD ANY" or "IEEPA"
_OLD_FORMAT_RE = re.compile(r"^\d{3}\s+(PRD ANY|IEEPA)", re.MULTILINE)


def _is_old_format(all_lines):
    """Return True if this PDF uses the original parse_7501.py format."""
    text = "\n".join(all_lines)
    return bool(_OLD_FORMAT_RE.search(text))


def parse_v2(pdf_path):
    """
    Parse a CBP 7501 PDF.
    - Old format (PRD ANY / IEEPA line items): delegates to parse_7501.parse()
    - New format (plain description line items): uses v2 generic extractor
    """
    all_lines = _extract_text_lines(pdf_path)

    if _is_old_format(all_lines):
        return _parse_v1(pdf_path)

    header     = _parse_header_v2(all_lines, pdf_path)
    blocks     = _split_blocks_v2(all_lines)
    line_items = [p for b in blocks for p in [_parse_block_v2(b)] if p]
    return {
        "header":          header,
        "line_items":      line_items,
        "line_item_count": len(line_items),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: {} <input.pdf> [output.json]".format(sys.argv[0]), file=sys.stderr)
        sys.exit(1)

    result = parse_v2(sys.argv[1])

    if len(sys.argv) >= 3:
        out_path = sys.argv[2]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print("Saved -> {}".format(out_path))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
