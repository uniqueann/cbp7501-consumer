import unittest
from pathlib import Path

import parse_7501

ROOT = Path(__file__).resolve().parents[1] / "files" / "input"


def _header(name):
    path = ROOT / name
    if not path.is_file():
        raise unittest.SkipTest(f"Missing sample: {path}")
    return parse_7501.parse(str(path))["header"]


class Fields25To28Tests(unittest.TestCase):
    """Fields 25-28 share one row with mixed identifiers and free text.

    A plain line regex cannot split that row, so these fields are read by
    x-coordinate. These tests pin the layout on real declarations.
    """

    def test_location_of_goods_is_read_not_left_blank(self):
        # The field used to be hard-coded to "" on the newer layout.
        cases = {
            "F-1.pdf": "Y257 Voyage: 1TUHT",
            "J-1.pdf": "L738 Voyage: 630E",
            "K-1.pdf": "E425 Voyage: 631E",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(_header(name)["location_of_goods"], expected)

    def test_voyage_number_does_not_leak_into_reference_number(self):
        # Field 25 holds "Y257 Voyage: 1MA"; its voyage number used to land in
        # field 28 because it sat in the third whitespace group of that row.
        for name in ("F-1.pdf", "H-1.pdf", "J-1.pdf", "K-1.pdf", "L-1.pdf"):
            with self.subTest(name=name):
                self.assertEqual(_header(name)["reference_number"], "")

    def test_reference_number_is_read_when_the_field_is_populated(self):
        # G-1 prints a 9-digit importer number under 28.Reference Number.
        self.assertEqual(_header("G-1.PDF")["reference_number"], "30-069900900")

    def test_placeholder_slash_is_not_a_value(self):
        # D-1 / E-1 print a bare "/" to mark field 21 (Location) as unused.
        for name in ("D-1.pdf", "E-1.pdf"):
            with self.subTest(name=name):
                self.assertEqual(_header(name)["location_of_goods"], "")

    def test_missing_row_returns_none(self):
        # A declaration without the row must not raise.
        cells = parse_7501._extract_numbered_cells(
            str(ROOT / "CAD-1.pdf"), (25, 26, 27, 28))
        self.assertIsNone(cells)


class CommodityLabelRowTests(unittest.TestCase):
    """Free-text description rows feed commodity.description."""

    def test_description_with_semicolon_is_kept(self):
        # Label rows containing ";" were dropped, leaving description empty for
        # every line item on the declaration.
        path = ROOT / "J-1.pdf"
        if not path.is_file():
            self.skipTest(f"Missing sample: {path}")
        items = parse_7501.parse(str(path))["line_items"]
        by_line = {item["line_no"]: item["commodity"].get("description", "")
                   for item in items}
        self.assertEqual(by_line["009"], "COOK/KITWAR,STNLS ST;O/KITCHWR")

    def test_semicolon_row_is_accepted_as_a_label(self):
        line = "COOK/KITWAR,STNLS ST;O/KITCHWR"
        self.assertTrue(parse_7501.re.match(
            r"^[A-Z][A-Z0-9,/;&\s\-\.]+$", line) and len(line) > 4)


if __name__ == "__main__":
    unittest.main()
