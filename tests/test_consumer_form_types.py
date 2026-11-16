import ast
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


if __name__ == "__main__":
    unittest.main()
