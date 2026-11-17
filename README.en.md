# cbp7501-consumer

PDF parsing service for **CBP Form 7501** (U.S. Customs Entry Summary) and **Canada CBSA BSF864**. Consumes tasks from RabbitMQ, downloads PDFs, routes by `CbpFormType`, and publishes structured JSON to an output queue.

> [中文版文档](README.md)

## Features

- **RabbitMQ consumer** (`consumer.py`): async task processing with concurrency, reconnection, error reporting, and parser routing via `CbpFormType`
- **PDF parser v1** (`parse_7501.py`): `CbpFormType=10` (default) — header, line items, HTSUS, duties, etc.
- **Canada parser** (`parse_cad.py`): `CbpFormType=20` — parses CBSA BSF864 into its native structure (`schema_version` 2.0); amounts are CAD
- **U.S. line-item OCR** (`parse_7501_line_ocr.py`): `CbpFormType=15` — parses 7501 line-item screenshots (no text layer) with local OCR plus arithmetic checks
- **Canada line-item OCR** (`parse_b3_line_ocr.py`): `CbpFormType=25` — parses BSF864 line-item screenshots (no text layer) with local OCR plus arithmetic checks

### CbpFormType overview

| Value | Document | Input | Parser | Output key |
|---|---|---|---|---|
| `10` | U.S. CBP 7501 | PDF with text layer | `parse_7501.py` | `us_data` |
| `15` | U.S. CBP 7501 | Image / image-only PDF | `parse_7501_line_ocr.py` | `us_data` |
| `20` | Canada CBSA BSF864 | PDF with text layer | `parse_cad.py` | `ca_data` |
| `25` | Canada CBSA BSF864 | Image / image-only PDF | `parse_b3_line_ocr.py` | `ca_data` |

`15` / `25` are the OCR counterparts of `10` / `20` and **report under the same output
key** as their text-layer siblings, so downstream code binding `us_data` / `ca_data`
treats both sources interchangeably.

### Automatic text-layer fallback for `15`

`15` normally runs OCR, but when the **input URL ends in `.pdf`** it is parsed by
`parse_7501.py` (the same parser as `10`): a PDF text layer is exact and far faster than
OCRing the same document.

```
CbpFormType=15  +  a.pdf   →  parse_7501.py         (text layer)
CbpFormType=15  +  a.jpg   →  parse_7501_line_ocr.py (OCR)
```

Four things worth knowing:

- **Only `15` is rerouted.** `10` / `20` already require a text layer, and a caller asking
  for `25` explicitly wants OCR.
- **The extension comes from the URL, never from the temp file.** The downloaded temp file
  is always named `..._t15_<id>.pdf`; reading that would route every `15` to the text-layer
  parser and silently disable OCR.
- **When the URL has no extension** the downloaded bytes are sniffed for the `%PDF-` magic;
  if that is inconclusive, OCR stays in charge.
- **The reply still echoes `CbpFormType=15`.** Only the contents of `us_data` come from the
  `10` parser; the envelope keeps the caller's requested value.

Every parser is **imported only on first use**: a deployment that serves only text-layer
PDFs does not need the OCR packages, and vice versa. A missing dependency fails just that
one message, naming the absent package, without affecting process startup.

## Project Structure

```
cbp7501-consumer/
├── consumer.py              # RabbitMQ consumer entry point
├── parse_7501.py            # 7501 PDF parser (CbpFormType=10)
├── parse_cad.py             # Canada BSF864 parser (CbpFormType=20)
├── parse_7501_line_ocr.py   # 7501 line-item OCR parser (CbpFormType=15)
├── parse_b3_line_ocr.py     # BSF864 line-item OCR parser (CbpFormType=25)
├── parse_b3_line_schema.py  # Reshapes OCR output into BSF864 schema 2.0 (used by 25)
├── .env.example             # Environment variable template
├── files/
│   ├── input/               # Sample PDFs / images for local testing
│   └── output/              # Sample parse results
├── README.md                # Chinese documentation
└── README.en.md             # English documentation (this file)
```

