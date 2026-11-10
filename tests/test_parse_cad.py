import json
import unittest
from pathlib import Path
from unittest.mock import patch

import parse_7501
import parse_cad


ROOT = Path(__file__).resolve().parents[1]
CAD_PDF = ROOT / "files" / "input" / "CAD-1.pdf"
US_PDF = ROOT / "files" / "input" / "C-1.pdf"


class ParseCadFieldTests(unittest.TestCase):
    def test_empty_type_does_not_use_importer_cell(self):
        table = [["1 Type\nType"], ["11 Importer BN\n12 Importer name"]]
        self.assertEqual(parse_cad._field(table, 1, "Type Code"), "")

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
        self.assertEqual(importer["zip"], "V6C 1S4")
        self.assertEqual(importer["country"], "CA")

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

    def test_undotted_classification_and_conversion_fields(self):
        table = [[
            "56 C. Ln N.\nN. Catég.\n00002",
            "58 Classification No.\nN° de classement\n9403609090",
            "60 Narrative Description\nDescription narrative\nBookshelf",
            "61 Quantity\nQuantité\n120.000",
            "62 Unit of Measure\nUnité de mesure\nNMB",
            "75 Value for Currency Conversion\nValeur pour conversion des devises\n2628.00",
            "76 Currency\nDevise\nCAD",
            "77 Exchange Rate\nTaux de change\n1.000000",
            "78 Value for Duty\nValeur en douane\n2628.00",
            "90 GST\nTPS\n131.40",
        ]]
        item = parse_cad._parse_item(table, 0, len(table))
        self.assertEqual(item["line_no"], "00002")
        self.assertEqual(item["commodity"]["htsus"], "9403.60.9090")
        self.assertEqual(item["commodity"]["entered_value"], 2628.0)
        self.assertEqual(item["value_for_currency_conversion"], 2628.0)
        self.assertEqual(item["conversion_currency"], "CAD")
        self.assertEqual(item["exchange_rate"], 1.0)
        self.assertEqual(item["fees"][0]["amount"], 131.4)

    def test_importer_in_combined_cell(self):
        cell = (
            "11 Importer BN 756552105RM0001 12 Importer name, address and telephone no.\n"
            "l’importateur\n(1019) VALUELINK CANADA INC.\n"
            "200 Granville Street, suite 2360\nVancouver, BC V6C 1S4\n2362593375"
        )
        importer = parse_cad._parse_importer([[cell]])
        self.assertEqual(importer["name"], "(1019) VALUELINK CANADA INC.")
        self.assertEqual(importer["street"], "200 Granville Street, suite 2360")
        self.assertEqual(importer["zip"], "V6C 1S4")


@unittest.skipUnless(CAD_PDF.is_file() and US_PDF.is_file(), "PDF samples unavailable")
class ParseCadTests(unittest.TestCase):
    def test_schema_and_sample_values(self):
        cad = parse_cad.parse(str(CAD_PDF))
        us = parse_7501.parse(str(US_PDF))
        self.assertEqual(cad.keys(), us.keys())
        self.assertEqual(cad["header"].keys(), us["header"].keys())
        self.assertEqual(us["header"]["currency"], "USD")
        self.assertEqual(us["header"]["invoice_currency"], "USD")
        for key in ("consignee", "importer_of_record", "totals"):
            self.assertEqual(cad["header"][key].keys(), us["header"][key].keys())
        self.assertEqual(cad["line_items"][0].keys(), us["line_items"][0].keys())
        self.assertLessEqual(
            cad["line_items"][0]["commodity"].keys(),
            us["line_items"][0]["commodity"].keys(),
        )
        self.assertEqual(cad["line_items"][0]["fees"][0].keys(), us["line_items"][0]["fees"][0].keys())
        self.assertEqual(cad["line_item_count"], 1)
        self.assertEqual(cad["header"]["entry_type"], "AB")
        self.assertEqual(cad["header"]["currency"], "CAD")
        self.assertEqual(cad["header"]["invoice_currency"], "USD")
        self.assertEqual(cad["header"]["reference_number"], "8FN0ZBHK26041179")
        self.assertEqual(cad["header"]["totals"]["entered_value_usd"], 13584.45)
        self.assertEqual(cad["header"]["totals"]["duty_usd"], 0.0)
        self.assertEqual(cad["header"]["totals"]["other_fees_usd"], 679.22)
        self.assertEqual(cad["header"]["totals"]["grand_total_usd"], 679.22)
        self.assertEqual(cad["line_items"][0]["commodity"]["htsus"], "9403.60.9090")
        self.assertEqual(cad["line_items"][0]["commodity"]["net_quantity"], 578.0)
        self.assertEqual(cad["line_items"][0]["commodity"]["entered_value"], 13584.45)
        self.assertEqual(cad["line_items"][0]["value_for_currency_conversion"], 10006.22)
        self.assertEqual(cad["line_items"][0]["conversion_currency"], "USD")
        self.assertEqual(cad["line_items"][0]["exchange_rate"], 1.3576)
        self.assertEqual(cad["line_items"][0]["fees"][0]["amount"], 679.22)
        json.dumps(cad)

    def test_non_cad_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Not a recognized Canada CAD"):
            parse_cad.parse(str(US_PDF))

    def test_non_usd_invoice_keeps_raw_amounts(self):
        original = parse_cad._field

        def non_usd_field(table, number, label, *args, **kwargs):
            if number == 40:
                return "CAD"
            return original(table, number, label, *args, **kwargs)

        with patch.object(parse_cad, "_field", side_effect=non_usd_field):
            cad = parse_cad.parse(str(CAD_PDF))
        self.assertEqual(cad["header"]["invoice_currency"], "CAD")
        self.assertEqual(cad["header"]["totals"]["grand_total_usd"], 679.22)

    def test_missing_exchange_rate_does_not_block_raw_amounts(self):
        original = parse_cad._field

        def no_rate(table, number, label, *args, **kwargs):
            if number == 77:
                return ""
            return original(table, number, label, *args, **kwargs)

        with patch.object(parse_cad, "_field", side_effect=no_rate):
            cad = parse_cad.parse(str(CAD_PDF))
        self.assertIsNone(cad["line_items"][0]["exchange_rate"])
        self.assertEqual(cad["header"]["totals"]["grand_total_usd"], 679.22)

    def test_missing_total_is_rejected(self):
        original = parse_cad._amount

        def missing_total(text, number, label):
            return None if number == 130 else original(text, number, label)

        with patch.object(parse_cad, "_amount", side_effect=missing_total):
            with self.assertRaisesRegex(ValueError, "declaration totals"):
                parse_cad.parse(str(CAD_PDF))


class ParseCadOverlayTests(unittest.TestCase):
    def test_duplicate_text_overlay_and_empty_header_values(self):
        import tempfile

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
        self.assertEqual(result["header"]["entry_type"], "")
        self.assertEqual(result["line_item_count"], 2)
        self.assertEqual([i["commodity"]["description"] for i in result["line_items"]], ["Bookshelf", "Bookshelf"])
        self.assertEqual([i["value_for_currency_conversion"] for i in result["line_items"]], [100.0, 50.0])
        self.assertEqual(result["line_items"][0]["commodity"]["duty_amount"], 5.0)
        self.assertEqual(result["line_items"][0]["fees"][0]["amount"], 5.25)
        self.assertEqual(result["line_items"][0]["commodity"]["net_quantity_unit"], "")


if __name__ == "__main__":
    unittest.main()
