# cbp7501-consumer

PDF parsing service for **CBP Form 7501** (U.S. Customs Entry Summary) and **Canada CBSA BSF864**. Consumes tasks from RabbitMQ, downloads PDFs, routes by `CbpFormType`, and publishes structured JSON to an output queue.

> [中文版文档](README.md)

## Features

- **RabbitMQ consumer** (`consumer.py`): async task processing with concurrency, reconnection, error reporting, and parser routing via `CbpFormType`
- **PDF parser v1** (`parse_7501.py`): `CbpFormType=10` (default) — header, line items, HTSUS, duties, etc.
- **Canada parser** (`parse_cad.py`): `CbpFormType=20` — parses CBSA BSF864 and returns the same schema as the 7501 parser; amounts are CAD (see the currency notes below)

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

**Success:**

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
  "CbpFormType": 10,
  "status": "ok",
  "data": {
    "header": { "...": "..." },
    "line_items": [ "..." ],
    "line_item_count": 12
  }
}
```

**Failure:**

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

For **`CbpFormType=10` (7501)**, `data` typically contains:

- **header**: form header (entry number, entry type, port, dates, carrier, country of origin, etc.)
- **line_items**: line-level commodity data (descriptions, HTSUS, quantities, duty rates and amounts)
- **line_item_count**: number of line items

For **`CbpFormType=20` (Canada BSF864)**, `data` uses the **exact same** `header` / `line_items` / `line_item_count` structure as the 7501 parser so downstream code can handle both uniformly. Watch these semantic differences:

- `header.currency` is `"CAD"`; `header.invoice_currency` comes from form field 40
- The `totals.*_usd` key names are inherited from the 7501 parser but the **values are CAD** — always check `header.currency` first
- `totals.other_fees_usd` = `grand_total - duty`, i.e. the GST/PST tax total
- `line_items[].fees[]` carries GST / PST / excise items; `code` is the BSF864 field number (e.g. `"90"` is GST) and `rate` is always `null`
- `header.consignee` is an empty placeholder; importer details live in `header.importer_of_record`
- 7501-only fields (`surety_number`, `bond_type`, `mpf_usd`, `hmf_usd`, …) are always empty string or `null`

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