## Requirements

- Python 3.10+
- RabbitMQ (required when running the consumer)
- OCR dependencies (only for `CbpFormType=15` / `25`)

## Installation

Dependencies are split into two files, so you install only what you need:

| Deployment | Command | Handles |
|---|---|---|
| Text layer only (small) | `pip install -r requirements.txt` | `10` / `20` |
| Text layer + OCR | `pip install -r requirements.txt -r requirements-ocr.txt` | `10` / `15` / `20` / `25` |
| OCR only | `pip install pika -r requirements-ocr.txt` | `15` / `25` |

```bash
# Typical: full deployment
pip install -r requirements.txt -r requirements-ocr.txt
```

- `requirements.txt`: `pika`, `pdfplumber`, `PyMuPDF` (text-layer parsing)
- `requirements-ocr.txt`: `rapidocr-onnxruntime`, `onnxruntime`, `opencv-python`, `Pillow`, `numpy`, `PyMuPDF`

The two files share only `PyMuPDF`, with matching versions. The OCR set is large
(~150 MB); skip it entirely when you do not need `15` / `25` — `consumer.py` imports
parsers lazily, so a missing dependency fails only the affected message and names the
absent package.

Copy the environment template and adjust as needed:

```bash
cp .env.example .env
```

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `RABBITMQ_HOST` | RabbitMQ host | `localhost` |
| `RABBITMQ_PORT` | AMQP port | `5672` |
| `RABBITMQ_USER` | Username | `guest` |
| `RABBITMQ_PASS` | Password | `guest` |
| `RABBITMQ_VHOST` | Virtual host | `/` |
| `INPUT_QUEUE` | Input queue | `cbp7501.parse.request` |
| `OUTPUT_QUEUE` | Output queue | `cbp7501.parse.result` |
| `PREFETCH_COUNT` | Concurrent in-flight messages | `4` |

Queues are automatically declared as **durable** on startup.

## Usage

### Start the Consumer

```bash
# Linux / macOS
export $(grep -v '^#' .env | xargs)
python consumer.py

# Windows PowerShell (example)
Get-Content .env | ForEach-Object {
  if ($_ -match '^([^#=]+)=(.*)$') { Set-Item -Path "env:$($matches[1])" -Value $matches[2] }
}
python consumer.py
```

### Parse PDFs Locally (CLI)

```bash
# U.S. 7501 (CbpFormType=10)
python parse_7501.py files/input/MRKU5394955.pdf files/output/result.json

# Canada BSF864 (CbpFormType=20)
python parse_cad.py files/input/CAD-1.pdf files/output/CAD-1.json
```

If no output path is given, results are printed to stdout.

## Message Formats

### Input Queue (`INPUT_QUEUE`)

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
  "URL": "https://example.com/pre-signed/7501.pdf",
  "CbpFormType": 10
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `ID` | Yes | Task ID; echoed in the response |
| `URL` | Yes | File download URL (supports pre-signed OBS links); `15` / `25` may point at an image or an image-only PDF |
| `FileNumber` | No | Business reference; included in error responses |
| `CbpFormType` | No | Integer (a numeric string is also accepted): `10` U.S. full container (default), `15` U.S. line-item screenshot, `20` Canada full container, `25` Canada line-item screenshot |

### Output Queue (`OUTPUT_QUEUE`)

The parsed payload is keyed by the form type it came from: `us_data` for type 10, `ca_data` for type 20. **Only the matching key is present.**

**Success (`CbpFormType=10`):**

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
  "CbpFormType": 10,
  "status": "ok",
  "us_data": {
    "header": { "...": "..." },
    "line_items": [ "..." ],
    "line_item_count": 12
  }
}
```

**Success (`CbpFormType=20`):**

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
  "CbpFormType": 20,
  "status": "ok",
  "ca_data": {
    "schema_version": "2.0",
    "document": { "...": "..." },
    "header": { "...": "..." },
    "invoices": [ "..." ],
    "totals": { "...": "..." },
    "line_items": [ "..." ],
    "line_item_count": 1,
    "warnings": []
  }
}
```

