import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import consumer

ROOT = Path(__file__).resolve().parents[1]

# form type -> (module file, expected CBP_FORM_TYPE). The OCR modules cannot be
# imported here (their wheels live in a separate venv), so their constants are read
# straight out of the source with ast -- that still catches a rename or renumber.
PARSERS = (
    ("parse_7501.py",         consumer.FORM_TYPE_US),
    ("parse_cad.py",          consumer.FORM_TYPE_CA),
    ("parse_7501_line_ocr.py", consumer.FORM_TYPE_US_OCR),
    ("parse_b3_line_ocr.py",  consumer.FORM_TYPE_CA_OCR),
)


def source_form_type(filename: str) -> int:
    """Read a module-level ``CBP_FORM_TYPE = <int>`` without importing it."""
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "CBP_FORM_TYPE":
                return ast.literal_eval(node.value)
    raise AssertionError(f"{filename} has no module-level CBP_FORM_TYPE")


class FormTypeConstantsTests(unittest.TestCase):
    """consumer.py restates the form-type numbers; they must match the parsers."""

    def test_constants_match_parser_modules(self):
        for filename, expected in PARSERS:
            with self.subTest(module=filename):
                self.assertEqual(source_form_type(filename), expected)

    def test_all_four_numbers_are_distinct(self):
        types = [consumer.FORM_TYPE_US, consumer.FORM_TYPE_CA,
                 consumer.FORM_TYPE_US_OCR, consumer.FORM_TYPE_CA_OCR]
        self.assertEqual(len(set(types)), len(types), types)

    def test_expected_numbers(self):
        self.assertEqual(consumer.FORM_TYPE_US, 10)
        self.assertEqual(consumer.FORM_TYPE_CA, 20)
        self.assertEqual(consumer.FORM_TYPE_US_OCR, 15)
        self.assertEqual(consumer.FORM_TYPE_CA_OCR, 25)


class SupportedFormTypeTests(unittest.TestCase):

    def test_all_four_are_supported(self):
        self.assertEqual(set(consumer._SUPPORTED_FORM_TYPES), {10, 20, 15, 25})

    def test_each_parser_entry_names_a_real_module(self):
        for form_type, (module_name, callable_name, data_key) in consumer._PARSERS.items():
            with self.subTest(form_type=form_type):
                self.assertTrue((ROOT / f"{module_name}.py").is_file(), module_name)
                self.assertIn(data_key, ("us_data", "ca_data"))
                self.assertIn(callable_name, ("parse", "parse_result"))

    def test_ocr_variant_shares_data_key_with_text_layer_sibling(self):
        pairs = ((consumer.FORM_TYPE_US, consumer.FORM_TYPE_US_OCR),
                 (consumer.FORM_TYPE_CA, consumer.FORM_TYPE_CA_OCR))
        for text_type, ocr_type in pairs:
            with self.subTest(text_type=text_type, ocr_type=ocr_type):
                self.assertEqual(consumer._DATA_KEYS[text_type],
                                 consumer._DATA_KEYS[ocr_type])

    def test_data_keys_cover_every_supported_type(self):
        self.assertEqual(set(consumer._DATA_KEYS), set(consumer._SUPPORTED_FORM_TYPES))


