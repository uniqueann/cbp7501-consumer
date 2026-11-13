# cbp7501-consumer

PDF parsing service for **CBP Form 7501** (U.S. Customs Entry Summary) and **Canada CBSA BSF864**. Consumes tasks from RabbitMQ, downloads PDFs, routes by `CbpFormType`, and publishes structured JSON to an output queue.

> [中文版文档](README.md)

## Features

- **RabbitMQ consumer** (`consumer.py`): async task processing with concurrency, reconnection, error reporting, and parser routing via `CbpFormType`
- **PDF parser v1** (`parse_7501.py`): `CbpFormType=10` (default) — header, line items, HTSUS, duties, etc.
- **Canada parser** (`parse_cad.py`): `CbpFormType=20` — parses CBSA BSF864 into its native structure (`schema_version` 2.0); amounts are CAD

## Project Structure

```
cbp7501-consumer/
├── consumer.py          # RabbitMQ consumer entry point
├── parse_7501.py        # 7501 PDF parser (CbpFormType=10)
├── parse_cad.py         # Canada BSF864 parser (CbpFormType=20)
├── .env.example         # Environment variable template
├── files/
│   ├── input/           # Sample PDFs / JSON for local testing
│   └── output/          # Sample parse results
├── README.md            # Chinese documentation
└── README.en.md         # English documentation (this file)
```

## Requirements

- Python 3.10+
- RabbitMQ (required when running the consumer)

## Installation

```bash
pip install -r requirements.txt
```

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
| `URL` | Yes | PDF download URL (supports pre-signed OBS links) |
| `FileNumber` | No | Business reference; included in error responses |
| `CbpFormType` | No | Integer: `10` U.S. full container (default), `20` Canada full container |

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

For **`CbpFormType=10` (7501)**, `us_data` typically contains:

- **header**: form header (entry number, entry type, port, dates, carrier, country of origin, etc.)
- **line_items**: line-level commodity data (descriptions, HTSUS, quantities, duty rates and amounts)
- **line_item_count**: number of line items

For **`CbpFormType=20` (Canada BSF864)**, `ca_data` uses the native BSF864 shape (`schema_version` 2.0) rather than mirroring the 7501 result:

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
INPUT_QUEUE → download PDF → CbpFormType?
                              ├─ 10 → parse_7501.parse()
                              └─ 20 → parse_cad.parse()
                                    → OUTPUT_QUEUE → ACK
                  ↓ on failure
              OUTPUT_QUEUE (status: error) → ACK
```

The consumer **ACKs** the original message whether parsing succeeds or fails, preventing task backlog.

## Notes

- Do not commit `.env` (contains secrets); use `.env.example` as a reference only
- Sample files under `files/input/` and `files/output/` are for local testing
- The v1 parser handles common 7501 PDF layouts; Canada BSF864 layouts are handled by `parse_cad.py`.

## License

This repository is **proprietary software. All rights reserved.** No use, copying, modification, distribution, or sublicensing is permitted without the prior written permission of the copyright holder.

Public visibility on GitHub is for demonstration and collaboration convenience only and does not grant any license. See [LICENSE](LICENSE).
