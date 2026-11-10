import unittest
from pathlib import Path
from unittest.mock import patch

import parse_7501


ROOT = Path(__file__).resolve().parents[1] / "files" / "input"
SAMPLES = (
    ("A-1.pdf", "USD"),
    ("C-1.pdf", "USD"),
    ("MRKU5394955.pdf", "USD"),
    ("D-1.pdf", "USD"),
)


class Parse7501CurrencyTests(unittest.TestCase):
    def test_currency_fields_in_both_parser_formats(self):
        for name, invoice_currency in SAMPLES:
            path = ROOT / name
            with self.subTest(name=name):
                if not path.is_file():
                    self.skipTest(f"Missing sample: {path}")
                header = parse_7501.parse(str(path))["header"]
                self.assertEqual(header["currency"], "USD")
                self.assertEqual(header["invoice_currency"], invoice_currency)

    def test_line_conversion_fields_in_both_parser_formats(self):
        expected = (
            ("A-1.pdf", "USD", 1.0),
            ("C-1.pdf", "USD", 1.0),
            ("MRKU5394955.pdf", "USD", None),
            ("D-1.pdf", "", None),
        )
        for name, currency, rate in expected:
            with self.subTest(name=name):
                path = ROOT / name
                if not path.is_file():
                    self.skipTest(f"Missing sample: {path}")
                result = parse_7501.parse(str(path))
                self.assertTrue(result["line_items"])
                for item in result["line_items"]:
                    self.assertEqual(
                        item["value_for_currency_conversion"],
                        item["commodity"].get("entered_value"),
                    )
                    self.assertEqual(item["conversion_currency"], currency)
                    self.assertEqual(item["exchange_rate"], rate)

    @unittest.skipUnless((ROOT / "C-1.pdf").is_file(), "PDF sample unavailable")
    def test_line_value_backfill_respects_currency_and_original_amount(self):
        cases = (
            (None, "USD", 1.0, 5959.0),
            (None, "USD", None, 5959.0),
            (None, "EUR", 1.1, None),
            (None, "USD", 1.1, None),
            (None, "", None, None),
            (100.25, "EUR", 1.1, 100.25),
            (0.0, "USD", 1.0, 0.0),
        )
        for value, currency, rate, expected in cases:
            fields = {
                "value_for_currency_conversion": value,
                "conversion_currency": currency,
                "exchange_rate": rate,
            }
            with self.subTest(value=value, currency=currency, rate=rate):
                with patch.object(parse_7501, "_line_conversion_fields", return_value=fields):
                    result = parse_7501.parse(str(ROOT / "C-1.pdf"))
                item = result["line_items"][0]
                self.assertEqual(item["value_for_currency_conversion"], expected)
                self.assertEqual(item["commodity"]["entered_value"], 5959.0)

    def test_invoice_total_is_not_repeated_as_line_value(self):
        text = (
            "Totals for Invoice Invoice Value +/- MMV Exchange Entered Value\n"
            "ZBHK26050302 8,015.00 USD 1.00000 8,015.00 USD"
        )
        self.assertEqual(parse_7501._line_conversion_fields(text, 6), {
            "value_for_currency_conversion": None,
            "conversion_currency": "USD",
            "exchange_rate": 1.0,
        })

    def test_single_line_preserves_original_invoice_value(self):
        text = (
            "Totals for Invoice Invoice Value +/- MMV Exchange Entered Value\n"
            "INV-1 100.25 EUR 1.10000 110.00 USD"
        )
        self.assertEqual(parse_7501._line_conversion_fields(text, 1), {
            "value_for_currency_conversion": 100.25,
            "conversion_currency": "EUR",
            "exchange_rate": 1.1,
        })

    def test_missing_line_conversion_fields_stay_empty(self):
        self.assertEqual(parse_7501._line_conversion_fields("Invoice 001 (ABC)", 1), {
            "value_for_currency_conversion": None,
            "conversion_currency": "",
            "exchange_rate": None,
        })

    def test_multiple_invoices_are_not_assigned_to_all_lines(self):
        heading = "Totals for Invoice Invoice Value +/- MMV Exchange Entered Value\n"
        text = heading + "INV-1 100.00 EUR 1.10000 110.00 USD\n" + heading + "INV-2 200.00 CAD 0.75000 150.00 USD"
        self.assertEqual(parse_7501._line_conversion_fields(text, 2), {
            "value_for_currency_conversion": None,
            "conversion_currency": "",
            "exchange_rate": None,
        })

    def test_missing_invoice_currency_defaults_to_usd(self):
        self.assertEqual(parse_7501._invoice_currency("Invoice 001 (ABC)"), "USD")

    def test_explicit_invoice_currency_is_preserved(self):
        self.assertEqual(parse_7501._invoice_currency("Invoice Value EUR 100.00"), "EUR")


if __name__ == "__main__":
    unittest.main()
