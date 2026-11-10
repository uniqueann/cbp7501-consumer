import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import parse_cad

ROOT = Path(__file__).resolve().parents[1]
CAD_PDF = ROOT / "files" / "input" / "CAD-1.pdf"

CAD_HEADER = {
    "type_code", "sub_type", "submission_date", "transaction_no", "office_no",
    "mode_of_transport", "release_date", "gross_weight_kg", "carrier_code",
    "cargo_control_no", "accepted_date", "port_of_unlading", "importer_bn",
    "importer", "signed_date", "accounting_date",
}
CAD_IMPORTER = {"name", "street", "city", "zip", "country"}
CAD_TOTALS = {
    "currency", "value_for_duty", "duty", "excise_duty", "excise_tax", "surtax",
    "anti_dumping", "countervailing", "safeguard", "gst", "pst_hst",
    "provincial_alcohol_tax", "provincial_tobacco_tax", "provincial_cannabis_duty",
    "relieved", "interest", "total_amount", "total_with_interest",
    "total_duties_and_taxes", "value_for_tax", "value_for_tax_derived",
}
CAD_INVOICE = {
    "page", "invoice_no", "invoice_value", "invoice_currency",
    "purchase_order_no", "freight", "us_port_of_exit", "vendor",
}
CAD_LINE = {
    "line_no", "line_no_raw", "page", "invoice_ref", "hs_code", "hs_code_6",
    "description", "quantity", "unit_of_measure", "country_of_origin",
    "place_of_export", "direct_shipment_date", "tariff_treatment",
    "value_for_currency_conversion", "conversion_currency", "exchange_rate",
    "value_for_duty", "duty", "excise_tax", "excise_duty", "surtax",
    "anti_dumping", "safeguard", "countervailing", "value_for_tax", "gst",
    "pst_hst", "provincial_alcohol_tax", "provincial_tobacco_tax",
    "pst_cannabis", "line_total",
}


