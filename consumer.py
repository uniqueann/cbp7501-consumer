#!/usr/bin/env python3
"""
CBP / Canada customs PDF — RabbitMQ Consumer
---------------------------------------------
Consumes messages from INPUT_QUEUE, each message is JSON:
    {
        "ID": 1,
        "FileNumber": "FN2603200001",
        "URL": "https://...doc.pdf?...",
        "CbpFormType": 10
    }

CbpFormType (optional, default 10):
    10 — U.S. full container (CBP Form 7501) → parse_7501.parse()
    20 — Canada full container (CBSA BSF864) → parse_cad.parse()

For each message:
  1. Download the PDF from URL (pre-signed OBS link, no auth header needed)
  2. Parse with the parser selected by CbpFormType
  3. Publish result to OUTPUT_QUEUE, echoing ID / FileNumber / CbpFormType:
     {"ID": 1, "FileNumber": "...", "CbpFormType": 10, "status": "ok", "data": { ...parsed... }}
     or on error:
     {"ID": 1, "FileNumber": "...", "CbpFormType": 10, "status": "error", "message": "..."}
  4. ack the original message

Environment variables (put in .env or export before running):
    RABBITMQ_HOST   broker host                         (default: localhost)
    RABBITMQ_PORT   broker AMQP port                    (default: 5672)
    RABBITMQ_USER   username                            (default: guest)
    RABBITMQ_PASS   password                            (required in production)
    RABBITMQ_VHOST  virtual host                        (default: /)
    INPUT_QUEUE     queue to consume from               (default: cbp7501.parse.request)
    OUTPUT_QUEUE    queue to publish results to         (default: cbp7501.parse.result)
    PREFETCH_COUNT  messages in-flight at once          (default: 4)
"""

import json
import logging
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pika

# ── make parsers importable whether running from repo root or here ─────────────
sys.path.insert(0, str(Path(__file__).parent))
# FORM_TYPE_* come from the parsers themselves so each form type has a single
# source of truth (parse_7501 → 10, parse_cad → 20).
from parse_7501 import CBP_FORM_TYPE as FORM_TYPE_US, parse as parse_pdf
from parse_cad import CBP_FORM_TYPE as FORM_TYPE_CA, parse as parse_cad_pdf

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cbp7501-consumer")

# ── config from env ───────────────────────────────────────────────────────────
RABBITMQ_HOST  = os.environ.get("RABBITMQ_HOST",  "localhost")
RABBITMQ_PORT  = int(os.environ.get("RABBITMQ_PORT", "5672"))
RABBITMQ_USER  = os.environ.get("RABBITMQ_USER",  "guest")
RABBITMQ_PASS  = os.environ.get("RABBITMQ_PASS",  "guest")
RABBITMQ_VHOST = os.environ.get("RABBITMQ_VHOST", "/")
INPUT_QUEUE    = os.environ.get("INPUT_QUEUE",  "cbp7501.parse.request")
OUTPUT_QUEUE   = os.environ.get("OUTPUT_QUEUE", "cbp7501.parse.result")
PREFETCH_COUNT = int(os.environ.get("PREFETCH_COUNT", "4"))

# FORM_TYPE_US / FORM_TYPE_CA are imported from parse_7501 / parse_cad above.
_SUPPORTED_FORM_TYPES = (FORM_TYPE_US, FORM_TYPE_CA)


def _resolve_form_type(payload: dict) -> int:
    """Return normalized CbpFormType; default 10. Raises ValueError if unsupported."""
    raw = payload.get("CbpFormType", FORM_TYPE_US)
    if raw is None:
        raw = FORM_TYPE_US
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return FORM_TYPE_US
        try:
            raw = int(raw)
        except ValueError as e:
            raise ValueError(f"Invalid CbpFormType: {payload.get('CbpFormType')!r}") from e
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"Invalid CbpFormType: {payload.get('CbpFormType')!r}")
    if raw not in _SUPPORTED_FORM_TYPES:
        raise ValueError(f"Unsupported CbpFormType: {raw}")
    return raw


