#!/usr/bin/env python3
"""Parse CBSA BSF864 (Canada Commercial Accounting Declaration) PDFs.

Output schema (``schema_version`` 2.0) is native to the BSF864 form rather than
being shaped like CBP Form 7501:

    {
      "schema_version": "2.0",
      "document":   {form, pages, printed_at, notes},
      "header":     {type_code, office_no, ... , importer{...}},
      "invoices":   [{page, invoice_no, invoice_value, ...}],   # page-level 36-43
      "totals":     {currency, value_for_duty, duty, gst, ...},
      "line_items": [{line_no, hs_code, description, ...}],
      "warnings":   [{code, ...}]
    }

Amounts are returned exactly as printed, in CAD. ``header.currency`` /
``totals.currency`` are always "CAD"; ``invoice_value`` and
``value_for_currency_conversion`` keep their own invoice currency.

Field-number mapping onto the new key names is documented next to each
``_field(...)`` call below. A couple of 7501-era keys are intentionally gone:
there is no declaration-level "value for tax" field on BSF864, so
``totals.value_for_tax`` is derived from the line items and flagged with
``value_for_tax_derived``.

Usage:
    python parse_cad.py <input.pdf> [output.json]
"""

from __future__ import annotations

import json
import re
import sys
from decimal import Decimal
from itertools import groupby
from operator import itemgetter

import pdfplumber
from pdfplumber.utils import cluster_objects

SCHEMA_VERSION = "2.0"

# CbpFormType handled by this parser. consumer.py reports it in the output
# envelope next to ID / FileNumber, and uses it as the default when an inbound
# message omits the field.
CBP_FORM_TYPE = 20

_FORM_MARKERS = (
    "Commercial Accounting Declaration",
    "Déclaration en détail commerciale",
)
# Some batches are printed without the BSF864 title block (only a "Draft Version"
# or a bare form revision number in its place). Recognize those by form structure
# instead: the numbered field labels below are unique to this declaration and
# never appear on CBP 7501, so 3+ hits is a safe positive.
_STRUCTURAL_MARKERS = (
    "Total Value for Duty",
    "CAD Transaction No.",
    "Classification No.",
    "Importer BN",
)
_STRUCTURAL_MIN_HITS = 3
_MONEY_RE = r"-?\d[\d,]*(?:\.\d+)?"

# Line-level tax fields: (form field number, printed label, output key).
_LINE_TAX_FIELDS = (
    (82, "Customs Duty", "duty"),
    (83, "Excise Tax", "excise_tax"),
    (84, "Excise Duty", "excise_duty"),
    (85, "Surtax", "surtax"),
    (86, "Anti-Dumping", "anti_dumping"),
    (87, "Safeguard", "safeguard"),
    (88, "Countervailing", "countervailing"),
    (89, "Value for Tax", "value_for_tax"),
    (90, "GST", "gst"),
    (91, "PST/HST Amount", "pst_hst"),
    (92, "Provincial Alcohol Tax", "provincial_alcohol_tax"),
    (93, "Provincial Tobacco Amount", "provincial_tobacco_tax"),
    (95, "Provincial Cannabis Excise Duty", "pst_cannabis"),
)

# Declaration-level totals: (form field number, printed label, output key).
_TOTAL_FIELDS = (
    (113, "Total Value for Duty", "value_for_duty"),
    (120, "Total Customs Duties", "duty"),
    (121, "Total Excise Duties", "excise_duty"),
    (122, "Total Excise Taxes", "excise_tax"),
    (126, "Total Surtaxes", "surtax"),
    (124, "Total Anti-Dumping", "anti_dumping"),
    (125, "Total Countervailing", "countervailing"),
    (127, "Total Safeguards", "safeguard"),
    (123, "Total GST", "gst"),
    (114, "Total PST/HST", "pst_hst"),
    (116, "Total Prov Alcohol Tax Amount", "provincial_alcohol_tax"),
    (117, "Total Prov Tobacco Amount", "provincial_tobacco_tax"),
    (115, "Total PST Cannabis Amount", "provincial_cannabis_duty"),
    (118, "Declaration Total Relieved", "relieved"),
    (128, "Total Interest", "interest"),
    (119, "Total Amount", "total_amount"),
    (129, "Total Duties & Taxes with Interest", "total_with_interest"),
)

