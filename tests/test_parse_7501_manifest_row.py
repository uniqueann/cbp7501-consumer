import unittest
from pathlib import Path

import parse_7501

ROOT = Path(__file__).resolve().parents[1] / "files" / "input"

# A minimal declaration body: the two column-header rows, a manifest row, then one
# item. Used by the in-memory tests so they run without any sample PDF.
SYNTHETIC = [
    "A.HTSUS No. A.Gross Weight Net Quantity in C.Relationship C.IRC Rate Dollars Cents",
    "B.AD/CVD No. B.Manifest Qty. HTSUS Units D.Visa Number",
    "29 CTN",
    "001 PRDTS OF CHINA, NOTE 52",
    "9903.05.31 12.5% $144.88",
    "BLANKETS, ELECTRIC",
    "6301.10.0000 392 KG 522.00 NO $1,159 11.4% $132.13",
    "499 - Merchandise Processing Fee 0.3464% $4.01",
]


def _parse_block(lines):
    blocks = parse_7501._split_blocks_v2(lines)
    if not blocks:
        raise AssertionError("no block produced")
    return blocks[0], parse_7501._parse_block_v2(blocks[0])


class ManifestQuantityNormalisationTests(unittest.TestCase):
    """header.manifest_quantity is normalised to "<qty> CTN"."""

    def test_all_printed_carton_spellings_become_ctn(self):
        # The same field is printed as "CT", "CTN", "CTNS", "PCS" and "PKG"
        # depending on the batch, so the unit is canonicalised instead of passed
        # through.
        for raw, expected in (
            ("520 CT", "520 CTN"),      # B-1 / MRKU print the short form
            ("29 CTN", "29 CTN"),
            ("65 CTN", "65 CTN"),
            ("365 CTNS", "365 CTN"),
            ("572 CTNS", "572 CTN"),
            ("652 PCS", "652 CTN"),     # HAMU2014380 prints pieces
            ("2360 PCS", "2360 CTN"),
            ("12 PKG", "12 CTN"),       # package count
            ("12 PKGS", "12 CTN"),
            ("520 ct", "520 CTN"),       # case-insensitive
            ("520 ctns", "520 CTN"),
            ("652 pcs", "652 CTN"),
            ("12 pkg", "12 CTN"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(parse_7501.normalise_manifest_quantity(raw), expected)

    def test_thousands_separator_is_kept(self):
        # The digits used to be matched with \d{3,}, which truncated "1,320 CTN"
        # to "320 CTN" by stopping at the comma.
        self.assertEqual(parse_7501.normalise_manifest_quantity("1,320 CTN"),
                         "1,320 CTN")

    def test_short_counts_survive(self):
        # A three-digit minimum used to drop two-box-count declarations entirely.
        for raw in ("1 CTN", "9 CTN", "29 CTN", "65 CTN"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_7501.normalise_manifest_quantity(raw), raw)

    def test_empty_and_missing_stay_empty(self):
        for raw in ("", None):
            with self.subTest(raw=raw):
                self.assertEqual(parse_7501.normalise_manifest_quantity(raw), "")

    def test_weights_are_not_relabelled_as_cartons(self):
        # KG is a weight, not a package count: it must never be rewritten to CTN,
        # even though every other unit is canonicalised.
        for raw in ("5133 KG", "12,569 KG", "380.93 KG"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_7501.normalise_manifest_quantity(raw), raw)

    def test_a_pieces_count_is_not_lost_before_normalisation(self):
        # HAMU2014380 prints "652 PCS": PCS has to be extractable in the first
        # place, otherwise the normalisation above never gets a value to convert.
        self.assertEqual(parse_7501.normalise_manifest_quantity("652 PCS"), "652 CTN")
        self.assertEqual(parse_7501.normalise_manifest_quantity("300 PCS"), "300 CTN")


class NetQuantityFormatTests(unittest.TestCase):
    """line_items[].manifest_note renders "<qty> <UNIT>" as the form prints it."""

    def test_renders_two_decimals_and_the_unit(self):
        for qty, unit, expected in (
            (420.0, "NO", "420.00 NO"),
            (172.0, "NO", "172.00 NO"),
            (41.33, "DOZ", "41.33 DOZ"),
            (477.36, "KG", "477.36 KG"),
            (16658.4, "KG", "16658.40 KG"),
            (5.0, "NO", "5.00 NO"),
        ):
            with self.subTest(qty=qty, unit=unit):
                self.assertEqual(parse_7501.format_net_quantity(qty, unit), expected)

    def test_missing_quantity_or_unit_yields_none(self):
        # A line whose quantity could not be read must report None rather than
        # borrow a neighbour's value or invent "0.00".
        for qty, unit in ((None, "NO"), (420.0, None), (None, None), (420.0, "")):
            with self.subTest(qty=qty, unit=unit):
                self.assertIsNone(parse_7501.format_net_quantity(qty, unit))

    def test_zero_quantity_is_a_value_not_a_missing_one(self):
        # J-1 line 005 prints 0.00 M2; zero must survive rather than collapse to None.
        self.assertEqual(parse_7501.format_net_quantity(0, "M2"), "0.00 M2")


class OcrPathParityTests(unittest.TestCase):
    """The 7501 OCR parser must derive manifest_note the same way (form type 15).

    parse_7501_line_ocr cannot be imported here (it needs rapidocr-onnxruntime,
    which lives in a separate venv), so its source is inspected instead: it must
    call its own format_net_quantity helper rather than hard-coding None.
    """

    def setUp(self):
        # ROOT points at files/input; the parser sits two levels up.
        self.src = (ROOT.parents[1] / "parse_7501_line_ocr.py").read_text(encoding="utf-8")

    def test_it_derives_manifest_note_from_the_parsed_quantity(self):
        self.assertIn("def format_net_quantity", self.src)
        self.assertIn('"manifest_note": format_net_quantity(', self.src)

    def test_it_does_not_hard_code_manifest_note_to_none(self):
        self.assertNotIn('"manifest_note": None', self.src)


class ManifestRowPatternTests(unittest.TestCase):
    """MANIFEST_RE must recognise box counts but never the net-quantity unit."""

    def test_recognises_singular_and_plural_units(self):
        for text, qty, unit in (
            ("29 CTN", "29", "CTN"),
            ("365 CTNS", "365", "CTN"),
            ("1,320 CTN", "1,320", "CTN"),
            ("572 CTNS", "572", "CTN"),
            ("5133 KG", "5133", "KG"),
            ("10 LB", "10", "LB"),
        ):
            with self.subTest(text=text):
                match = parse_7501.MANIFEST_RE.match(text)
                self.assertIsNotNone(match, text)
                self.assertEqual(match.group(1), qty)
                # The plural "S" sits outside the capture group, so the unit is
                # reported in the singular either way.
                self.assertEqual(match.group(2).upper(), unit)

    def test_rejects_the_htsus_net_quantity_unit_no(self):
        # "NO" is the HTSUS net unit and appears INSIDE a block as the net
        # quantity. Treating it as a manifest row made the block parser skip that
        # quantity and lose it (MRSU line 004 regressed to net_quantity=None).
        for text in ("10 NO", "20 NO", "522.00 NO", "5.00 NO"):
            with self.subTest(text=text):
                self.assertIsNone(parse_7501.MANIFEST_RE.match(text))

    def test_rejects_prose_and_prices(self):
        for text in ("PRDTS OF CHINA, NOTE 52", "$1,159", "499 - Merchandise Processing Fee"):
            with self.subTest(text=text):
                self.assertIsNone(parse_7501.MANIFEST_RE.match(text))


class ManifestRowInBlockTests(unittest.TestCase):
    """A manifest row printed above its item must land in that item's block.

    The block parser only has to recognise and skip the row; the value that ends
    up in line_items[].manifest_note is derived later in parse() from the parsed
    net quantity, so these tests check that the row does not corrupt the item.
    """

    def test_row_above_the_item_is_kept_in_the_block(self):
        block, _ = _parse_block(SYNTHETIC)
        self.assertEqual(block[0].strip(), "29 CTN")

    def test_item_fields_survive_the_leading_manifest_row(self):
        _, item = _parse_block(SYNTHETIC)
        self.assertEqual(item["line_no"], "001")
        commodity = item["commodity"]
        self.assertEqual(commodity["htsus"], "6301.10.0000")
        self.assertEqual(commodity["net_quantity"], 522.0)
        self.assertEqual(commodity["net_quantity_unit"], "NO")
        self.assertEqual(commodity["entered_value"], 1159.0)

    def test_three_digit_row_is_not_mistaken_for_a_line_number(self):
        # "772 CTN" also matches ITEM_START_RE (3 digits + text), so the manifest
        # row has to be classified first or the box count becomes line 772.
        lines = ["772 CTN", "001 GOLF CLUBS, COMPLETE",
                 "9506.31.0000 1168 KG 35.00 NO $855 4.9% $41.90"]
        _, item = _parse_block(lines)
        self.assertEqual(item["line_no"], "001")
        self.assertEqual(item["commodity"]["net_quantity"], 35.0)

    def test_block_without_manifest_row_leaves_the_note_empty(self):
        lines = ["001 BLANKETS, ELECTRIC",
                 "6301.10.0000 392 KG 522.00 NO $1,159 11.4% $132.13"]
        _, item = _parse_block(lines)
        self.assertIsNone(item["manifest_note"])
        self.assertEqual(item["line_no"], "001")

    def test_trailing_manifest_row_is_not_read_as_merchandise_text(self):
        # A manifest row inside the body must be ignored rather than treated as a
        # description or quantity line.
        lines = ["001 BLANKETS, ELECTRIC", "29 CTN",
                 "6301.10.0000 392 KG 522.00 NO $1,159 11.4% $132.13"]
        _, item = _parse_block(lines)
        self.assertEqual(item["exclusion_note"], "BLANKETS, ELECTRIC")
        self.assertIsNone(item["manifest_note"])
        self.assertEqual(item["commodity"]["net_quantity"], 522.0)
        self.assertNotIn("CTN", str(item["commodity"].get("description") or ""))

    def test_lone_manifest_row_does_not_swallow_the_block(self):
        # I-1 prints a manifest row followed by "Invoice No.", not by an item. The
        # row must not make _parse_block_v2 return None silently for a real block.
        lines = ["432 CTN", "Invoice No. 0049394"]
        self.assertIsNone(parse_7501._parse_block_v2(lines))


@unittest.skipUnless((ROOT / "J-1.pdf").is_file(), "sample PDFs unavailable")
class ManifestRowOnRealDeclarationsTests(unittest.TestCase):
    """Regression cover for the declarations that used to lose their box count."""

    def _items(self, name):
        return parse_7501.parse(str(ROOT / name))["line_items"]

    def test_manifest_note_carries_the_net_quantity_per_line(self):
        # manifest_note is the line's Net Quantity in HTSUS Units, printed with two
        # decimals and its unit ("420.00 NO"). It is NOT the declaration's box
        # count -- that lives in header.manifest_quantity. The two used to be
        # conflated, which put one whole-declaration figure on the first line.
        cases = {
            "J-1.pdf": "35.00 NO",
            "K-1.pdf": "334.00 NO",
            "L-1.pdf": "334.00 NO",
            "H-1.pdf": "16658.40 KG",
            "F-1.pdf": "270.00 NO",
            "C-1.pdf": "379.00 NO",
            "WER232322121-7501.pdf": "190.00 NO",
            "202605-CAAU8232594.pdf": "365.00 NO",
            "202611-HAMU2014380-7501.pdf": "420.00 NO",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                path = ROOT / name
                if not path.is_file():
                    self.skipTest(f"Missing sample: {path}")
                items = self._items(name)
                self.assertEqual(items[0]["manifest_note"], expected)

    def test_every_line_gets_its_own_quantity_not_the_declaration_total(self):
        # HAMU2014380 prints "420.00 NO" / "172.00 NO" / "60.00 NO"; they sum to
        # the 652 box count, so a line must never inherit that total.
        path = ROOT / "202611-HAMU2014380-7501.pdf"
        if not path.is_file():
            self.skipTest(f"Missing sample: {path}")
        items = self._items("202611-HAMU2014380-7501.pdf")
        self.assertEqual([i["manifest_note"] for i in items],
                         ["420.00 NO", "172.00 NO", "60.00 NO"])
        header = parse_7501.parse(str(path))["header"]
        self.assertEqual(header["manifest_quantity"], "652 CTN")
        self.assertNotIn("652", [i["manifest_note"] for i in items])

    def test_net_quantity_is_not_lost_where_the_unit_is_no(self):
        # "10 NO" inside MRSU line 004 used to be read as a manifest row and
        # dropped from the commodity.
        path = ROOT / "202606-MRSU9844098.PDF"
        if not path.is_file():
            self.skipTest(f"Missing sample: {path}")
        items = self._items("202606-MRSU9844098.PDF")
        by_line = {item["line_no"]: item for item in items}
        self.assertEqual(by_line["004"]["commodity"]["net_quantity"], 10.0)

    def test_v1_path_reports_the_same_net_quantity_field(self):
        # A-1 is the old layout; manifest_note must mean the same thing there.
        path = ROOT / "A-1.pdf"
        if not path.is_file():
            self.skipTest(f"Missing sample: {path}")
        first = self._items("A-1.pdf")[0]
        self.assertEqual(first["manifest_note"], "65.00 NO")
        # ...while the declaration's box count stays in the header.
        header = parse_7501.parse(str(path))["header"]
        self.assertEqual(header["manifest_quantity"], "887 CTN")

    def test_header_manifest_quantity_is_populated(self):
        # header.manifest_quantity used to be "" on every declaration: the pattern
        # required three plain digits, so "29 CTN" / "65 CTN" never matched and
        # "1,320 CTN" was truncated at the comma.
        cases = {
            "B-1.pdf": "520 CTN",                    # printed as "520 CT"
            "MRKU5394955.pdf": "520 CTN",            # printed as "520 CT"
            "A-1.pdf": "887 CTN",
            "H-1.pdf": "1,320 CTN",                  # thousands separator
            "C-1.pdf": "379 CTN",                    # printed as "CTNS"
            "K-1.pdf": "572 CTN",
            "L-1.pdf": "572 CTN",
            "J-1.pdf": "772 CTN",
            "202606-MRSU9844098.PDF": "380 CTN",
            "202605-CAAU8232594.pdf": "365 CTN",
            "WER232322121-7501.pdf": "885 CTN",
            "FN2603200001-7501.pdf": "887 CTN",
            "G-1.PDF": "585 CTN",
            "F-1.pdf": "269 CTN",
            "202611-HAMU2014380-7501.pdf": "652 CTN",   # printed as "652 PCS"
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                path = ROOT / name
                if not path.is_file():
                    self.skipTest(f"Missing sample: {path}")
                header = parse_7501.parse(str(path))["header"]
                self.assertEqual(header["manifest_quantity"], expected)

    def test_header_manifest_quantity_normalises_a_pieces_count(self):
        # D-1 / E-1 print their count as PCS; it is canonicalised to CTN like every
        # other package unit.
        cases = {"D-1.pdf": "2360 CTN", "E-1.pdf": "300 CTN"}
        for name, expected in cases.items():
            with self.subTest(name=name):
                path = ROOT / name
                if not path.is_file():
                    self.skipTest(f"Missing sample: {path}")
                header = parse_7501.parse(str(path))["header"]
                self.assertEqual(header["manifest_quantity"], expected)

    def test_header_value_never_keeps_the_short_ct_unit(self):
        # Regression guard for the normalisation itself: no declaration may report
        # a bare "CT" unit once normalisation is applied.
        for name in ("B-1.pdf", "MRKU5394955.pdf", "A-1.pdf", "C-1.pdf", "J-1.pdf"):
            with self.subTest(name=name):
                path = ROOT / name
                if not path.is_file():
                    self.skipTest(f"Missing sample: {path}")
                value = parse_7501.parse(str(path))["header"]["manifest_quantity"]
                self.assertFalse(value.endswith(" CT"), value)
                self.assertFalse(value.endswith("CTNS"), value)


if __name__ == "__main__":
    unittest.main()