# ── helpers ───────────────────────────────────────────────────────────────────
def download_pdf(url: str, dest: str, timeout: int = 60) -> None:
    """Download a file from URL to dest path (works with pre-signed OBS links)."""
    req = urllib.request.Request(url, headers={"User-Agent": "cbp7501-consumer/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        while chunk := resp.read(1 << 16):   # 64 KB chunks
            f.write(chunk)


def declare_queues(channel: pika.adapters.blocking_connection.BlockingChannel) -> None:
    for q in (INPUT_QUEUE, OUTPUT_QUEUE):
        channel.queue_declare(queue=q, durable=True)


# ── message handler ───────────────────────────────────────────────────────────
def on_message(channel, method, properties, body):
    msg_id      = None
    file_number = ""
    form_type   = None
    tmp_pdf     = None
    try:
        payload     = json.loads(body)
        msg_id      = payload.get("ID")
        file_number = payload.get("FileNumber", "")
        # Echo the requested CbpFormType in the envelope even when validation or
        # parsing fails; _resolve_form_type() overwrites it with the normalized
        # value on success.
        raw_form_type = payload.get("CbpFormType")
        form_type     = FORM_TYPE_US if raw_form_type in (None, "") else raw_form_type
        url           = payload["URL"]
        form_type     = _resolve_form_type(payload)
        log.info(
            "Received ID=%s  FileNumber=%s  CbpFormType=%s  URL=%s…",
            msg_id, file_number, form_type, url[:80],
        )

        # 1. Download PDF to a temp file
        suffix  = f"_t{form_type}_{msg_id}.pdf"
        tmp_pdf = tempfile.mktemp(suffix=suffix)
        download_pdf(url, tmp_pdf)
        log.info("Downloaded %d bytes → %s", Path(tmp_pdf).stat().st_size, tmp_pdf)

        # 2. Parse
        if form_type == FORM_TYPE_US:
            result = parse_pdf(tmp_pdf)
        else:
            result = parse_cad_pdf(tmp_pdf)
        log.info("Parsed %d line items", result.get("line_item_count", 0))

        # 3. Publish result
        out_msg = json.dumps(
            {
                "ID":           msg_id,
                "FileNumber":   file_number,
                "CbpFormType":  form_type,
                "status":       "ok",
                "data":         result,
            },
            ensure_ascii=False,
        )
        channel.basic_publish(
            exchange="",
            routing_key=OUTPUT_QUEUE,
            body=out_msg.encode(),
            properties=pika.BasicProperties(
                delivery_mode=2,          # persistent
                content_type="application/json",
            ),
        )
        log.info("Published result for ID=%s to %s", msg_id, OUTPUT_QUEUE)

    except KeyError as e:
        _publish_error(channel, msg_id, file_number, form_type, f"Missing field in message: {e}")
    except ValueError as e:
        _publish_error(channel, msg_id, file_number, form_type, str(e))
    except urllib.error.URLError as e:
        _publish_error(channel, msg_id, file_number, form_type, f"Download failed: {e}")
    except Exception as e:
        log.exception("Unexpected error for ID=%s", msg_id)
        _publish_error(channel, msg_id, file_number, form_type, str(e))
    finally:
        # Always ack (we've either published result or error)
        channel.basic_ack(delivery_tag=method.delivery_tag)
        # Clean up temp file
        if tmp_pdf and Path(tmp_pdf).exists():
            Path(tmp_pdf).unlink(missing_ok=True)


def _publish_error(channel, msg_id, file_number, form_type, message: str) -> None:
    log.error("Error for ID=%s FileNumber=%s CbpFormType=%s: %s",
              msg_id, file_number, form_type, message)
    try:
        err_msg = json.dumps(
            {
                "ID":          msg_id,
                "FileNumber":  file_number,
                "CbpFormType": form_type,
                "status":      "error",
                "message":     message,
            },
            ensure_ascii=False,
        )
        channel.basic_publish(
            exchange="",
            routing_key=OUTPUT_QUEUE,
            body=err_msg.encode(),
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type="application/json",
            ),
        )
    except Exception:
        log.exception("Failed to publish error message")


# ── connection with retry ─────────────────────────────────────────────────────
def connect_with_retry(max_retries: int = 10, delay: float = 3.0):
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    params = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        virtual_host=RABBITMQ_VHOST,
        credentials=credentials,
        heartbeat=60,
        blocked_connection_timeout=300,
    )
    for attempt in range(1, max_retries + 1):
        try:
            conn = pika.BlockingConnection(params)
            log.info("Connected to RabbitMQ %s:%d (attempt %d)",
                     RABBITMQ_HOST, RABBITMQ_PORT, attempt)
            return conn
        except pika.exceptions.AMQPConnectionError as e:
            if attempt == max_retries:
                raise
            log.warning("Connection failed (%s), retrying in %.0fs…", e, delay)
            time.sleep(delay)


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("Starting CBP 7501 consumer")
    log.info("  RABBITMQ     = %s:%d  vhost=%s", RABBITMQ_HOST, RABBITMQ_PORT, RABBITMQ_VHOST)
    log.info("  INPUT_QUEUE  = %s", INPUT_QUEUE)
    log.info("  OUTPUT_QUEUE = %s", OUTPUT_QUEUE)
    log.info("  PREFETCH     = %d", PREFETCH_COUNT)

    while True:
        try:
            conn    = connect_with_retry()
            channel = conn.channel()
            declare_queues(channel)
            channel.basic_qos(prefetch_count=PREFETCH_COUNT)
            channel.basic_consume(queue=INPUT_QUEUE, on_message_callback=on_message)
            log.info("Waiting for messages on %s …", INPUT_QUEUE)
            channel.start_consuming()

        except pika.exceptions.ConnectionClosedByBroker:
            log.warning("Connection closed by broker, reconnecting…")
        except pika.exceptions.AMQPChannelError as e:
            log.error("Channel error %s, reconnecting…", e)
        except pika.exceptions.AMQPConnectionError:
            log.error("Connection lost, reconnecting in 5s…")
            time.sleep(5)
        except KeyboardInterrupt:
            log.info("Shutting down (KeyboardInterrupt)")
            try:
                conn.close()
            except Exception:
                pass
            break


if __name__ == "__main__":
    main()