class ParseCadFieldTests(unittest.TestCase):
    def test_empty_type_does_not_use_importer_cell(self):
        table = [["1 Type\nType"], ["11 Importer BN\n12 Importer name"]]
        self.assertEqual(parse_cad._field(table, 1, "Type Code"), "")

    def test_type_variants(self):
        tables = (
            [["1 Type\nAB"]],
            [["1 Type Code\nCode du type"], ["AB"]],
            [["1 Type Code\nCode du type\nAB"]],
        )
        for table in tables:
            with self.subTest(table=table):
                self.assertEqual(parse_cad._field(table, 1, "Type Code"), "AB")

    def test_total_printed_before_label(self):
        text = "701.18\n130 Total Duties & Taxes - Total des droits et taxes"
        self.assertEqual(float(parse_cad._amount(text, 130, "TOTAL DUTIES & TAXES")), 701.18)

    def test_amount_accepts_one_decimal_place(self):
        # CAD-4 prints field 123 as "743.4" — one decimal, not two.
        text = "123 Total GST - Total de la TPS 743.4"
        self.assertEqual(parse_cad._amount(text, 123, "TOTAL GST"),
                         parse_cad.Decimal("743.4"))
        # And still handles the usual two-decimal form.
        text = "123 Total GST - Total de la TPS 1116.34"
        self.assertEqual(parse_cad._amount(text, 123, "TOTAL GST"),
                         parse_cad.Decimal("1116.34"))

    def test_bsf864_detected_without_title_block(self):
        # CAD-2 / CAD-4 print no "Commercial Accounting Declaration" banner,
        # only a revision marker; structure alone must be enough.
        untitled = "\n".join([
            "Draft Version 3122",
            "1 Type 2 W Subtype 3 Submission Date 4 CAD Transaction No. 5 Office No.",
            "58 Classification No. 113 Total Value for Duty 11 Importer BN",
        ])
        self.assertTrue(parse_cad._looks_like_bsf864([untitled]))
        # A titled declaration is still accepted.
        titled = "Commercial Accounting Declaration\n1 Type\n"
        self.assertTrue(parse_cad._looks_like_bsf864([titled]))
        # A CBP 7501 must not be mistaken for a BSF864.
        us = "\n".join([
            "ENTRY SUMMARY",
            "1.Filer Code/Entry Number",
            "A.HTSUS Gross Weight Net Quantity",
        ])
        self.assertFalse(parse_cad._looks_like_bsf864([us]))

    def test_cell_tolerates_missing_space_after_number(self):
        table = [[None, "113Total Value for Duty - Total de la valeur en douane 21677.63"]]
        self.assertIn("113Total", parse_cad._cell(table, 113))

    def test_importer_without_bn_in_combined_cell(self):
        cell = (
            "11 Importer BN 12 Importer name, address and telephone no.\n"
            "(NE) N° de Importateur - nom, adresse et n° de\n"
            "l’importateur téléphone\nVALUELINK CANADA INC\n"
            "2360-200 GRANVILLEST. PO BOX41\n"
            "POBOX41 VANCOUVER BC V6C 1S4 CANADA\n001 2362593375"
        )
        importer = parse_cad._parse_importer([["1 Type\nType"], [cell], ["17 Previous Transaction No."]])
        self.assertEqual(importer["name"], "VALUELINK CANADA INC")
        self.assertEqual(importer["zip"], "V6C1S4")
        self.assertEqual(importer["country"], "CA")

    def test_importer_in_combined_cell(self):
        cell = (
            "11 Importer BN 756552105RM0001 12 Importer name, address and telephone no.\n"
            "l’importateur\n(1019) VALUELINK CANADA INC.\n"
            "200 Granville Street, suite 2360\nVancouver, BC V6C 1S4\n2362593375"
        )
        importer = parse_cad._parse_importer([[cell]])
        self.assertEqual(importer["name"], "(1019) VALUELINK CANADA INC.")
        self.assertEqual(importer["street"], "200 Granville Street, suite 2360")
        self.assertEqual(importer["city"], "Vancouver")
        self.assertEqual(importer["zip"], "V6C1S4")
        self.assertEqual(importer["country"], "CA")

    def test_address_block_splits_city_province_postal(self):
        parsed = parse_cad._parse_address_block(
            ["VALUELINK CANADA INC.", "2360-200 GRANVILLEST. PO BOX41",
             "RICHMOND BC V6C 1S4", "Canada"]
        )
        self.assertEqual(parsed["name"], "VALUELINK CANADA INC.")
        self.assertEqual(parsed["street"], "2360-200 GRANVILLEST. PO BOX41")
        self.assertEqual(parsed["city"], "RICHMOND")
        self.assertEqual(parsed["state"], "BC")
        self.assertEqual(parsed["zip"], "V6C1S4")
        self.assertEqual(parsed["country"], "CA")

    def test_flattened_address_is_resplit(self):
        parsed = parse_cad._parse_address_block(
            ["VENDOR LTD 250 KING'S ROAD HONG KONG"]
        )
        # No postal code to anchor on, so the block stays verbatim in name.
        self.assertEqual(parsed["name"], "VENDOR LTD 250 KING'S ROAD HONG KONG")

    def test_number_tolerates_trailing_decimal_point(self):
        # CSNU prints the gross weight as "19674." — a truncated print, not junk.
        self.assertEqual(parse_cad._number("19674."), parse_cad.Decimal("19674"))
        self.assertEqual(parse_cad._number("1,234."), parse_cad.Decimal("1234"))
        self.assertEqual(parse_cad._number("465.000"), parse_cad.Decimal("465.000"))
        self.assertIsNone(parse_cad._number(""))
        self.assertIsNone(parse_cad._number("abc"))
        self.assertIsNone(parse_cad._number("12.3.4"))

    def test_header_label_variants(self):
        # Field 2 is "2 W Subtype" / "2 Ws S.typ" / "2 Ws S. typ" depending on batch.
        for heading in ("2 W Subtype\nSubtype ent.\nX",
                        "2 Ws S.typ\nStyp ent.\nX",
                        "2 Ws S. typ\nStyp ent.\nX"):
            with self.subTest(heading=heading):
                self.assertEqual(parse_cad._field([[heading]], 2, parse_cad._HEADER_FIELDS[1][1]), "X")

    def test_field3_accepts_accounting_and_submission_date(self):
        # Older batches label field 3 "Accounting Date", newer ones "Submission Date".
        for heading in ("3 Accounting Date\nDate de la déclaration\n2026-05-20",
                        "3 Submission Date\nDate de soumission\n2026-05-20"):
            with self.subTest(heading=heading):
                self.assertEqual(parse_cad._field([[heading]], 3, ("Submission Date", "Accounting Date")),
                                 "2026-05-20")

    def test_importer_bn_found_when_heading_glyphs_overlap(self):
        # CSNU prints the field 11 heading twice, so "11 Importer BN" never matches.
        scrambled = ("1 1 ( I N m E p ) o N rt ° e r d B e N 756552105RM0001 "
                     "1 2 I I m m p p o o r r t t e a r te n u a r m - e n")
        table = [["1 Type\nAB"], [scrambled]]
        result = parse_cad._parse_header(table, [])
        self.assertEqual(result["importer_bn"], "756552105RM0001")

    def test_flattened_vendor_without_postal_code_is_split_on_commas(self):
        parsed = parse_cad._parse_address_block(
            ["JUNSHENG INT'L GROUP LIMITED., , FLAT/RM 185 G/F HANG WAI IND, "
             "CENTRE NO.6 KIN TAI ST TUEN, HONGKONG, , HK, Phone: 852111111111"]
        )
        self.assertEqual(parsed["name"], "JUNSHENG INT'L GROUP LIMITED.")
        self.assertNotIn("Phone", parsed["street"])
        self.assertIn("HONGKONG", parsed["street"])

    def test_hs_code_is_full_ten_digits(self):
        self.assertEqual(
            parse_cad._format_hs_code("9403609090"),
            ("9403.60.9090", "9403.60"),
        )
        # CAD-1 prints the code pre-dotted with a stray space.
        self.assertEqual(
            parse_cad._format_hs_code("9403.60.90 90"),
            ("9403.60.9090", "9403.60"),
        )

    def test_fast_dedupe_matches_pdfplumber(self):
        import pdfplumber
        with pdfplumber.open(CAD_PDF) as pdf:
            page = pdf.pages[1]
            official = page.dedupe_chars(tolerance=1).chars
            mine = parse_cad._fast_dedupe_chars(page.chars, tolerance=1)
        self.assertEqual(
            [(c["text"], round(c["x0"], 3), round(c["top"], 3)) for c in official],
            [(c["text"], round(c["x0"], 3), round(c["top"], 3)) for c in mine],
        )