# Header fields: (form field number, printed labels, output key).
# The label tuple lists the print variants seen across CBSA output batches.
_HEADER_FIELDS: tuple[tuple[int, str | tuple[str, ...], str], ...] = (
    (1, "Type Code", "type_code"),
    (2, ("W Subtype", "Ws S.typ", "Ws S. typ", "S.typ"), "sub_type"),
    (3, ("Submission Date", "Accounting Date"), "submission_date"),
    (4, "CAD Transaction No.", "transaction_no"),
    (5, "Office No.", "office_no"),
    (6, "Mode of transport", "mode_of_transport"),
    (7, "Release Date", "release_date"),
    (8, "Gross Weight/kg", "gross_weight_kg"),
    (9, "Carrier Code at Importation", "carrier_code"),
    (15, "Cargo Control No.", "cargo_control_no"),
    (18, "Accepted Date", "accepted_date"),
    (21, "Port of Unlading", "port_of_unlading"),
)

_POSTAL_RE = re.compile(r"[A-Z]\d[A-Z]\s?\d[A-Z]\d")
_BN_RE = re.compile(r"\b\d{9}RM\d{4}\b")
_PRINTED_RE = re.compile(r"Printed\s+(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?")


# ── Low-level cell readers ────────────────────────────────────────────────────
def _field(table: list[list[str | None]], number: int, label: str | tuple[str, ...],
           start: int = 0, end: int | None = None) -> str:
    """Read the value printed inside a numbered form cell or directly below it.

    ``label`` may be a tuple of alternatives: CBSA print variants use different
    headings for the same field number (e.g. field 2 is "2 W Subtype" on one
    layout and "2 Ws S.typ" on another, field 3 is "Submission Date" or
    "Accounting Date").
    """
    labels = (label,) if isinstance(label, str) else label
    patterns = [
        (r"Type(?: Code)?" if number == 1 and alt == "Type Code" else re.escape(alt))
        for alt in labels
    ]
    pattern = "(?:" + "|".join(patterns) + ")"

    for row_index in range(start, len(table) if end is None else end):
        for col, cell in enumerate(table[row_index]):
            if not cell or not re.match(rf"^\s*{number}\s*{pattern}", cell, re.I):
                continue
            lines = cell.splitlines()
            value_start = 1 if (number == 1 and len(lines) == 2
                                and re.fullmatch(r"[A-Z]{2}", lines[1].strip())) else 2
            value = " ".join(line.strip() for line in lines[value_start:] if line.strip())
            if value:
                return value
            if row_index + 1 < len(table):
                for next_col in (col, col + 1):
                    if next_col < len(table[row_index + 1]):
                        below = (table[row_index + 1][next_col] or "").strip()
                        if below and not re.match(r"^\d{1,3}\s*[A-Za-z]", below):
                            return below
            return ""
    return ""


def _cell(table: list[list[str | None]], number: int) -> str:
    """Return the first cell whose text starts with the given form field number.

    Values are printed either as ``113 Total ...`` or ``113Total ...`` depending
    on the printer, so the separator after the number is optional.
    """
    for row in table:
        for cell in row:
            if cell and re.match(rf"^\s*{number}\s*[A-Za-z]", cell):
                return cell
    return ""


def _amount(text: str, number: int, label: str) -> Decimal | None:
    """Read a declaration-level amount, tolerating value-before-label layouts."""
    match = re.search(
        rf"(?m)^\s*{number}\s+{re.escape(label)}\s+-[^\n]*?\s+({_MONEY_RE})(?=\s|$)",
        text,
        re.I,
    )
    if not match:
        match = re.search(
            rf"(?m)^[ \t]*({_MONEY_RE})[ \t]*\n[ \t]*{number}[ \t]+{re.escape(label)}[ \t]+-[^\n]*",
            text,
            re.I,
        )
    return Decimal(match.group(1).replace(",", "")) if match else None


_NUMBER_RE = re.compile(r"\s*-?[\d,]+(?:\.\d*)?\s*")


def _number(value: str) -> Decimal | None:
    """Parse a printed number.

    A trailing decimal point with no fractional digits ("19674.") is a truncated
    print, not an error, so the fractional part is optional — dropping such a
    value would silently lose real data (e.g. CSNU's gross weight).
    """
    text = (value or "").strip()
    if not text or not _NUMBER_RE.fullmatch(text):
        return None
    text = text.replace(",", "").rstrip(".")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    return Decimal(text)


def _money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


