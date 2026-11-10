#!/usr/bin/env python3
"""Parse CBSA BSF864 CAD PDFs into the parse_7501.py result shape.

Duty and tax amounts are returned as printed, in CAD; invoice values retain
invoice currency. The 7501-compatible *_usd keys hold CAD values here, so
consumers must check header.currency before interpreting them.

Usage:
    python parse_cad.py <input.pdf> [output.json]
"""

from __future__ import annotations

import json
import re
import sys
from decimal import Decimal

import pdfplumber

_FORM_MARKERS = (
    "Commercial Accounting Declaration",
    "Déclaration en détail commerciale",
)
# CbpFormType handled by this parser. consumer.py reports it in the output
# envelope next to ID / FileNumber, and uses it as the default when an inbound
# message omits the field.
CBP_FORM_TYPE = 20
_MONEY_RE = r"-?[\d,]+\.\d{2}"
_TAX_FIELDS = {
    83: "Excise Tax",
    84: "Excise Duty",
    85: "Surtax",
    86: "Anti-Dumping",
    87: "Safeguard",
    88: "Countervailing",
    90: "GST",
    91: "PST/HST",
    92: "Provincial Alcohol Tax",
    93: "Provincial Tobacco Amount",
    95: "Provincial Cannabis Excise Duty",
}


