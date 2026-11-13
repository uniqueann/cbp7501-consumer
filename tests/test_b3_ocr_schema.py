import json
import unittest
from pathlib import Path

import parse_b3_line_schema as ocr_schema
import parse_cad

ROOT = Path(__file__).resolve().parents[1]
CAD_PDF = ROOT / "files" / "input" / "CAD-1.pdf"

# What parse_b3_line_ocr.parse() reads off the ca-lcl-01.jpg screenshot.
CA_LCL_FIELDS = {
    56: "00002", 58: "5702.42.00 00", 60: "CARPET", 61: "285.000",
    65: "CN", 67: "CN", 69: "2026-09-30", 70: "2",
    75: "1423.67", 76: "USD", 77: "1.4212", 78: "2023.32",
    82: "283.26", 90: "115.33", 100: "398.59",
}


def _as_cells(mapping):
    """Wrap values the way the OCR script reports them (label + value cells)."""
    return {str(number): {"label": "", "value": value}
            for number, value in mapping.items()}


class ConverterParityTests(unittest.TestCase):
    """The reshaped output must not drift from parse_cad's own converters."""

    def test_number_matches_parse_cad(self):
        for raw in ("285.000", "1,234.56", "15", "", None, "abc", "19674.", "-3.5"):
            self.assertEqual(ocr_schema.number(raw), parse_cad._number(raw), raw)

    def test_money_matches_parse_cad(self):
        for raw in ("1423.67", "0.00", "", None, "junk"):
            self.assertEqual(ocr_schema.money(ocr_schema.number(raw)),
                             parse_cad._money(parse_cad._number(raw)), raw)

    def test_hs_code_matches_parse_cad(self):
        # The OCR reads the dotted code with spaces; normalisation must be identical.
        for raw in ("5702.42.00 00", "9403.60.90 90", "5702", ""):
            self.assertEqual(ocr_schema.format_hs_code(raw),
                             parse_cad._format_hs_code(raw), raw)

    def test_hs_code_normalises_ocr_spacing(self):
        self.assertEqual(ocr_schema.format_hs_code("5702.42.00 00"),
                         ("5702.42.0000", "5702.42"))


@unittest.skipUnless(CAD_PDF.is_file(), "PDF sample unavailable")
class SchemaParityTests(unittest.TestCase):
    """Key sets and order of the OCR-shaped result match the text-layer parser."""

    @classmethod
    def setUpClass(cls):
        cls.reference = parse_cad.parse(str(CAD_PDF))
        cls.result = ocr_schema.build_result(_as_cells(CA_LCL_FIELDS),
                                            source="ca-lcl-01.jpg")

    def test_top_level_keys_and_order(self):
        self.assertEqual(list(self.result), list(self.reference))

    def test_nested_key_parity(self):
        for section in ("document", "header", "totals"):
            self.assertEqual(list(self.result[section]), list(self.reference[section]),
                             section)
        self.assertEqual(list(self.result["header"]["importer"]),
                         list(self.reference["header"]["importer"]))
        self.assertEqual(list(self.result["line_items"][0]),
                         list(self.reference["line_items"][0]))

    def test_json_serialisable(self):
        json.dumps(self.result)


class LineItemValueTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.result = ocr_schema.build_result(_as_cells(CA_LCL_FIELDS),
                                            source="ca-lcl-01.jpg")
        cls.item = cls.result["line_items"][0]

    def test_schema_version_and_form(self):
        self.assertEqual(self.result["schema_version"], parse_cad.SCHEMA_VERSION)
        self.assertEqual(self.result["document"]["form"], "CBSA BSF864")

    def test_line_item_values(self):
        item = self.item
        self.assertEqual(item["line_no"], 2)
        self.assertEqual(item["line_no_raw"], "00002")
        self.assertEqual(item["hs_code"], "5702.42.0000")
        self.assertEqual(item["hs_code_6"], "5702.42")
        self.assertEqual(item["description"], "CARPET")
        self.assertEqual(item["quantity"], 285.0)
        self.assertEqual(item["country_of_origin"], "CN")
        self.assertEqual(item["place_of_export"], "CN")
        self.assertEqual(item["direct_shipment_date"], "2026-09-30")
        self.assertEqual(item["tariff_treatment"], "2")
        self.assertEqual(item["value_for_currency_conversion"], 1423.67)
        self.assertEqual(item["conversion_currency"], "USD")
        self.assertEqual(item["exchange_rate"], 1.4212)
        self.assertEqual(item["value_for_duty"], 2023.32)
        self.assertEqual(item["duty"], 283.26)
        self.assertEqual(item["gst"], 115.33)
        self.assertEqual(item["line_total"], 398.59)
        # Field 89 is blank on this declaration, so it stays null like parse_cad.
        self.assertIsNone(item["value_for_tax"])

    def test_absent_sections_are_empty_not_invented(self):
        self.assertEqual(self.result["invoices"], [])
        self.assertIsNone(self.result["header"]["type_code"])
        self.assertIsNone(self.result["header"]["importer_bn"])
        self.assertEqual(self.result["header"]["importer"]["name"], "")
        self.assertIsNone(self.result["totals"]["total_duties_and_taxes"])
        self.assertEqual(self.result["totals"]["currency"], "CAD")

    def test_warnings_use_parse_cad_codes(self):
        codes = [warning["code"] for warning in self.result["warnings"]]
        self.assertIn("EMPTY_HEADER_FIELDS", codes)
        self.assertIn("MISSING_DECLARATION_TOTAL", codes)
        self.assertIn("DERIVED_VALUE", codes)
        self.assertIn("IMAGE_ONLY_SOURCE", codes)
        derived = next(w for w in self.result["warnings"]
                       if w["code"] == "DERIVED_VALUE")
        self.assertEqual(derived["value"], 2306.58)


if __name__ == "__main__":
    unittest.main()
