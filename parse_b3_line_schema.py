#!/usr/bin/env python3
"""Map a BSF864 line-item field dump onto the ``parse_cad.py`` result schema.

The OCR fallback (``parse_b3_line_ocr.py``) reads a raster band of fields 56-100
and returns raw strings. This module reshapes those into exactly the dict
``parse_cad.parse()`` returns -- same keys, same order, same number formatting --
so a screenshot produces output the consumer already knows how to handle.

Kept dependency-free on purpose: ``parse_cad`` itself needs pdfplumber, which the
OCR virtualenv does not have, so the three scalar converters are re-implemented
here and pinned by ``tests/test_b3_ocr_schema.py`` against the originals.
"""
from __future__ import annotations

import re
from decimal import Decimal

SCHEMA_VERSION = "2.0"

# Key orders copied from parse_cad so json.dump output lines up byte for byte.
HEADER_KEYS = (
    "type_code", "sub_type", "submission_date", "transaction_no", "office_no",
    "mode_of_transport", "release_date", "gross_weight_kg", "carrier_code",
    "cargo_control_no", "accepted_date", "port_of_unlading",
)
IMPORTER_KEYS = ("name", "street", "city", "zip", "country")
TOTALS_KEYS = (
    "value_for_duty", "duty", "excise_duty", "excise_tax", "surtax",
    "anti_dumping", "countervailing", "safeguard", "gst", "pst_hst",
    "provincial_alcohol_tax", "provincial_tobacco_tax", "provincial_cannabis_duty",
    "relieved", "interest", "total_amount", "total_with_interest",
    "total_duties_and_taxes", "value_for_tax", "value_for_tax_derived",
)
# Declaration-level totals with their form field numbers, for the warnings.
TOTAL_FORM_FIELDS = {
    "value_for_duty": 113, "duty": 120, "excise_duty": 121, "excise_tax": 122,
    "surtax": 126, "anti_dumping": 124, "countervailing": 125,
    "safeguard": 127, "gst": 123, "pst_hst": 114,
    "provincial_alcohol_tax": 116, "provincial_tobacco_tax": 117,
    "provincial_cannabis_duty": 115, "relieved": 118, "interest": 128,
    "total_amount": 119, "total_with_interest": 129,
}
LINE_KEYS = (
    "line_no", "line_no_raw", "page", "invoice_ref", "hs_code", "hs_code_6",
    "description", "quantity", "unit_of_measure", "country_of_origin",
    "place_of_export", "direct_shipment_date", "tariff_treatment",
    "value_for_currency_conversion", "conversion_currency", "exchange_rate",
    "value_for_duty",
)
# Line-level tax fields, in parse_cad's order: (form field, output key).
LINE_TAX_FIELDS = (
    (82, "duty"), (83, "excise_tax"), (84, "excise_duty"), (85, "surtax"),
    (86, "anti_dumping"), (87, "safeguard"), (88, "countervailing"),
    (89, "value_for_tax"), (90, "gst"), (91, "pst_hst"),
    (92, "provincial_alcohol_tax"), (93, "provincial_tobacco_tax"),
    (95, "pst_cannabis"),
)

_NUMBER_RE = re.compile(r"\s*-?[\d,]+(?:\.\d*)?\s*")


def number(value: str | None) -> Decimal | None:
    """Parse a printed number the way ``parse_cad._number`` does."""
    text = (value or "").strip()
    if not text or not _NUMBER_RE.fullmatch(text):
        return None
    text = text.replace(",", "").rstrip(".")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    return Decimal(text)