# ── Page text deduplication ───────────────────────────────────────────────────
def _fast_dedupe_chars(chars: list, tolerance: int = 1,
                       extra_attrs: tuple[str, ...] = ("fontname", "size")) -> list:
    """Drop overlapping duplicate chars, equivalent to Page.dedupe_chars().

    pdfplumber's version finishes with ``sorted(deduped, key=chars.index)``,
    which is O(n^2) and dominates runtime on the 9-page BSF864 files (~17s of
    19s). Tracking the original index up front makes it O(n log n) while
    keeping the exact same survivor selection.
    """
    if not chars:
        return []

    key = itemgetter("upright", "text", *extra_attrs)
    pos_key = itemgetter("doctop", "x0")

    indexed = list(enumerate(chars))
    indexed.sort(key=lambda pair: key(pair[1]))

    survivors: list[int] = []
    for _, group in groupby(indexed, key=lambda pair: key(pair[1])):
        group_list = list(group)
        for y_cluster in cluster_objects(group_list, lambda p: p[1]["doctop"], tolerance):
            for x_cluster in cluster_objects(y_cluster, lambda p: p[1]["x0"], tolerance):
                survivors.append(min(x_cluster, key=lambda p: pos_key(p[1]))[0])

    survivors.sort()
    return [chars[index] for index in survivors]


def _dedupe_page(page):
    """Return a copy of *page* with duplicate overlay chars removed."""
    kept = {id(char) for char in _fast_dedupe_chars(page.chars)}
    return page.filter(
        lambda obj: obj.get("object_type") != "char" or id(obj) in kept
    )


def _looks_like_bsf864(texts: list[str]) -> bool:
    """True when the document is a CBSA BSF864, with or without its title block.

    The title block ("Commercial Accounting Declaration" / "Déclaration en détail
    commerciale") is missing on some batches, which instead start with a bare
    revision marker such as "Draft Version 3122" or "3121". Those still carry the
    full set of numbered form fields, so fall back to structure.
    """
    if any(marker in texts[0] for marker in _FORM_MARKERS):
        return True
    hits = sum(1 for marker in _STRUCTURAL_MARKERS if marker in texts[0])
    return hits >= _STRUCTURAL_MIN_HITS


def _extract_tables(page, text: str) -> list[list[list[str | None]]]:
    if text.count(_FORM_MARKERS[0]) < 2:
        return page.extract_tables()
    tables = []
    for table in page.find_tables():
        rows = table.extract()
        starts = [index for index, row in enumerate(rows) if any(
            cell and re.match(r"^\s*58\s*Classification No\.", cell) for cell in row
        )]
        if len(starts) > 1:
            reference = starts[-1]
            line_col = next(col for col, cell in enumerate(rows[reference])
                            if cell and re.match(r"^\s*56\s*C\. Ln N\.", cell))
            line_box = table.rows[reference].cells[line_col]
            value_size = max(char["size"] for char in page.chars
                             if line_box[0] <= (char["x0"] + char["x1"]) / 2 <= line_box[2]
                             and line_box[1] <= (char["top"] + char["bottom"]) / 2 <= line_box[3])
            values = page.filter(lambda obj: obj.get("object_type") != "char"
                                 or abs(obj["size"] - value_size) < 0.01)
            template = rows[reference:reference + 5]
            for start in starts:
                shift = table.rows[start].cells[line_col][1] - line_box[1]
                for offset, row in enumerate(template):
                    cleaned = []
                    for cell, box in zip(row, table.rows[reference + offset].cells):
                        if not cell or box is None:
                            continue
                        heading = cell.splitlines()[0]
                        match = re.match(r"^\s*(\d+)\s+", heading)
                        if not match or not 56 <= int(match.group(1)) <= 100:
                            continue
                        value = values.crop((box[0], box[1] + shift,
                                             box[2], box[3] + shift)).extract_text() or ""
                        cleaned.append(f"{heading}\n\n{value.strip()}")
                    rows[start + offset] = cleaned
        tables.append(rows)
    return tables


# ── Header / importer ─────────────────────────────────────────────────────────
_PHONE_RE = re.compile(r"^(?:001\s*)?\d{10}$")


