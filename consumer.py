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
    10 — U.S. full container (CBP Form 7501)          → parse_7501.parse()
         PDF with a real text layer.
    15 — U.S. 7501 line-item screenshot               → parse_7501_line_ocr.parse_result()
         Raster image / image-only PDF, parsed by local OCR.
    20 — Canada full container (CBSA BSF864)          → parse_cad.parse()
         PDF with a real text layer.
    25 — Canada BSF864 line-item screenshot           → parse_b3_line_ocr.parse_result()
         Raster image / image-only PDF, parsed by local OCR.

For each message:
  1. Download the PDF from URL (pre-signed OBS link, no auth header needed)
  2. Parse with the parser selected by CbpFormType
  3. Publish result to OUTPUT_QUEUE, echoing ID / FileNumber / CbpFormType.
     On success the parsed payload is keyed by the form type family it came from:
     {"ID": 1, "FileNumber": "...", "CbpFormType": 10, "status": "ok", "us_data": { ...parsed... }}
     {"ID": 1, "FileNumber": "...", "CbpFormType": 15, "status": "ok", "us_data": { ...parsed... }}
     {"ID": 1, "FileNumber": "...", "CbpFormType": 20, "status": "ok", "ca_data": { ...parsed... }}
     {"ID": 1, "FileNumber": "...", "CbpFormType": 25, "status": "ok", "ca_data": { ...parsed... }}
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

import importlib
import json
import logging
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pika

# ── make parsers importable whether running from repo root or here ─────────────
sys.path.insert(0, str(Path(__file__).parent))

# CbpFormType values. 10/20 read a PDF text layer, 15/25 run local OCR on a raster
# image or an image-only PDF. 15 is the OCR path for the same U.S. 7501 form as 10,
# and 25 the OCR path for the same BSF864 form as 20.
#
# These mirror each parser's own ``CBP_FORM_TYPE`` constant. The numbers are
# restated here (not imported) because the parsers are loaded lazily -- see below --
# and tests/test_consumer_form_types.py pins them so the two cannot drift apart.
FORM_TYPE_US      = 10     # parse_7501.py            (U.S. 7501, text layer)
FORM_TYPE_CA      = 20     # parse_cad.py             (CBSA BSF864, text layer)
FORM_TYPE_US_OCR  = 15     # parse_7501_line_ocr.py   (U.S. 7501, screenshot)
FORM_TYPE_CA_OCR  = 25     # parse_b3_line_ocr.py     (CBSA BSF864, screenshot)

# form type -> (module name, callable name, output envelope key). Every parser
# family maps an OCR variant onto its text-layer sibling's payload key, so
# downstream code binding ``us_data`` / ``ca_data`` stays interchangeable.
_PARSERS = {
    FORM_TYPE_US:     ("parse_7501",          "parse",        "us_data"),
    FORM_TYPE_CA:     ("parse_cad",           "parse",        "ca_data"),
    FORM_TYPE_US_OCR: ("parse_7501_line_ocr", "parse_result", "us_data"),
    FORM_TYPE_CA_OCR: ("parse_b3_line_ocr",   "parse_result", "ca_data"),
}

# The parsers are imported on first use, never at startup. Two reasons:
#   * their dependency sets barely overlap -- the text-layer parsers need
#     pdfplumber / PyMuPDF, the OCR parsers need rapidocr-onnxruntime / onnxruntime
#     / opencv -- and a deployment may only have one of the two;
#   * parse_7501.py calls sys.exit() when pdfplumber is missing, so importing it at
#     module load would kill the consumer before it could ever serve a 15/25 message.
# A missing dependency now surfaces as a per-message error naming the form type.
_PARSER_CACHE: dict[int, object] = {}

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

# All four form types are usable; each is validated on arrival.
_SUPPORTED_FORM_TYPES = tuple(_PARSERS)

# Extensions treated as "a PDF was delivered". A pre-signed OBS link keeps the real
# filename in the path and the signature in the query string, so the query has to be
# dropped before the extension is read (see _url_suffix).
PDF_SUFFIXES = (".pdf",)

# Which output envelope key each form type reports under.
_DATA_KEYS = {form_type: data_key for form_type, (_m, _f, data_key) in _PARSERS.items()}


def _url_suffix(url: str) -> str:
    """Lower-case extension of the file a URL points at, or '' when unknown.

    The path is read without its query string: a pre-signed OBS link carries the
    signature in ``?X-Amz-...``, which would otherwise be mistaken for part of the
    extension (``7501.pdf?X-Amz-Signature=...``). A link with no extension in the
    path returns '', so callers can tell "not a PDF" from "cannot tell".
    """
    path = urllib.parse.urlsplit(url).path
    return os.path.splitext(path)[1].lower()