def money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def format_hs_code(raw: str) -> tuple[str, str | None]:
    """Return (10-digit dotted hs_code, 6-digit parent), as parse_cad does."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) < 10:
        return raw, None
    digits = digits[:10]
    return f"{digits[:4]}.{digits[4:6]}.{digits[6:]}", f"{digits[:4]}.{digits[4:6]}"


def _text(value):
    return value if value not in (None, "") else None


def build_result(fields: dict, source: str | None = None) -> dict:
    """Build a parse_cad-shaped result from a raw field dump.

    ``fields`` maps a form field number (as str or int) to either the raw value
    string or a ``{"label": ..., "value": ...}`` cell as the OCR script emits.
    """
    def cell(form_field: int):
        raw = fields.get(str(form_field), fields.get(form_field))
        if isinstance(raw, dict):
            return raw.get("value")
        return raw

    hs_code, hs_code_6 = format_hs_code(cell(58) or "")
    line_no_raw = "".join(str(cell(56) or "").split())

    item = {
        "line_no": int(line_no_raw) if line_no_raw.isdigit() else None,
        "line_no_raw": line_no_raw or None,
        "page": 1,
        "invoice_ref": None,
        "hs_code": hs_code or None,
        "hs_code_6": hs_code_6,
        "description": _text(cell(60)) or _text(cell(59)),
        "quantity": money(number(cell(61))),
        "unit_of_measure": _text(cell(62)),
        "country_of_origin": _text(cell(65)),
        "place_of_export": _text(cell(67)),
        "direct_shipment_date": _text(cell(69)),
        "tariff_treatment": _text(cell(70)),
        "value_for_currency_conversion": money(number(cell(75))),
        "conversion_currency": _text(cell(76)),
        "exchange_rate": money(number(cell(77))),
        "value_for_duty": money(number(cell(78))),
    }
    for form_field, key in LINE_TAX_FIELDS:
        item[key] = money(number(cell(form_field)))
    item["line_total"] = money(number(cell(100)))

    # A crop of fields 56-100 carries no header or invoice block. Emit every key
    # parse_cad emits, leave the values empty, and let the warnings explain why.
    header = {key: None for key in HEADER_KEYS}
    header["importer_bn"] = None
    header["importer"] = {key: "" for key in IMPORTER_KEYS}
    header["signed_date"] = None
    header["accounting_date"] = None   # mirrors submission_date, which is absent

    totals = {"currency": "CAD"}
    for key in TOTALS_KEYS:
        totals[key] = None
    line_tax_values = [item["value_for_tax"]] if item["value_for_tax"] is not None else []
    totals["value_for_tax"] = round(sum(line_tax_values), 2) if line_tax_values else None
    totals["value_for_tax_derived"] = True

    warnings = [{"code": "EMPTY_HEADER_FIELDS",
                 "fields": list(HEADER_KEYS) + ["importer_bn", "signed_date"]}]
    for key in TOTAL_FORM_FIELDS:
        warnings.append({"code": "MISSING_DECLARATION_TOTAL",
                         "field": f"totals.{key}",
                         "form_field": TOTAL_FORM_FIELDS[key]})
    warnings.append({"code": "MISSING_DECLARATION_TOTAL",
                     "field": "totals.total_duties_and_taxes", "form_field": 130})
    if item["value_for_tax"] is None and None not in (item["value_for_duty"], item["duty"]):
        # Field 89 is often blank on BSF864; parse_cad marks its derived total the
        # same way, and this states the per-line base that reproduces the GST.
        warnings.append({"code": "DERIVED_VALUE", "field": "totals.value_for_tax",
                         "method": "value_for_duty + duty",
                         "value": round(item["value_for_duty"] + item["duty"], 2)})
    warnings.append({"code": "IMAGE_ONLY_SOURCE",
                     "detail": "Input is a raster image of fields 56-100; values come "
                               "from local OCR, not a PDF text layer.",
                     "parser": "parse_b3_line_ocr.py",
                     "source": source})

    return {
        "schema_version": SCHEMA_VERSION,
        "document": {
            "form": "CBSA BSF864",
            "pages": 1,
            "printed_at": None,
            "notes": _text(cell(35)),
        },
        "header": header,
        "invoices": [],
        "totals": totals,
        "line_items": [item],
        "line_item_count": 1,
        "warnings": warnings,
    }