def _parse_address_block(lines: list[str]) -> dict:
    """Split an importer/vendor address block into name/street/city/state/zip/country.

    Handles both layouts seen in practice:
      * multi-line, last line "RICHMOND BC V6C 1S4" then "Canada"
      * one flattened line "… PO BOX41 POBOX41 VANCOUVER BC V6C 1S4 CANADA 001 236…"
    """
    result = {"name": "", "street": "", "city": "", "state": "", "zip": "", "country": ""}
    lines = [line.strip() for line in lines if line and line.strip()]
    if not lines:
        return result

    # A flattened block arrives as a single line; re-split it on the postal code
    # and phone number so the generic logic below can run unchanged.
    if len(lines) == 1:
        lines = _split_flattened_address(lines[0])

    while len(lines) > 1 and _PHONE_RE.match(lines[-1]):
        lines.pop()

    country = ""
    if len(lines) > 1 and lines[-1].upper() in ("CANADA", "CA"):
        country = "CA"
        lines.pop()

    result["name"] = lines[0] if lines else ""
    if len(lines) < 2:
        result["country"] = country
        return result

    # Walk from the last line backwards to find the "CITY PROV POSTAL" line.
    city_index = None
    for index in range(len(lines) - 1, 0, -1):
        postal = _POSTAL_RE.search(lines[index])
        if postal:
            result["zip"] = postal.group(0).replace(" ", "")
            head = lines[index][:postal.start()].strip(" ,")
            prov = re.search(r"(?:^|[\s,])([A-Z]{2})$", head)
            if prov:
                result["state"] = prov.group(1)
                head = head[:prov.start()].strip(" ,")
            result["city"] = head
            city_index = index
            break

    result["street"] = " ".join(lines[1:city_index]) if city_index is not None else " ".join(lines[1:])
    if not country and result["zip"]:
        # A Canadian postal code is itself proof of country.
        country = "CA"
    result["country"] = country
    return result


def _split_flattened_address(text: str) -> list[str]:
    """Best-effort re-split of a single-line address block.

    Two shapes occur:
      * space-separated with a Canadian postal code tail
        ("… POBOX41 VANCOUVER BC V6C 1S4 CANADA 001 236…")
      * comma-separated foreign address without a postal code
        ("JUNSHENG INT'L GROUP LIMITED., , FLAT/RM 185 … , HK, Phone: 852…")
    """
    # A trailing "Phone: …" / "Tel: …" segment is never part of the address.
    text = re.sub(r"[\s,]*(?:Phone|Tel|Tél)\s*:.*$", "", text, flags=re.I).strip(" ,")

    match = re.search(
        r"(?P<city>[A-Za-z][A-Za-z .'\-]*?)[\s,]+"
        r"(?P<prov>[A-Z]{2})[\s,]+"
        r"(?P<postal>[A-Z]\d[A-Z]\s?\d[A-Z]\d)",
        text,
    )
    if match:
        head = text[:match.start()].strip(" ,")
        tail = text[match.end():].strip(" ,")
        tail = re.sub(r"^(?:CANADA|CA)\b", "", tail, flags=re.I).strip(" ,")
        tail = re.sub(r"\b(?:001\s*)?\d{10}\b.*$", "", tail).strip(" ,")
        city_line = f"{match.group('city').strip()} {match.group('prov')} {match.group('postal')}"
        if tail:
            city_line += " " + tail
        return [head, city_line]

    # Comma-separated foreign address: keep the name, then one street line.
    parts = [part.strip() for part in text.split(",")]
    parts = [part for part in parts if part]
    if len(parts) > 1:
        return [parts[0], ", ".join(parts[1:])]
    return [text]