def resolve_parser_type(form_type: int, url: str, path: str | None = None) -> int:
    """Pick the form type whose parser should actually run for this request.

    CbpFormType=15 means "U.S. 7501 delivered as a raster screenshot", so it is
    normally served by the OCR parser. When the delivered file is in fact a PDF,
    the text layer is exact and roughly 50x faster than OCR, so it is parsed by
    parse_7501.py (form type 10) instead. 25 / 20 are left alone: 20 expects a text
    layer and a caller asking for 25 explicitly wants OCR.

    The extension comes from *url* -- the original name -- and never from *path*,
    the downloaded temp file, which is always named ``..._t15_<id>.pdf`` regardless
    of what was actually fetched. Reading the temp name would route every 15 to the
    text-layer parser and silently disable OCR.

    When the URL carries no extension, the decision is made from the downloaded
    file's magic bytes if *path* is given, and otherwise keeps OCR.
    """
    if form_type != FORM_TYPE_US_OCR:
        return form_type

    suffix = _url_suffix(url)
    if suffix:
        return FORM_TYPE_US if suffix in PDF_SUFFIXES else form_type

    # No extension in the URL: sniff the downloaded bytes rather than guess.
    if path and _looks_like_pdf(path):
        return FORM_TYPE_US
    return form_type


def _looks_like_pdf(path: str) -> bool:
    """True when the file starts with the %PDF- signature (or a UTF-8 BOM before it)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(1024)
    except OSError:
        return False
    return b"%PDF-" in head[:8] or head.lstrip(b"\xef\xbb\xbf").startswith(b"%PDF-")


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


def _load_parser(form_type: int):
    """Import (once) and return the parser callable for a form type.

    Import failures are turned into a RuntimeError naming the form type and the
    missing package, so the operator sees which deployment dependency is absent
    instead of a bare ImportError or an exit from deep inside a parser module.
    """
    if form_type in _PARSER_CACHE:
        return _PARSER_CACHE[form_type]
    module_name, callable_name, _data_key = _PARSERS[form_type]
    try:
        module = importlib.import_module(module_name)
    except (ImportError, SystemExit) as e:
        # parse_7501.py calls sys.exit(1) when pdfplumber is missing, hence SystemExit.
        detail = getattr(e, "code", None) or str(e) or "dependency missing"
        raise RuntimeError(
            f"CbpFormType={form_type} needs parser '{module_name}', which could not "
            f"be loaded ({detail}). Install its dependencies -- text-layer parsers "
            "need pdfplumber/PyMuPDF, OCR parsers need rapidocr-onnxruntime, "
            "onnxruntime, opencv-python and pillow."
        ) from None
    parser = getattr(module, callable_name, None)
    if parser is None:
        raise RuntimeError(
            f"Parser module '{module_name}' has no {callable_name}() function; "
            "the deployment has a mismatched version of this file."
        )
    _PARSER_CACHE[form_type] = parser
    return parser


def parse_document(form_type: int, path: str, source: str | None = None) -> dict:
    """Parse *path* with the parser belonging to *form_type*.

    10 / 20 read the PDF text layer; 15 / 25 run the local-OCR parsers on a raster
    image (or an image-only PDF). The OCR parsers expose ``parse_result()``, the
    text-layer ones ``parse()``; either way exactly one dict comes back.
    """
    if form_type not in _PARSERS:
        raise ValueError(f"Unsupported CbpFormType: {form_type}")
    parser = _load_parser(form_type)
    if form_type in (FORM_TYPE_US_OCR, FORM_TYPE_CA_OCR):
        return parser(path, source=source)
    return parser(path)


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
        # ``source`` lets the OCR parsers record a human-readable name in their
        # IMAGE_ONLY_SOURCE warning. The downloaded temp file is named after the
        # form type and message id, so the original URL's basename is used when
        # there is one and the message id otherwise.
        source_name = Path(urllib.parse.urlsplit(url).path).name or f"ID{msg_id}"
        log.info(
            "Received ID=%s  FileNumber=%s  CbpFormType=%s  URL=%s…",
            msg_id, file_number, form_type, url[:80],
        )

        # 1. Download the file to a temp file. The temp name is always .pdf, which
        # is why the parser is chosen from the original URL below and never from it.
        suffix  = f"_t{form_type}_{msg_id}.pdf"
        tmp_pdf = tempfile.mktemp(suffix=suffix)
        download_pdf(url, tmp_pdf)
        log.info("Downloaded %d bytes → %s", Path(tmp_pdf).stat().st_size, tmp_pdf)

        # 2. Pick the parser. A requested 15 that turned out to be a PDF is served by
        # the text-layer parser: exact, and much faster than OCR on the same input.
        parser_type = resolve_parser_type(form_type, url, tmp_pdf)
        if parser_type != form_type:
            log.info(
                "CbpFormType=%s with a PDF payload → parsing with CbpFormType=%s (%s)",
                form_type, parser_type, _PARSERS[parser_type][0],
            )
        result = parse_document(parser_type, tmp_pdf, source=source_name)
        log.info("Parsed %d line items", result.get("line_item_count", 0))

        # 3. Publish result
        # The payload key names the form family it came from (us_data / ca_data)
        # so consumers can bind the field without first testing CbpFormType. Only
        # the matching key is present; the other one is omitted entirely.
        # 15 and 10 share the us_data key, so this stays true after the reroute.
        data_key = _DATA_KEYS[parser_type]
        out_msg = json.dumps(
            {
                "ID":           msg_id,
                "FileNumber":   file_number,
                # Echo what the caller asked for, not the parser that ran: the
                # reroute is ours to decide and must not change their envelope.
                "CbpFormType":  form_type,
                "status":       "ok",
                data_key:       result,
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
    except RuntimeError as e:
        # Raised when an OCR parser's dependencies are missing; the message is
        # already self-explanatory and naming the form type helps the operator.
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