**Failure (no `us_data` / `ca_data`, only `message`):**

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
  "CbpFormType": 10,
  "status": "error",
  "message": "Download failed: ..."
}
```

## Parse Result Structure

For **`CbpFormType=10` (7501)** and **`CbpFormType=15` (7501 screenshot)**, `us_data`
typically contains:

- **header**: form header (entry number, entry type, port, dates, carrier, country of origin, etc.); `null` for the OCR path, since a line-item crop has no header block
- **line_items**: line-level commodity data (descriptions, HTSUS, quantities, duty rates and amounts)
- **line_item_count**: number of line items

### The two quantity fields (easy to confuse)

A 7501 carries two different quantity concepts, and they live at different levels:

| Field | Meaning | Source | Example |
|---|---|---|---|
| `header.manifest_quantity` | **Whole-declaration box count**, one per entry | Trailing the BL row (`... a 520 CT`) or a standalone manifest row | `652 CTN` |
| `line_items[].manifest_note` | **That line's net quantity** (Net Quantity in HTSUS Units) | The quantity column of each item row | `420.00 NO` |

```json
{
  "header": { "manifest_quantity": "652 CTN" },
  "line_items": [
    { "line_no": "001", "manifest_note": "420.00 NO",
      "commodity": { "net_quantity": 420.0, "net_quantity_unit": "NO" } },
    { "line_no": "002", "manifest_note": "172.00 NO" },
    { "line_no": "003", "manifest_note": "60.00 NO" }
  ]
}
```

Notes:

- **`manifest_note` is per line**, formatted as `<quantity> <HTSUS unit>` with **two decimals** (`420.00 NO`, `41.33 DOZ`, `477.36 KG`)
- It is **not** the box count. An earlier build put the declaration-wide box count on the first line's `manifest_note`, so one figure stood in for every line -- fixed
- `manifest_note` shares its source with `commodity.net_quantity` / `net_quantity_unit` and is the formatted view of those two; read `commodity` for the number, `manifest_note` for the display string
- When a line's quantity could not be read, `manifest_note` is `null` -- it never borrows another line's value or invents `0.00`
- **On the `15` (OCR) path**: a line-item screenshot has no header block, so `header` is `null` and there is no `header.manifest_quantity`; `line_items[].manifest_note` is still filled per line (`420.00 NO`)

#### Unit normalisation for `header.manifest_quantity`

The form spells the same box count several ways; every package unit is normalised to **`CTN`**:

| Printed | Output | Note |
|---|---|---|
| `520 CT` | `520 CTN` | short form |
| `365 CTNS` | `365 CTN` | plural |
| `652 PCS` | `652 CTN` | piece count |
| `12 PKG` | `12 CTN` | package count |
| `1,320 CTN` | `1,320 CTN` | thousands separator kept |
| `5133 KG` | `5133 KG` | **a weight is not a box count**, left as printed |

The numeric part is kept exactly as printed (thousands separator included); only the unit is rewritten.

For **`CbpFormType=20`** and **`CbpFormType=25`** (Canada BSF864), `ca_data` uses the native BSF864 shape (`schema_version` 2.0) rather than mirroring the 7501 result:

```json
{
  "schema_version": "2.0",
  "document":   { "form": "CBSA BSF864", "pages": 9,
                  "printed_at": "2026-11-06 17:02", "notes": "DFSU7422980" },
  "header":     { "type_code": "AB", "office_no": "495", "importer_bn": "756552105RM0001",
                  "gross_weight_kg": 19859.53, "cargo_control_no": "...",
                  "importer": { "name": "...", "street": "...", "city": "...",
                                "zip": "V6C1S4", "country": "CA" } },
  "invoices":   [ { "page": 2, "invoice_no": "...", "invoice_value": 10006.22,
                    "invoice_currency": "USD", "freight": 0.0, "vendor": {...} } ],
  "totals":     { "currency": "CAD", "value_for_duty": 13584.45, "duty": 0.0,
                  "gst": 679.22, "pst_hst": 0.0, "total_duties_and_taxes": 679.22,
                  "value_for_tax": 13584.45, "value_for_tax_derived": true },
  "line_items": [ { "line_no": 1, "hs_code": "9403.60.9090", "hs_code_6": "9403.60",
                    "description": "STORAGE CABINET", "quantity": 578,
                    "value_for_duty": 13584.45, "gst": 679.22, "line_total": 679.22 } ],
  "line_item_count": 1,
  "warnings":   [ { "code": "EMPTY_HEADER_FIELDS", "fields": ["signed_date"] } ]
}
```

Key points:

- **`document`**: form identity, page count, print timestamp (from the footer), and field 35 notes
- **`header`**: flat mapping of BSF864 fields 1-22; `importer` is `{name, street, city, zip, country}` and `importer_bn` is separate (`null` when the form does not print one)
- **`invoices`**: fields 36-43 are a **page-level** block (one invoice can span pages), so they live in an array; line items point at it via `invoice_ref`
- **`totals`**: full duty/tax breakdown from fields 113-130; the `*_usd` suffixes are gone and `currency` is always `"CAD"`
- **`value_for_tax`**: BSF864 has **no** declaration-level value for tax, so this is derived as `sum(line_items[].value_for_tax)` and flagged with `value_for_tax_derived: true`
- **`line_items`**: `hs_code` is the full **10-digit** code (`9403.60.9090`) with `hs_code_6` alongside; `line_no` is an integer and `line_no_raw` keeps the printed form; each duty/tax is its own flat field
- **`warnings`**: separates "blank on the form" (`EMPTY_HEADER_FIELDS` / `EMPTY_QUANTITY`) from real parse problems (`TOTAL_MISMATCH` / `MISSING_DECLARATION_TOTAL`), plus `DERIVED_VALUE` for computed fields

## Processing Flow

```
INPUT_QUEUE → download file → CbpFormType?
                             ├─ 10 → parse_7501.parse()                  (PDF text layer)
                             ├─ 15 → URL ends in .pdf?
                             │        ├─ yes → parse_7501.parse()            (text layer)
                             │        └─ no  → parse_7501_line_ocr.parse_result() (OCR)
                             ├─ 20 → parse_cad.parse()                   (PDF text layer)
                             └─ 25 → parse_b3_line_ocr.parse_result()    (local OCR)
                                   → OUTPUT_QUEUE → ACK
                 ↓ on failure
             OUTPUT_QUEUE (status: error) → ACK
