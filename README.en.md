# cbp7501-consumer

PDF parsing service for **CBP Form 7501** (U.S. Customs Entry Summary). Consumes tasks from RabbitMQ, downloads PDFs, parses them into structured JSON, and publishes results to an output queue.

> [中文版文档](README.md)

## Features

- **RabbitMQ consumer** (`consumer.py`): async task processing with concurrency, reconnection, and error reporting
- **PDF parser v1** (`parse_7501.py`): extracts header, line items, HTSUS codes, duties, and related fields from common 7501 layouts

## Project Structure

```
cbp7501-consumer/
├── consumer.py          # RabbitMQ consumer entry point
├── parse_7501.py        # 7501 PDF parser (v1; used by consumer)
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
pip install pika pdfplumber
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
# v1 parser
python parse_7501.py files/input/MRKU5394955.pdf files/output/result.json

# ```

If no output path is given, results are printed to stdout.

## Message Formats

### Input Queue (`INPUT_QUEUE`)

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
  "URL": "https://example.com/pre-signed/7501.pdf"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `ID` | Yes | Task ID; echoed in the response |
| `URL` | Yes | PDF download URL (supports pre-signed OBS links) |
| `FileNumber` | No | Business reference; included in error responses |

### Output Queue (`OUTPUT_QUEUE`)

**Success:**

```json
{
  "ID": 1,
  "FileNumber": "FN2603200001",
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
  "status": "error",
  "message": "Download failed: ..."
}
```

## Parse Result Structure

The `data` field typically contains:

- **header**: form header (entry number, entry type, port, dates, carrier, country of origin, etc.)
- **line_items**: line-level commodity data (descriptions, HTSUS, quantities, duty rates and amounts)
- **line_item_count**: number of line items

## Processing Flow

```
INPUT_QUEUE → download PDF → parse_7501.parse() → OUTPUT_QUEUE → ACK
                  ↓ on failure
              OUTPUT_QUEUE (status: error) → ACK
```

The consumer **ACKs** the original message whether parsing succeeds or fails, preventing task backlog.

## Notes

- Do not commit `.env` (contains secrets); use `.env.example` as a reference only
- Sample files under `files/input/` and `files/output/` are for local testing
- The v1 parser handles common PDF layouts; if you encounter a new format, extend or adjust parsing rules locally.

## License

No open-source license is specified. Contact the repository maintainer before use.