class ResolveFormTypeTests(unittest.TestCase):

    def test_accepts_all_four_integers(self):
        for form_type in (10, 20, 15, 25):
            with self.subTest(form_type=form_type):
                self.assertEqual(consumer._resolve_form_type({"CbpFormType": form_type}),
                                 form_type)

    def test_accepts_numeric_strings(self):
        self.assertEqual(consumer._resolve_form_type({"CbpFormType": "15"}), 15)
        self.assertEqual(consumer._resolve_form_type({"CbpFormType": " 25 "}), 25)

    def test_absent_and_blank_default_to_us(self):
        for payload in ({}, {"CbpFormType": None}, {"CbpFormType": ""}):
            with self.subTest(payload=payload):
                self.assertEqual(consumer._resolve_form_type(payload), consumer.FORM_TYPE_US)

    def test_rejects_unsupported_integers(self):
        for value in (0, 1, 5, 11, 30, 99, -15):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Unsupported CbpFormType"):
                    consumer._resolve_form_type({"CbpFormType": value})

    def test_rejects_non_integers(self):
        for value in (15.0, True, False, "abc", "15.0", [15], {"t": 15}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Invalid CbpFormType"):
                    consumer._resolve_form_type({"CbpFormType": value})


class ParseDocumentDispatchTests(unittest.TestCase):
    """parse_document() must call each parser with the right signature."""

    def setUp(self):
        consumer._PARSER_CACHE.clear()

    def tearDown(self):
        consumer._PARSER_CACHE.clear()

    def _record(self, calls, return_value):
        def parser(*args, **kwargs):
            calls.append((args, kwargs))
            return return_value
        return parser

    def test_text_layer_parsers_get_only_the_path(self):
        for form_type in (consumer.FORM_TYPE_US, consumer.FORM_TYPE_CA):
            calls = []
            consumer._PARSER_CACHE[form_type] = self._record(calls, {"line_item_count": 0})
            with self.subTest(form_type=form_type):
                consumer.parse_document(form_type, "/tmp/x.pdf", source="a.pdf")
                self.assertEqual(calls, [(("/tmp/x.pdf",), {})])

    def test_ocr_parsers_receive_the_source_name(self):
        for form_type in (consumer.FORM_TYPE_US_OCR, consumer.FORM_TYPE_CA_OCR):
            calls = []
            consumer._PARSER_CACHE[form_type] = self._record(calls, {"line_item_count": 0})
            with self.subTest(form_type=form_type):
                consumer.parse_document(form_type, "/tmp/x.jpg", source="page.jpg")
                self.assertEqual(calls, [(("/tmp/x.jpg",), {"source": "page.jpg"})])

    def test_unsupported_type_raises_before_import(self):
        with self.assertRaisesRegex(ValueError, "Unsupported CbpFormType: 30"):
            consumer.parse_document(30, "/tmp/x.pdf")

    def test_missing_dependency_reports_the_form_type(self):
        consumer._PARSER_CACHE.clear()
        with patch.object(consumer.importlib, "import_module",
                          side_effect=ImportError("No module named 'rapidocr_onnxruntime'")):
            with self.assertRaisesRegex(RuntimeError, "CbpFormType=15"):
                consumer.parse_document(consumer.FORM_TYPE_US_OCR, "/tmp/x.jpg")

    def test_parser_exiting_at_import_is_reported_not_fatal(self):
        # parse_7501.py calls sys.exit(1) when pdfplumber is missing.
        consumer._PARSER_CACHE.clear()
        with patch.object(consumer.importlib, "import_module", side_effect=SystemExit(1)):
            with self.assertRaisesRegex(RuntimeError, "CbpFormType=10"):
                consumer.parse_document(consumer.FORM_TYPE_US, "/tmp/x.pdf")


class StartupImportTests(unittest.TestCase):
    """The consumer must import cleanly without any optional parser dependency."""

    def test_no_parser_is_imported_at_module_load(self):
        self.assertEqual(consumer._PARSER_CACHE, {})

    def test_parser_modules_are_not_in_the_consumer_namespace(self):
        # If consumer imported a parser at module level, its name would leak here.
        for module_name in ("parse_7501_line_ocr", "parse_b3_line_ocr"):
            with self.subTest(module=module_name):
                self.assertNotIn(module_name, vars(consumer))


class UrlSuffixTests(unittest.TestCase):

    def test_reads_the_extension_from_the_path(self):
        for url, expected in (
            ("https://x/a.pdf", ".pdf"),
            ("https://x/a.PDF", ".pdf"),
            ("https://x/dir.d/a.jpg", ".jpg"),
            ("https://x/a.png", ".png"),
            ("https://x/a", ""),
            ("https://x/a/", ""),
        ):
            with self.subTest(url=url):
                self.assertEqual(consumer._url_suffix(url), expected)

    def test_ignores_the_query_string_of_a_presigned_link(self):
        # A dot inside the signature must not be taken for an extension.
        url = ("https://obs.example.com/qa/7501.pdf"
               "?X-Amz-Signature=abc.def&X-Amz-Credential=ghi")
        self.assertEqual(consumer._url_suffix(url), ".pdf")

    def test_ignores_the_fragment(self):
        self.assertEqual(consumer._url_suffix("https://x/a.pdf#page=2"), ".pdf")


class ResolveParserTypeTests(unittest.TestCase):
    """CbpFormType=15 falls back to the text-layer parser when a PDF is delivered."""

    def test_pdf_url_reroutes_15_to_the_text_layer_parser(self):
        for url in ("https://x/a.pdf", "https://x/a.PDF",
                    "https://x/a.pdf?X-Amz-Signature=s"):
            with self.subTest(url=url):
                self.assertEqual(
                    consumer.resolve_parser_type(consumer.FORM_TYPE_US_OCR, url),
                    consumer.FORM_TYPE_US,
                )

    def test_raster_url_keeps_15_on_the_ocr_parser(self):
        for url in ("https://x/a.jpg", "https://x/a.JPG",
                    "https://x/a.png", "https://x/a.jpeg", "https://x/a.webp"):
            with self.subTest(url=url):
                self.assertEqual(
                    consumer.resolve_parser_type(consumer.FORM_TYPE_US_OCR, url),
                    consumer.FORM_TYPE_US_OCR,
                )

    def test_other_form_types_are_never_rerouted(self):
        # 10/20 already expect a text layer, and a caller asking for 25 wants OCR.
        for form_type in (consumer.FORM_TYPE_US, consumer.FORM_TYPE_CA,
                          consumer.FORM_TYPE_CA_OCR):
            for url in ("https://x/a.pdf", "https://x/a.jpg", "https://x/a"):
                with self.subTest(form_type=form_type, url=url):
                    self.assertEqual(consumer.resolve_parser_type(form_type, url),
                                     form_type)

    def test_extensionless_url_falls_back_to_magic_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp, "noext")
            pdf.write_bytes(b"%PDF-1.7\n%...\n")
            self.assertEqual(
                consumer.resolve_parser_type(consumer.FORM_TYPE_US_OCR,
                                             "https://x/download", str(pdf)),
                consumer.FORM_TYPE_US,
            )

            img = Path(tmp, "noext2")
            img.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
            self.assertEqual(
                consumer.resolve_parser_type(consumer.FORM_TYPE_US_OCR,
                                             "https://x/download", str(img)),
                consumer.FORM_TYPE_US_OCR,
            )

    def test_extensionless_url_without_a_file_keeps_ocr(self):
        self.assertEqual(
            consumer.resolve_parser_type(consumer.FORM_TYPE_US_OCR,
                                         "https://x/download"),
            consumer.FORM_TYPE_US_OCR,
        )

    def test_magic_byte_probe_does_not_trust_a_bare_match_far_into_the_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "f")
            path.write_bytes(b"<html>" + b"x" * 100 + b"%PDF-")
            self.assertFalse(consumer._looks_like_pdf(str(path)))

    def test_tolerates_a_utf8_bom_before_the_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "f")
            path.write_bytes(b"\xef\xbb\xbf%PDF-1.4\n")
            self.assertTrue(consumer._looks_like_pdf(str(path)))

    def test_missing_file_is_not_a_pdf(self):
        self.assertFalse(consumer._looks_like_pdf("/nonexistent/file"))