```

The consumer **ACKs** the original message whether parsing succeeds or fails, preventing task backlog.

Parsers are **not imported at startup**; each is loaded and cached on the first message of
its type:

- With only the text-layer parsers installed, a `15` / `25` message gets an error reply
  naming the missing package, and the process keeps running
- With only the OCR parsers installed the reverse holds, leaving `10` / `20` untouched
- `parse_7501.py` calls `sys.exit()` when `pdfplumber` is absent; lazy import turns even
  that into a single-message error instead of a process exit

## Notes

- Do not commit `.env` (contains secrets); use `.env.example` as a reference only
- Sample files under `files/input/` and `files/output/` are for local testing
- The v1 parser handles common 7501 PDF layouts; Canada BSF864 layouts are handled by `parse_cad.py`.
- `15` / `25` need the extra OCR packages: `rapidocr-onnxruntime`, `onnxruntime`,
  `opencv-python`, `pillow`, `pymupdf` (see `requirements-ocr.txt`)
- On the OCR path `header` is `null` and `totals` is summed from the line items: a
  line-item crop contains neither the header block nor the declaration totals, and the
  result flags this with `PARTIAL_DOCUMENT` / `DERIVED_VALUE` warnings

## License

This repository is **proprietary software. All rights reserved.** No use, copying, modification, distribution, or sublicensing is permitted without the prior written permission of the copyright holder.

Public visibility on GitHub is for demonstration and collaboration convenience only and does not grant any license. See [LICENSE](LICENSE).