def _parse_importer(table: list[list[str | None]]) -> dict:
    address = ""
    for row_index, row in enumerate(table[:-1]):
        if any(cell and re.match(r"^\s*11\s*Importer BN\b", cell)
               and not re.search(r"\b12\s+Importer name", cell) for cell in row):
            address = (table[row_index + 1][0] or "").strip()
            break
    if not address:
        for row in table:
            for cell in row:
                if (cell and (re.search(r"\b\d{9}RM\d{4}\b", cell)
                              or re.match(r"^\s*11\s+Importer BN.*\b12\s+Importer name", cell))
                    and re.search(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b", cell)):
                    lines = cell.splitlines()[2:]
                    if lines and re.search(r"l’importateur|téléphone", lines[0]):
                        lines = lines[1:]
                    address = "\n".join(lines).strip()
                    break
            if address:
                break
    importer = _parse_address_block(address.splitlines())
    importer.pop("state", None)   # importer_of_record has no state in this schema
    return importer


def _parse_header(header_table: list[list[str | None]], warnings: list[dict]) -> dict:
    header: dict = {}
    empty: list[str] = []
    for number, label, key in _HEADER_FIELDS:
        raw = _field(header_table, number, label)
        if key == "gross_weight_kg":
            value = _money(_number(raw))
        else:
            value = raw or None
        header[key] = value
        if value is None:
            empty.append(key)

    # Field 11 carries the BN when the form prints one; many declarations omit it.
    # Search every cell for the BN pattern rather than relying on the heading,
    # because the heading glyphs are sometimes printed twice and overlap.
    header["importer_bn"] = None
    for row in header_table:
        for cell in row:
            if not cell:
                continue
            bn_match = _BN_RE.search(cell)
            if bn_match:
                header["importer_bn"] = bn_match.group(0)
                break
        if header["importer_bn"]:
            break
    if header["importer_bn"] is None:
        empty.append("importer_bn")

    header["importer"] = _parse_importer(header_table)

    # The "Date Signature" line is a signature area, not a text field.
    header["signed_date"] = None
    empty.append("signed_date")

    # Field 3 is the single declaration date on BSF864. Older batches print it as
    # "Accounting Date", newer ones as "Submission Date"; both are the same cell,
    # so expose the value under both names instead of leaving one silently empty.
    header["accounting_date"] = header["submission_date"]

    if empty:
        warnings.append({"code": "EMPTY_HEADER_FIELDS", "fields": empty})
    return header


# ── Invoice block (fields 36-43, one per page) ────────────────────────────────
def _field_multiline(table: list[list[str | None]], number: int, label: str,
                     start: int = 0, end: int | None = None) -> str:
    """Read a numbered cell that may hold its value on following rows.

    Unlike ``_field``, all lines below the bilingual heading are kept (Vendor /
    Purchaser blocks print a multi-line address), and a following standalone row
    is used when the cell itself carries no value.
    """
    label_pattern = re.escape(label)
    for row_index in range(start, len(table) if end is None else end):
        for col, cell in enumerate(table[row_index]):
            if not cell or not re.match(rf"^\s*{number}\s*{label_pattern}", cell, re.I):
                continue
            lines = cell.splitlines()
            # Drop the English and French heading lines.
            body = [line.strip() for line in lines[2:] if line.strip()]
            if body:
                return "\n".join(body)
            if row_index + 1 < len(table):
                for next_col in (col, col + 1):
                    if next_col < len(table[row_index + 1]):
                        below = (table[row_index + 1][next_col] or "").strip()
                        if below and not re.match(r"^\d{1,3}\s*[A-Za-z]", below):
                            return below
            return ""
    return ""


def _parse_notes(header_table: list[list[str | None]]) -> str | None:
    """Read field 35 (Notes). The cell holds only the bilingual label when empty."""
    for row in header_table:
        for cell in row:
            if not cell or not re.match(r"^\s*35\s*Notes", cell, re.I):
                continue
            lines = [line.strip() for line in cell.splitlines() if line.strip()]
            body = lines[1:]   # line 0 is "35 Notes - Remarques"
            return " ".join(body) if body else None
    return None


def _parse_invoice(table: list[list[str | None]], page_no: int) -> dict | None:
    """Read the page-level fields 36-43 block; None when the page has none."""
    has_block = any(
        cell and re.match(r"^\s*(?:36|38|39)\s*[A-Za-z]", cell) for row in table for cell in row
    )
    if not has_block:
        return None

    vendor_raw = _field_multiline(table, 36, "Vendor - Name, Address and Telephone No.")
    vendor = _parse_address_block(vendor_raw.splitlines()) if vendor_raw else None
    invoice = {
        "page": page_no,
        "invoice_no": _field(table, 38, "Invoice No.") or None,
        "invoice_value": _money(_number(_field(table, 39, "Invoice Value"))),
        "invoice_currency": _field(table, 40, "Invoice Currency Code") or None,
        "purchase_order_no": _field(table, 41, "Purchase Order No.") or None,
        "freight": _money(_number(_field(table, 42, "Freight Charges"))),
        "us_port_of_exit": _field(table, 43, "U.S. Port of Exit") or None,
        "vendor": vendor,
    }
    if invoice["vendor"]:
        invoice["vendor"].pop("state", None)
    return invoice


# ── Line items ────────────────────────────────────────────────────────────────
def _format_hs_code(raw: str) -> tuple[str, str | None]:
    """Return (10-digit dotted hs_code, 6-digit parent) from a printed code."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) < 10:
        return raw, None
    digits = digits[:10]
    return f"{digits[:4]}.{digits[4:6]}.{digits[6:]}", f"{digits[:4]}.{digits[4:6]}"


def _parse_line_item(table: list[list[str | None]], start: int, end: int,
                     page_no: int, invoice_ref: int | None) -> dict:
    field = lambda number, label: _field(table, number, label, start, end)

    line_no_raw = re.sub(r"\s+", "", field(56, "C. Ln N."))
    classification = re.sub(r"[\s.]", "", field(58, "Classification No."))
    if not line_no_raw.isdigit() or not re.fullmatch(r"\d{10}", classification):
        raise ValueError("Could not parse CAD line number or classification")

    hs_code, hs_code_6 = _format_hs_code(classification)

    item = {
        "line_no": int(line_no_raw),
        "line_no_raw": line_no_raw,
        "page": page_no,
        "invoice_ref": invoice_ref,
        "hs_code": hs_code,
        "hs_code_6": hs_code_6,
        "description": field(60, "Narrative Description") or field(59, "Classification Description"),
        "quantity": _money(_number(field(61, "Quantity"))),
        "unit_of_measure": field(62, "Unit of Measure") or None,
        "country_of_origin": field(65, "Country of Origin") or None,
        "place_of_export": field(67, "Place of Export") or None,
        "direct_shipment_date": field(69, "Direct Shipment Date") or None,
        "tariff_treatment": field(70, "Tariff Treatment") or None,
        "value_for_currency_conversion": _money(_number(field(75, "Value for Currency Conversion"))),
        "conversion_currency": field(76, "Currency") or None,
        "exchange_rate": _money(_number(field(77, "Exchange Rate"))),
        "value_for_duty": _money(_number(field(78, "Value for Duty"))),
    }
    for number, label, key in _LINE_TAX_FIELDS:
        item[key] = _money(_number(field(number, label)))
    item["line_total"] = _money(_number(field(100, "Line Total Duties & Taxes")))
    return item


# ── Totals ────────────────────────────────────────────────────────────────────
def _parse_totals(header_table: list[list[str | None]], text: str,
                  line_items: list[dict], warnings: list[dict]) -> dict:
    # Every declaration total lives in the single cell that starts at field 113.
    totals_cell = _cell(header_table, 113)
    totals: dict = {"currency": "CAD"}

    for number, label, key in _TOTAL_FIELDS:
        value = None
        if totals_cell:
            match = re.search(
                rf"(?m)^\s*{number}\s+[^\n]*?\s+({_MONEY_RE})\s*$", totals_cell
            )
            value = Decimal(match.group(1).replace(",", "")) if match else None
        if value is None:
            value = _amount(text, number, label.upper())
        totals[key] = _money(value)
        if totals[key] is None:
            warnings.append({"code": "MISSING_DECLARATION_TOTAL",
                             "field": f"totals.{key}", "form_field": number})

    # Field 130 prints its amount above the label.
    grand_total = _amount(text, 130, "TOTAL DUTIES & TAXES")
    totals["total_duties_and_taxes"] = _money(grand_total)
    if grand_total is None:
        warnings.append({"code": "MISSING_DECLARATION_TOTAL",
                         "field": "totals.total_duties_and_taxes", "form_field": 130})

    # BSF864 has no declaration-level "value for tax"; derive it from the lines.
    line_tax_values = [i["value_for_tax"] for i in line_items if i.get("value_for_tax") is not None]
    totals["value_for_tax"] = round(sum(line_tax_values), 2) if line_tax_values else None
    totals["value_for_tax_derived"] = True
    if line_tax_values:
        warnings.append({"code": "DERIVED_VALUE", "field": "totals.value_for_tax",
                         "method": "sum(line_items[].value_for_tax)"})
    return totals


def _check_totals(totals: dict, line_items: list[dict], warnings: list[dict]) -> None:
    """Cross-check declaration totals against the sum of the line items."""
    pairs = (
        ("value_for_duty", "value_for_duty"),
        ("duty", "duty"),
        ("gst", "gst"),
        ("total_duties_and_taxes", "line_total"),
    )
    for total_key, line_key in pairs:
        declared = totals.get(total_key)
        values = [i.get(line_key) for i in line_items if i.get(line_key) is not None]
        if declared is None or not values:
            continue
        summed = round(sum(values), 2)
        if abs(summed - declared) > 0.02:
            warnings.append({"code": "TOTAL_MISMATCH", "field": f"totals.{total_key}",
                             "sum_of_lines": summed, "declared": declared})


# ── Entry point ───────────────────────────────────────────────────────────────
def parse(pdf_path: str) -> dict:
    """Parse a CBSA BSF864 PDF and return the schema_version 2.0 result dict."""
    warnings: list[dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            raise ValueError("Empty CAD PDF")
        pages = [_dedupe_page(page) for page in pdf.pages]
        texts = [page.extract_text() or "" for page in pages]
        if not _looks_like_bsf864(texts):
            raise ValueError("Not a recognized Canada CAD (BSF864) PDF")
        page_tables = [
            (page_no, _extract_tables(page, text))
            for page_no, (page, text) in enumerate(zip(pages, texts), start=1)
        ]
        page_count = len(pdf.pages)

    all_tables = [table for _, tables in page_tables for table in tables]
    header_table = next((table for table in all_tables if any(
        cell and re.match(r"^\s*1\s+Type(?: Code)?\b", cell, re.I)
        for row in table for cell in row
    )), None)
    if header_table is None:
        raise ValueError("Could not parse CAD header")

    # ── document ─────────────────────────────────────────────────────────────
    printed = _PRINTED_RE.search(texts[0])
    document = {
        "form": "CBSA BSF864",
        "pages": page_count,
        "printed_at": (f"{printed.group(1)} {printed.group(2)}".strip()
                       if printed else None),
        "notes": _parse_notes(header_table),
    }

    # ── header ───────────────────────────────────────────────────────────────
    header = _parse_header(header_table, warnings)

    # ── invoices (page-level, fields 36-43) ──────────────────────────────────
    invoices: list[dict] = []
    for page_no, tables in page_tables:
        for table in tables:
            invoice = _parse_invoice(table, page_no)
            if invoice is None:
                continue
            if any(value not in (None, "") for key, value in invoice.items()
                   if key not in ("page", "vendor")):
                invoices.append(invoice)
            break
    for invoice in invoices:
        if invoice["invoice_value"] is None:
            warnings.append({"code": "EMPTY_INVOICE_VALUE", "page": invoice["page"]})

    # ── line items (fields 56-100) ───────────────────────────────────────────
    # Field 36-43 is a page-level block; continuation pages often repeat only the
    # line items, so a line inherits the invoice of the nearest preceding page.
    invoice_by_page = {invoice["page"]: index for index, invoice in enumerate(invoices)}
    line_items: list[dict] = []
    empty_quantity: list[int] = []
    current_invoice_ref: int | None = None
    for page_no, tables in page_tables:
        if page_no in invoice_by_page:
            current_invoice_ref = invoice_by_page[page_no]
        for table in tables:
            starts = [index for index, row in enumerate(table) if any(
                cell and re.match(r"^\s*58\s*Classification No\.", cell) for cell in row
            )]
            for index, start in enumerate(starts):
                end = starts[index + 1] if index + 1 < len(starts) else len(table)
                item = _parse_line_item(table, start, end, page_no, current_invoice_ref)
                if item["quantity"] is None:
                    empty_quantity.append(item["line_no"])
                line_items.append(item)

    if not line_items:
        raise ValueError("No CAD line items could be parsed from the PDF")
    if empty_quantity:
        warnings.append({"code": "EMPTY_QUANTITY", "lines": empty_quantity})

    # ── totals ───────────────────────────────────────────────────────────────
    totals = _parse_totals(header_table, texts[0], line_items, warnings)
    if totals["total_duties_and_taxes"] is None:
        raise ValueError("Could not parse CAD declaration totals")
    _check_totals(totals, line_items, warnings)

    return {
        "schema_version": SCHEMA_VERSION,
        "document": document,
        "header": header,
        "invoices": invoices,
        "totals": totals,
        "line_items": line_items,
        "line_item_count": len(line_items),
        "warnings": warnings,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input.pdf> [output.json]", file=sys.stderr)
        sys.exit(1)

    result = parse(sys.argv[1])
    if len(sys.argv) >= 3:
        with open(sys.argv[2], "w", encoding="utf-8") as file:
            json.dump(result, file, indent=2, ensure_ascii=False)
        print(f"Saved → {sys.argv[2]}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