class RerouteEnvelopeTests(unittest.TestCase):
    """A rerouted 15 must keep CbpFormType=15 and the us_data key in the reply."""

    def setUp(self):
        consumer._PARSER_CACHE.clear()

    def tearDown(self):
        consumer._PARSER_CACHE.clear()

    def _run(self, url):
        published = []

        class Channel:
            def basic_publish(self, exchange, routing_key, body, properties):
                published.append(json.loads(body.decode()))

            def basic_ack(self, delivery_tag):
                pass

        class Method:
            delivery_tag = 1

        payload = {"ID": 9, "FileNumber": "F", "URL": url, "CbpFormType": 15}

        def fake_download(url_, dest, timeout=60):
            Path(dest).write_bytes(b"%PDF-1.4 fake")

        with patch.object(consumer, "download_pdf", side_effect=fake_download), \
             patch.object(consumer, "parse_document",
                          side_effect=lambda ft, path, source=None:
                          {"line_item_count": 0, "parser_type_used": ft}):
            consumer.on_message(Channel(), Method(), None, json.dumps(payload).encode())

        return published[0]

    def test_pdf_payload_publishes_us_data_and_echoes_the_requested_type(self):
        msg = self._run("https://x/a.pdf")
        self.assertEqual(msg["CbpFormType"], 15)      # caller's value, not 10
        self.assertIn("us_data", msg)
        self.assertNotIn("ca_data", msg)
        self.assertEqual(msg["us_data"]["parser_type_used"], consumer.FORM_TYPE_US)

    def test_raster_payload_uses_the_ocr_parser(self):
        msg = self._run("https://x/a.jpg")
        self.assertEqual(msg["CbpFormType"], 15)
        self.assertEqual(msg["us_data"]["parser_type_used"],
                         consumer.FORM_TYPE_US_OCR)


if __name__ == "__main__":
    unittest.main()