@unittest.skipUnless(CAD_PDF.is_file(), "PDF sample unavailable")
class ParseCadSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = parse_cad.parse(str(CAD_PDF))

    def test_top_level_schema(self):
        self.assertEqual(
            set(self.result),
            {"schema_version", "document", "header", "invoices", "totals",
             "line_items", "line_item_count", "warnings"},
        )
        self.assertEqual(self.result["schema_version"], parse_cad.SCHEMA_VERSION)

    def test_key_sets_are_stable(self):
        self.assertEqual(set(self.result["header"]), CAD_HEADER)
        self.assertEqual(set(self.result["header"]["importer"]), CAD_IMPORTER)
        self.assertEqual(set(self.result["totals"]), CAD_TOTALS)
        self.assertEqual(set(self.result["invoices"][0]), CAD_INVOICE)
        for item in self.result["line_items"]:
            self.assertEqual(set(item), CAD_LINE)

    def test_document_and_header_values(self):
        document = self.result["document"]
        self.assertEqual(document["form"], "CBSA BSF864")
        self.assertEqual(document["pages"], 3)

        header = self.result["header"]
        self.assertEqual(header["type_code"], "AB")
        self.assertEqual(header["office_no"], "495")
        self.assertEqual(header["mode_of_transport"], "9")
        self.assertEqual(header["gross_weight_kg"], 19859.53)
        self.assertEqual(header["carrier_code"], "9476")
        self.assertEqual(header["cargo_control_no"], "8FN0ZBHK26041179")
        self.assertEqual(header["importer_bn"], "756552105RM0001")
        self.assertEqual(header["importer"]["name"], "VALUELINK CANADA INC.")
        self.assertEqual(header["importer"]["country"], "CA")

    def test_invoice_block(self):
        invoice = self.result["invoices"][0]
        self.assertEqual(invoice["page"], 2)
        self.assertEqual(invoice["invoice_no"], "EGLV149602965632")
        self.assertEqual(invoice["invoice_value"], 10006.22)
        self.assertEqual(invoice["invoice_currency"], "USD")
        self.assertEqual(invoice["freight"], 0.0)
        self.assertIn("JUNSHENG", invoice["vendor"]["name"])

    def test_totals(self):
        totals = self.result["totals"]
        self.assertEqual(totals["currency"], "CAD")
        self.assertEqual(totals["value_for_duty"], 13584.45)
        self.assertEqual(totals["duty"], 0.0)
        self.assertEqual(totals["gst"], 679.22)
        self.assertEqual(totals["pst_hst"], 0.0)
        self.assertEqual(totals["total_amount"], 679.22)
        self.assertEqual(totals["total_duties_and_taxes"], 679.22)
        # No declaration-level value for tax; derived from the line items.
        self.assertEqual(totals["value_for_tax"], 13584.45)
        self.assertTrue(totals["value_for_tax_derived"])

    def test_line_item(self):
        item = self.result["line_items"][0]
        self.assertEqual(item["line_no"], 1)
        self.assertEqual(item["line_no_raw"], "1")
        self.assertEqual(item["hs_code"], "9403.60.9090")
        self.assertEqual(item["hs_code_6"], "9403.60")
        self.assertEqual(item["description"], "STORAGE CABINET")
        self.assertEqual(item["quantity"], 578.0)
        self.assertEqual(item["country_of_origin"], "CN")
        self.assertEqual(item["direct_shipment_date"], "2026-05-03")
        self.assertEqual(item["tariff_treatment"], "02")
        self.assertEqual(item["value_for_currency_conversion"], 10006.22)
        self.assertEqual(item["conversion_currency"], "USD")
        self.assertEqual(item["exchange_rate"], 1.3576)
        self.assertEqual(item["value_for_duty"], 13584.45)
        self.assertEqual(item["value_for_tax"], 13584.45)
        self.assertEqual(item["gst"], 679.22)
        self.assertEqual(item["line_total"], 679.22)
        self.assertEqual(item["invoice_ref"], 0)

    def test_warnings_flag_empty_header_fields_only(self):
        codes = {warning["code"] for warning in self.result["warnings"]}
        self.assertIn("EMPTY_HEADER_FIELDS", codes)
        self.assertIn("DERIVED_VALUE", codes)
        self.assertNotIn("TOTAL_MISMATCH", codes)
        self.assertNotIn("EMPTY_QUANTITY", codes)

    def test_json_serialisable(self):
        json.dumps(self.result)

    def test_non_cad_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Not a recognized Canada CAD"):
            parse_cad.parse(str(ROOT / "files" / "input" / "C-1.pdf"))