def _field(table: list[list[str | None]], number: int, label: str,
           start: int = 0, end: int | None = None) -> str:
    """Read the value printed inside a numbered form cell or directly below it."""
    label_pattern = r"Type(?: Code)?" if number == 1 and label == "Type Code" else re.escape(label)
    for row_index in range(start, len(table) if end is None else end):
        for col, cell in enumerate(table[row_index]):
            if not cell or not re.match(rf"^\s*{number}\s*{label_pattern}", cell, re.I):
                continue
            lines = cell.splitlines()
            value_start = 1 if (number == 1 and label == "Type Code" and len(lines) == 2
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


def _amount(text: str, number: int, label: str) -> Decimal | None:
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


def _number(value: str) -> Decimal | None:
    match = re.fullmatch(r"\s*-?[\d,]+(?:\.\d+)?\s*", value)
    return Decimal(value.strip().replace(",", "")) if match else None


def _money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


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
    lines = address.splitlines()
    name = lines[0].strip() if lines else ""
    street = " ".join(line.strip() for line in lines[1:-2]) if len(lines) > 2 else ""
    city, zip_code = "", ""
    country = "CA" if lines and lines[-1].strip().lower() == "canada" else ""
    if len(lines) > 1:
        city_line = lines[-2] if len(lines) > 2 else lines[-1]
        match = re.match(r"(.+?)\s+[A-Z]{2}\s+([A-Z]\d[A-Z]\s?\d[A-Z]\d)$", city_line)
        if match:
            city, zip_code = match.groups()
        else:
            combined = re.fullmatch(r"(.+?)\s+[A-Z]{2}\s+([A-Z]\d[A-Z]\s?\d[A-Z]\d)\s+CANADA", city_line, re.I)
            if combined:
                city, zip_code = combined.groups()
                country = "CA"
            else:
                city = city_line.strip()
    return {
        "name": name,
        "street": street,
        "city": city,
        "country": country,
        "zip": zip_code,
    }


def _parse_item(table: list[list[str | None]], start: int, end: int) -> dict:
    field = lambda number, label: _field(table, number, label, start, end)
    line_no = field(56, "C. Ln N.")
    classification = re.sub(r"\s+", "", field(58, "Classification No."))
    if re.fullmatch(r"\d{10}", classification):
        classification = f"{classification[:4]}.{classification[4:6]}.{classification[6:]}"
    if not line_no.isdigit() or not re.fullmatch(r"\d{4}\.\d{2}\.\d{4}", classification):
        raise ValueError("Could not parse CAD line number or classification")
    fees = []
    for number, label in _TAX_FIELDS.items():
        value = _number(field(number, label))
        if value is not None and value != 0:
            fees.append({
                "code": str(number),
                "description": label,
                "rate": None,
                "amount": _money(value),
            })
    return {
        "line_no": line_no.zfill(3),
        "line_type": "STANDARD",
        "manifest_note": None,
        "exclusion_note": None,
        "tariff_subheadings": [],
        "value_for_currency_conversion": _money(_number(field(75, "Value for Currency Conversion"))),
        "conversion_currency": field(76, "Currency"),
        "exchange_rate": _money(_number(field(77, "Exchange Rate"))),
        "commodity": {
            "description": field(60, "Narrative Description") or field(59, "Classification Description"),
            "htsus": classification,
            "gross_weight_kg": None,
            "net_quantity": float(quantity) if (quantity := _number(field(61, "Quantity"))) is not None else None,
            "net_quantity_unit": field(62, "Unit of Measure"),
            "entered_value": _money(_number(field(78, "Value for Duty"))),
            "htsus_rate": None,
            "duty_amount": _money(_number(field(82, "Customs Duty"))),
        },
        "fees": fees,
    }


def parse(pdf_path: str) -> dict:
    """Return the same header / line_items / line_item_count schema as parse_7501."""
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            raise ValueError("Empty CAD PDF")
        pdf_pages = [page.dedupe_chars(tolerance=1) for page in pdf.pages]
        pages = [page.extract_text() or "" for page in pdf_pages]
        if not any(marker in pages[0] for marker in _FORM_MARKERS):
            raise ValueError("Not a recognized Canada CAD (BSF864) PDF")
        tables = [table for page, text in zip(pdf_pages, pages) for table in _extract_tables(page, text)]
        header_table = next((table for table in tables if any(
            cell and re.match(r"^\s*1\s+Type(?: Code)?\b", cell, re.I)
            for row in table for cell in row
        )), None)
        if header_table is None:
            raise ValueError("Could not parse CAD header")
        detail_tables = [table for table in tables if any(
            _field(table, 58, "Classification No.", index, index + 1)
            for index in range(len(table))
        )]
        if not detail_tables:
            raise ValueError("No CAD line items could be parsed from the PDF")

        invoice_table = next((table for table in tables if _field(table, 38, "Invoice No.")), None)
        invoice_currency = _field(invoice_table, 40, "Invoice Currency Code") if invoice_table else ""

        line_items = []
        for table in detail_tables:
            starts = [index for index, row in enumerate(table) if any(
                cell and re.match(r"^\s*58\s*Classification No\.", cell) for cell in row
            )]
            for index, start in enumerate(starts):
                end = starts[index + 1] if index + 1 < len(starts) else len(table)
                line_items.append(_parse_item(table, start, end))

        declaration = pages[0]
        value = _amount(declaration, 113, "Total Value for Duty")
        duty = _amount(declaration, 120, "Total Customs Duties")
        grand_total = _amount(declaration, 130, "TOTAL DUTIES & TAXES")
        if value is None or duty is None or grand_total is None:
            raise ValueError("Could not parse CAD declaration totals")
        if len(line_items) == 1:
            weight = _number(_field(header_table, 8, "Gross Weight/kg"))
            line_items[0]["commodity"]["gross_weight_kg"] = float(weight) if weight is not None else None

        origin = _field(detail_tables[0], 65, "Country of Origin")
        header = {
            "form": "CBSA BSF864",
            "currency": "CAD",
            "invoice_currency": invoice_currency,
            "filer_code_entry_number": _field(header_table, 4, "CAD Transaction No."),
            "entry_type": _field(header_table, 1, "Type Code"),
            "summary_date": _field(header_table, 3, "Accounting Date"),
            "surety_number": "",
            "bond_type": "",
            "port_code": _field(header_table, 5, "Office No."),
            "entry_date": _field(header_table, 7, "Release Date"),
            "importing_carrier": _field(header_table, 9, "Carrier Code at Importation"),
            "country_of_origin": origin,
            "import_date": _field(header_table, 7, "Release Date"),
            "bl_number": "",
            "manufacturer_id": "",
            "exporting_country": _field(detail_tables[0], 67, "Place of Export"),
            "export_date": _field(detail_tables[0], 69, "Direct Shipment Date"),
            "foreign_port_of_lading": "",
            "us_port_of_unlading": "",
            "location_of_goods": "",
            "reference_number": _field(header_table, 15, "Cargo Control No."),
            "manifest_quantity": "",
            "consignee": {
                "name": "", "street": "", "city": "", "state": "", "zip": "", "customer_reference": "",
            },
            "importer_of_record": _parse_importer(header_table),
            "totals": {
                "entered_value_usd": _money(value),
                "duty_usd": _money(duty),
                "other_fees_usd": _money(grand_total - duty),
                "grand_total_usd": _money(grand_total),
                "mpf_usd": None,
                "hmf_usd": None,
            },
        }
        return {"header": header, "line_items": line_items, "line_item_count": len(line_items)}


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