@unittest.skipUnless(CAD_PDF.is_file(), "PDF sample unavailable")
class ParseCadRobustnessTests(unittest.TestCase):
    def test_missing_grand_total_is_rejected(self):
        original = parse_cad._amount

        def missing_total(text, number, label):
            return None if number == 130 else original(text, number, label)

        with patch.object(parse_cad, "_amount", side_effect=missing_total):
            with self.assertRaisesRegex(ValueError, "declaration totals"):
                parse_cad.parse(str(CAD_PDF))

    def test_mismatched_totals_raise_a_warning_not_an_error(self):
        original = parse_cad._parse_totals

        def tweak(header_table, text, line_items, warnings):
            totals = original(header_table, text, line_items, warnings)
            totals["gst"] = 1.0
            return totals

        with patch.object(parse_cad, "_parse_totals", side_effect=tweak):
            result = parse_cad.parse(str(CAD_PDF))
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("TOTAL_MISMATCH", codes)

    def test_duplicate_text_overlay_and_empty_header_values(self):
        import pymupdf

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "overlay.pdf"
            document = pymupdf.open()
            header = document.new_page()
            header.insert_text((20, 20), "Commercial Accounting Declaration", fontsize=7)
            header.insert_text((20, 60), "1 Type", fontsize=5)
            header.insert_text((20, 68), "Type", fontsize=5)
            header.draw_rect(pymupdf.Rect(15, 50, 575, 95))
            header.draw_line((95, 50), (95, 95))
            header.draw_line((15, 75), (575, 75))
            for y, text in (
                (120, "113 Total Value for Duty - Total de la valeur en douane 150.00"),
                (140, "120 Total Customs Duties - Total des droits de douane 5.00"),
                (160, "130 Total Duties & Taxes - Total des droits et taxes 12.75"),
            ):
                header.insert_text((20, y), text, fontsize=5)
            page = document.new_page()
            page.insert_text((20, 20), "Commercial Accounting Declaration", fontsize=7)
            page.insert_text((20, 45), "Commercial Accounting Declaration", fontsize=7)
            columns = (15, 100, 190, 275, 365, 465, 575)
            labels = (
                ((56, "C. Ln N."), (58, "Classification No."), (60, "Narrative Description"),
                 (61, "Quantity"), (62, "Unit of Measure"), None),
                ((65, "Country of Origin"), (67, "Place of Export"), None, None, None, None),
                ((75, "Value for Currency Conversion"), (76, "Currency"), (77, "Exchange Rate"),
                 (78, "Value for Duty"), None, None),
                ((82, "Customs Duty"), (90, "GST"), None, None, None, None),
                ((100, "Line Total Duties & Taxes"), None, None, None, None, None),
            )
            for top, number, value, duty, gst in (
                (70, "00001", "100.00", "5.00", "5.25"),
                (245, "00002", "50.00", "", "2.50"),
            ):
                for x in columns:
                    page.draw_line((x, top), (x, top + 175))
                for y in range(top, top + 176, 35):
                    page.draw_line((columns[0], y), (columns[-1], y))
                values = (
                    (number, "9403609090", "Bookshelf", "", "", ""),
                    ("CN", "CN", "", "", "", ""),
                    (value, "CAD", "1.000000", value, "", ""),
                    (duty, gst, "", "", "", ""),
                    ("10.25" if number == "00001" else "2.50", "", "", "", "", ""),
                )
                for offset, row in enumerate(labels):
                    for col, label in enumerate(row):
                        if label is None:
                            continue
                        x, y = columns[col] + 2, top + offset * 35
                        heading = f"{label[0]} {label[1]}"
                        for _ in range(2):
                            page.insert_text((x, y + 7), heading, fontsize=5)
                            page.insert_text((x, y + 13), "Label", fontsize=5)
                        if values[offset][col]:
                            page.insert_text((x, y + 29), values[offset][col], fontsize=6)
            page.insert_text((195, 99), "Commercial Accounting Declaration", fontsize=7)
            document.save(str(source))
            document.close()
            result = parse_cad.parse(str(source))

        self.assertIsNone(result["header"]["type_code"])
        self.assertEqual(result["line_item_count"], 2)
        self.assertEqual([i["description"] for i in result["line_items"]], ["Bookshelf", "Bookshelf"])
        self.assertEqual([i["value_for_currency_conversion"] for i in result["line_items"]],
                         [100.0, 50.0])
        self.assertEqual(result["line_items"][0]["duty"], 5.0)
        self.assertEqual(result["line_items"][0]["gst"], 5.25)
        self.assertEqual(result["line_items"][0]["line_total"], 10.25)
        self.assertIsNone(result["line_items"][0]["unit_of_measure"])


if __name__ == "__main__":
    unittest.main()
