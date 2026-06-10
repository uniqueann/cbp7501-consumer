#!/usr/bin/env python3
"""
CBP Form 7501 Entry Summary Parser
Extracts structured JSON from CBP 7501 PDF files.

Usage:
    python parse_7501.py <input.pdf> [output.json]

If output path is omitted, prints JSON to stdout.
"""

import sys
import re
import json

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber not installed. Run: pip install pdfplumber --break-system-packages", file=sys.stderr)
    sys.exit(1)

# ── Commodity description lookup (HTSUS → human-readable label) ──────────────
# Extend this dict as you encounter new HTS codes across batches.
COMMODITY_DESCRIPTIONS: dict[str, str] = {
    "9506.91.0030": "GYM/PLAYGRND EXERC EQUIP;OTHER",
    "8303.00.0000": "ARMORED/REINFORCED SAFES, ETC.",
    "9506.99.6080": "OTHR GYM/SPORT EQUIP;PARTS&ACC",
    "8414.51.9090": "FANS N/PERMANENT, OUTPUT<=125W",
    "8210.00.0000": "HAND-OP MECH APPL,10KG OR LESS",
    "8516.60.6000": "COOK PLATES, GRILLERS,ETC, OTH",
    "8211.91.5030": "STEAK KNIVES W/HNDL OF RUB/PLA",
    "8419.81.9040": "OVENS ETC USED IN RESTAURANTS",
    "8509.80.5095": "ELEC DOM APPLIANCES, OTHER",
}

# ── Patterns ──────────────────────────────────────────────────────────────────
SKIP_LINES = {
    "N",
    "ENTRY SUMMARY CONTINUATION SHEET",
    "1.Filer Code/Entry Number",
}
SKIP_PREFIXES = ("32.", "27.", "Line", "29.", "A.HTSUS", "B.AD/CVD", "C $")

# 兼容两种格式：
#   新版：001 PRD ANY CTRY,EXC ...  /  007 IEEPA-RECIPROCAL EXCLUSION 232
#   旧版：001 HUMIDIFIERS,EVAPORATIVE  （无前缀，直接是商品描述）
# 匹配 001-999，后接大写字母开头的描述（排除 499/501 等 fee 行）
LINE_START_RE = re.compile(r"^(0\d{2}|[1-9]\d{2})\s+([A-Z].+)")
MANIFEST_RE   = re.compile(r"^([\d,]+)\s+(CTN|PCS|PKG|KG|LB)$", re.I)
# 格式A（旧版）：HTSUS  grossKG  netQty  unit  $value  rate  $duty
# net_quantity 可能含逗号，如 3,465.00
COMMODITY_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{4})"
    r"\s+([\d,]+)\s+KG"
    r"\s+([\d,.]+)\s+(\w+)"
    r"\s+\$([\d,]+)"
    r"\s+(.+?)"
    r"\s+\$([\d.]+)$"
)
# 格式B（新版无gross weight）：HTSUS  netQty  unit  $value  rate  $duty
# net_quantity 可能含逗号，如 3,465.00
COMMODITY_RE_NOGROSS = re.compile(
    r"^(\d{4}\.\d{2}\.\d{4})"
    r"\s+([\d,.]+)\s+(\w+)"
    r"\s+\$([\d,]+)"
    r"\s+(.+?)"
    r"\s+\$([\d.]+)$"
)
# tariff 行：HTSUS  [grossKG]  rate  $amount
TARIFF_RE = re.compile(
    r"^(\d{4}\.\d{2}\.\d{2,4})"
    r"\s+([\d,.]+\s*KG\s+)?"
    r"(FREE|\d+(?:\.\d+)?%)"
    r"\s+\$([\d.]+)"
)
FEE_RE  = re.compile(r"^(499|501)\s+-\s+(.+?)\s+([\d.]+%)\s+\$([\d.]+)")
# CHGS 行：C $80  或  C $80 999999999（末尾可能跟 Visa Number）
# CHGS 行："C $801" 或 "669 C $801"（Visa Number 紧跟 C $value）
CHGS_RE = re.compile(r"^(?:\d+\s+)?C\s+\$([\d,]+)")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _money(s: str | None) -> float | str | None:
    if not s:
        return None
    s = str(s).strip().lstrip("$").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return s


def _extract_text(pdf_path: str) -> list[str]:
    """Return all lines from every page of the PDF."""
    lines: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            lines.extend(text.splitlines())
    return lines


# ── Block splitter ────────────────────────────────────────────────────────────
# 排除表头噪音（字段标签行）
_HEADER_NOISE = re.compile(
    r"^(0\d{2}|[1-9]\d{2})\s+"
    r"(A\.HTSUS|B\.AD|B\.ADA|Line\s+No|A\.Gross|Net\s+Quantity|"
    r"28\.|29\.|30\.|31\.|32\.|33\.|34\.)"
)

def _is_real_line_start(line: str) -> bool:
    """True only for genuine line-item start rows.
    Valid: '001 HUMIDIFIERS,EVAPORATIVE'  /  '001 PRD ANY...'  /  '007 IEEPA...'
    Invalid: '499 - MPF ...'  /  '707 CTN'  /  '669 C $801'  /  header label rows
    """
    s = line.strip()
    # manifest 行（如 "887 CTN"）优先排除
    if MANIFEST_RE.match(s):
        return False
    # CHGS 行（如 "669 C $801"，Visa Number + CHGS 同行）优先排除
    if re.match(r"^\d+\s+C\s+\$[\d,]+", s):
        return False
    if not LINE_START_RE.match(s):
        return False
    if _HEADER_NOISE.match(s):
        return False
    return True


def _split_blocks(all_lines: list[str]) -> list[list[str]]:
    """
    Slide through lines and group them into per-line-item blocks.
    A manifest row (e.g. "887 CTN") immediately before a LINE_START row
    is included at the top of that block.
    """
    blocks: list[list[str]] = []
    i = 0
    while i < len(all_lines):
        if _is_real_line_start(all_lines[i]):
            start = (i - 1
                     if i > 0 and MANIFEST_RE.match(all_lines[i - 1].strip())
                     else i)
            j = i + 1
            while j < len(all_lines) and not _is_real_line_start(all_lines[j]):
                j += 1
            blocks.append(all_lines[start:j])
            i = j
        else:
            i += 1
    return blocks


# PRD ANY / IEEPA 前缀（新版 7501）
_PRD_IEEPA_RE = re.compile(r"^(PRD ANY|IEEPA)")

# ── Single-block parser ───────────────────────────────────────────────────────
def _parse_block(lines: list[str]) -> dict | None:
    # Find the actual LINE_START row within the block
    ls_idx = next(
        (i for i, l in enumerate(lines) if _is_real_line_start(l)),
        None,
    )
    if ls_idx is None:
        return None

    manifest_note = (
        lines[ls_idx - 1].strip()
        if ls_idx > 0 and MANIFEST_RE.match(lines[ls_idx - 1].strip())
        else None
    )

    m = re.match(r"^(\d{3})\s+(.*)", lines[ls_idx].strip())
    line_no_str = m.group(1)          # keep "001", "007", etc. as-is
    first_desc  = m.group(2).strip()
    is_ieepa    = "IEEPA-RECIPROCAL" in first_desc

    # 新版：first_desc 是 "PRD ANY..." / "IEEPA..."，作为 exclusion_note
    # 旧版：first_desc 是商品描述，exclusion_note 置 None，描述留给 commodity.description
    has_prefix = bool(_PRD_IEEPA_RE.match(first_desc))

    entry: dict = {
        "line_no":             line_no_str,
        "line_type":           "IEEPA_EXCLUSION_232" if is_ieepa else "STANDARD",
        "manifest_note":       manifest_note,
        "exclusion_note":      first_desc if has_prefix else None,
        "tariff_subheadings":  [],
        "commodity":           {"description": first_desc if not has_prefix else ""},
        "fees":                [],
    }

    for raw in lines[ls_idx + 1:]:
        line = raw.strip()
        if not line or line in SKIP_LINES:
            continue
        if any(line.startswith(p) for p in SKIP_PREFIXES):
            continue
        if MANIFEST_RE.match(line):
            continue

        # 499/501 fee rows
        fm = FEE_RE.match(line)
        if fm:
            entry["fees"].append({
                "code":        fm.group(1),
                "description": fm.group(2).strip(),
                "rate":        fm.group(3),
                "amount":      _money(fm.group(4)),
            })
            continue

        # CHGS row
        cm = CHGS_RE.match(line)
        if cm:
            entry["commodity"]["chgs"] = _money(cm.group(1))
            continue

        # Main commodity row — 格式A（含 gross weight）
        com = COMMODITY_RE.match(line)
        if com:
            htsus = com.group(1)
            entry["commodity"].update({
                "htsus":             htsus,
                "description":       entry["commodity"].get("description") or COMMODITY_DESCRIPTIONS.get(htsus, ""),
                "gross_weight_kg":   float(com.group(2).replace(",", "")),
                "net_quantity":      float(com.group(3).replace(",", "")),
                "net_quantity_unit": com.group(4),
                "entered_value":     _money(com.group(5)),
                "htsus_rate":        com.group(6),
                "duty_amount":       _money(com.group(7)),
            })
            continue

        # Main commodity row — 格式B（无 gross weight，如 "9403.20.0040 36.00 NO $455 FREE $0.00"）
        com2 = COMMODITY_RE_NOGROSS.match(line)
        if com2:
            htsus = com2.group(1)
            # gross_weight 从 tariff_subheadings 里取（已解析的最后一条带 KG 的行）
            gross_kg = next(
                (s["steel_weight_kg"] for s in reversed(entry["tariff_subheadings"]) if s["steel_weight_kg"]),
                None
            )
            entry["commodity"].update({
                "htsus":             htsus,
                "description":       entry["commodity"].get("description") or COMMODITY_DESCRIPTIONS.get(htsus, ""),
                "gross_weight_kg":   gross_kg,
                "net_quantity":      float(com2.group(2).replace(",", "")),
                "net_quantity_unit": com2.group(3),
                "entered_value":     _money(com2.group(4)),
                "htsus_rate":        com2.group(5),
                "duty_amount":       _money(com2.group(6)),
            })
            # gross weight 已归入 commodity，从 tariff_subheadings 清掉该字段避免重复
            for s in entry["tariff_subheadings"]:
                if s["steel_weight_kg"] == gross_kg:
                    s["gross_weight_kg"] = s.pop("steel_weight_kg")
                    break
            continue

        # Tariff sub-heading rows (9903.xx.xx)
        tm = TARIFF_RE.match(line)
        if tm:
            steel_kg = (
                float(tm.group(2).replace(" KG", "").replace(",", "").strip())
                if tm.group(2)
                else None
            )
            entry["tariff_subheadings"].append({
                "htsus":           tm.group(1),
                "steel_weight_kg": steel_kg,
                "rate":            tm.group(3),
                "amount":          _money(tm.group(4)),
            })
            continue

        # Annotation rows: append to exclusion_note (新版格式专用)
        if entry["exclusion_note"] and re.match(r"^(CN/HK|ARTICLE OF|DERIV STL)", line):
            entry["exclusion_note"] += " | " + line

    return entry


# ── Header extractor ──────────────────────────────────────────────────────────
def _parse_header(all_lines: list[str], _page0_words: list = None) -> dict:
    """
    Extract fixed header fields by scanning lines directly.
    Each field targets the specific data line where it appears in the PDF,
    avoiding cross-field contamination from full-text regex.
    """
    text = "\n".join(all_lines)

    def _search(pattern: str, default: str = "") -> str:
        m = re.search(pattern, text, re.MULTILINE)
        return m.group(1).strip() if m else default

    def _line_match(pattern: str, lines: list[str], default: str = "") -> str:
        """Search each line individually; return first match group(1)."""
        for l in lines:
            m = re.search(pattern, l)
            if m:
                return m.group(1).strip()
        return default

    # ── Filer Code/Entry Number：格式如 8G8-5538712-9 或 8S2-0153809-5
    filer_m = re.search(r"\b([A-Z0-9]{2,4}-\d{5,}-\d)\b", text)
    filer_code = filer_m.group(1) if filer_m else ""

    # ── 行5：filer_code? 01 ABI/A 日期 surety bond port entry_date
    # 兼容两种格式：
    #   有 filer_code：NXU-9997461-8 01 ABI/A 12/19/25 036 8 2704 12/09/25
    #   无 filer_code：01 ABI/A 02/26/26 856 8 2704 02/14/26
    row5_m = re.search(
        r"(?:[A-Z0-9]+-\d+-\d\s+)?(\d{2})\s+(ABI/\w+)\s+(\d{2}/\d{2}/\d{2})(?:\s+HBA)?\s+(\d+)\s+(\d)\s+(\d{4})\s+(\d{2}/\d{2}/\d{2})",
        text
    )
    entry_type    = row5_m.group(2)  if row5_m else ""
    summary_date  = row5_m.group(3)  if row5_m else ""
    surety_number = row5_m.group(4)  if row5_m else ""
    bond_type     = row5_m.group(5)  if row5_m else ""
    port_code     = row5_m.group(6)  if row5_m else ""
    entry_date    = row5_m.group(7)  if row5_m else ""

    # ── 行7：<carrier> <mode> <country_of_origin> <import_date>
    # 格式：NESTOS 11 VN 12/09/25  或  CMA CGM FORT DIAMANT 11 CN 02/14/26
    row7_m = re.search(
        r"^(.+?)\s+(\d{2})\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{2})\s*$",
        text, re.MULTILINE
    )
    importing_carrier  = row7_m.group(1) if row7_m else ""
    country_of_origin  = row7_m.group(3) if row7_m else ""
    import_date        = row7_m.group(4) if row7_m else ""

    # ── 行9：<bl_number> <manufacturer_id> <exporting_country> <export_date>
    # 格式A：CMDU GGZ2832805 CNGUAMAR4021ZHO CN 01/22/26
    # 格式B：ZIMU HAI80183820, LAX250603307 VNGRELTD9191HAN VN 11/22/25
    # 通用：最后三列固定是 manufacturer_id(无空格) country(2字母) date
    row9_m = re.search(
        r"^(.+?)\s+(\S+)\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{2})\s*$",
        text, re.MULTILINE
    )
    # row9 可能也匹配到 row7，需排除：确认 bl_number 行包含数字
    if row9_m and re.search(r"\d{6,}", row9_m.group(1)):
        bl_number         = row9_m.group(1).strip()
        manufacturer_id   = row9_m.group(2)
        exporting_country = row9_m.group(3)
        export_date       = row9_m.group(4)
    else:
        # 精确匹配：找紧跟 "13. Manufacturer ID" 标签行的下一数据行
        bl_m = re.search(r"13\..*?\n(.+?)\s+(\S+)\s+([A-Z]{2})\s+(\d{2}/\d{2}/\d{2})", text, re.DOTALL)
        bl_number         = bl_m.group(1).strip() if bl_m else ""
        manufacturer_id   = bl_m.group(2)         if bl_m else ""
        exporting_country = bl_m.group(3)         if bl_m else ""
        export_date       = bl_m.group(4)         if bl_m else ""

    # ── 行11：57078 2704
    ports_m = re.search(r"^(\d{5})\s+(\d{4})\s*$", text, re.MULTILINE)
    foreign_port    = ports_m.group(1) if ports_m else ""
    us_port         = ports_m.group(2) if ports_m else ""

    # ── location / reference
    location_m  = re.search(r"(Y\d+\s+Voyage:\s+\S+)", text)
    location    = location_m.group(1) if location_m else ""
    ref_m       = re.search(r"Customer Reference #\s*(\S+)", text)
    reference   = ref_m.group(1) if ref_m else ""

    # ── manifest quantity
    manifest_m  = re.search(r"([\d,]+\s+CTN)", text)
    manifest    = manifest_m.group(1) if manifest_m else ""

    # ── 行15：KBJ TRADING LLC  WUHAN JIU ZHOU TONG TRADING CO,.
    # 行16：Street: LIN LI...  Street: NO.95...
    # 行19：City: FONTANA State: C A Z ip: 92337-6970 City: WUHAN State: CNZip: 43000
    # consignee name：25. 标签行下一行，左半部分（到第二个公司名之前）
    # 格式：KBJ TRADING LLC  WUHAN JIU ZHOU TONG TRADING CO,.
    #   或：MWEKRE INC  MWEKRE INC
    # 用 pdfplumber 坐标精确切分 consignee / importer name
    # 第二个 "Street:" 的 x0 即为 importer 列起点
    _words = _page0_words  # 由调用方传入
    street_xs = sorted(set(
        round(w["x0"]) for w in _words if w["text"] in ("Street:", "Street")
    ))
    # 找第一行 name（紧跟 "25." 标签行）
    name_line_m = re.search(r"25\..*?\n([^\n]+)", text, re.DOTALL)
    name_line = name_line_m.group(1).strip() if name_line_m else ""

    # 策略1：完全重复（同名公司），逐字符扫
    consignee_name = ""
    importer_name  = ""
    for i in range(1, len(name_line)):
        l, r = name_line[:i].strip(), name_line[i:].strip()
        if l and r and l == r:
            consignee_name = l
            importer_name  = r
            break

    if not consignee_name:
        # 策略2：用坐标切分——找 name 行中 x0 >= importer_col_x 的第一个词
        importer_col_x = street_xs[1] if len(street_xs) >= 2 else None
        if importer_col_x:
            name_words_sorted = sorted(
                [w for w in _words if name_line and w["text"] in name_line.split()
                 and w["top"] < 250],   # 限定在页面上半部分
                key=lambda w: (round(w["top"]), w["x0"])
            )
            # 找同一行中 x0 >= importer_col_x 的第一个词，作为 importer name 起点
            left_parts, right_parts = [], []
            for w in name_words_sorted:
                if w["x0"] >= importer_col_x - 5:
                    right_parts.append(w["text"])
                else:
                    left_parts.append(w["text"])
            consignee_name = " ".join(left_parts).strip()
            importer_name  = " ".join(right_parts).strip()

    if not consignee_name:
        # 兜底：多空格切，或整行
        parts = re.split(r"\s{2,}", name_line)
        consignee_name = parts[0].strip()
        importer_name  = parts[1].strip() if len(parts) > 1 else parts[0].strip()

    # consignee street：第一个 "Street:" 到第二个 "Street:" 之间
    consignee_street_m = re.search(r"Street:\s*(.*?)\s+Street:", text)
    consignee_street = consignee_street_m.group(1).strip() if consignee_street_m else ""

    # importer street：第二个 "Street:" 之后
    importer_street_m = re.search(r"Street:.*?Street:\s*([^\n]+)", text)
    importer_street = importer_street_m.group(1).strip() if importer_street_m else ""

    # City 可能是多词（如 MORENO VALLEY），匹配到 State: 为止
    consignee_city_m = re.search(r"City:\s*([A-Z][A-Z\s]+?)\s+State:", text)
    consignee_city   = consignee_city_m.group(1).strip() if consignee_city_m else ""

    # State/Zip：兼容 "State: C A Z ip:" 和 "State: COZip:" 两种格式
    state_zip_m = re.search(
        r"State:\s*([A-Z](?:\s*[A-Z]?)*)\s*Z\s*ip:\s*([\d]{5}-?[\d]{0,4})", text
    )
    consignee_state = re.sub(r"\s+", "", state_zip_m.group(1)) if state_zip_m else ""
    consignee_zip   = state_zip_m.group(2) if state_zip_m else ""

    # importer city / zip：第二组 City/Zip
    # City 匹配多词（到 State: 或 Zip: 为止）
    city_all   = re.findall(r"City:\s*([A-Z][A-Z\s]+?)\s+(?:State:|Zip:)", text)
    zip_all    = re.findall(r"Zip:\s*([\d]{5}-?[\d]{0,4})", text)
    importer_city = city_all[1]  if len(city_all) > 1  else (city_all[0]  if city_all  else "")
    importer_zip  = zip_all[1]   if len(zip_all)  > 1  else (zip_all[0]   if zip_all   else "")

    # ── totals
    # entered_value：$ 8,051  （在 "501 - HMF $10.07 $ 8,051" 行末）
    # entered_value 在 "501 - HMF $xx.xx $ 8,051" 行末
    ev_m = re.search(r"501\s*-\s*HMF\s+\$[\d.]+\s+\$\s*([\d,]+)", text)
    if not ev_m:
        ev_m = re.search(r"35\.Total Entered Value.*?\$\s*([\d,]+)", text, re.DOTALL)
    entered_value = _money(ev_m.group(1)) if ev_m else None

    # duty：$2,568.06  单独一行紧跟 TOTALS 区
    duty_m = re.search(r"37\.Duty\s*\n\s*\$([\d,]+\.\d{2})", text)
    if not duty_m:
        duty_m = re.search(r"Ascertained Duty\s*37\.Duty\s*\n\s*\$([\d,]+\.\d{2})", text)
    if not duty_m:
        # 直接找 $2,568.06 格式独立行
        duty_m = re.search(r"^\$([\d,]+\.\d{2})\s*$", text, re.MULTILINE)
    duty = _money(duty_m.group(1)) if duty_m else None

    # other_fees：Total Other Fees 下一个 $ 值
    # other_fees 在 "Total Other Fees" 后 1~2 行，格式 "$ 43.65"
    other_m = re.search(r"Total Other Fees\s*\n\s*\$\s*([\d,]+\.[\d]{2})", text)
    if not other_m:
        other_m = re.search(r"39\.Other\s*\n\s*\$([\d.]+)", text)
    if not other_m:
        # 兜底：找独立行 "$ 43.65"
        other_m = re.search(r"^\$\s*([\d]+\.[\d]{2})\s*$", text, re.MULTILINE)
    other_fees = _money(other_m.group(1)) if other_m else None

    # grand_total：40. Total 右侧或下一行
    gt_m = re.search(r"40\.\s*Total\s*\n\s*\$([\d,]+\.\d{2})", text)
    if not gt_m:
        gt_m = re.search(r"OR owner\s+\$([\d,]+\.\d{2})", text)
    grand_total = _money(gt_m.group(1)) if gt_m else None

    mpf_m = re.search(r"499\s*-\s*MPF\s*\$([\d.]+)", text)
    hmf_m = re.search(r"501\s*-\s*HMF\s*\$([\d.]+)", text)

    return {
        "form":                   "CBP 7501",
        "filer_code_entry_number":  filer_code,
        "entry_type":             entry_type,
        "summary_date":           summary_date,
        "surety_number":          surety_number,
        "bond_type":              bond_type,
        "port_code":              port_code,
        "entry_date":             entry_date,
        "importing_carrier":      importing_carrier,
        "country_of_origin":      country_of_origin,
        "import_date":            import_date,
        "bl_number":              bl_number,
        "manufacturer_id":        manufacturer_id,
        "exporting_country":      exporting_country,
        "export_date":            export_date,
        "foreign_port_of_lading": foreign_port,
        "us_port_of_unlading":    us_port,
        "location_of_goods":      location,
        "reference_number":       reference,
        "manifest_quantity":      manifest,
        "consignee": {
            "name":   consignee_name,
            "street": consignee_street,
            "city":   consignee_city,
            "state":  consignee_state,
            "zip":    consignee_zip,
        },
        "importer_of_record": {
            "name":    importer_name,
            "street":  importer_street,
            "city":    importer_city,
            "country": exporting_country,
            "zip":     importer_zip,
        },
        "totals": {
            "entered_value_usd": entered_value,
            "duty_usd":          duty,
            "other_fees_usd":    other_fees,
            "grand_total_usd":   grand_total,
            "mpf_usd":           _money(mpf_m.group(1)) if mpf_m else None,
            "hmf_usd":           _money(hmf_m.group(1)) if hmf_m else None,
        },
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def parse(pdf_path: str) -> dict:
    """Parse a CBP 7501 PDF and return structured data as a dict."""
    all_lines  = _extract_text(pdf_path)
    # 传入第一页词坐标，用于精确切分 consignee/importer name
    _page0_words = []
    with pdfplumber.open(pdf_path) as _pdf:
        _page0_words = _pdf.pages[0].extract_words(x_tolerance=3, y_tolerance=3)
    header     = _parse_header(all_lines, _page0_words)
    blocks     = _split_blocks(all_lines)
    line_items = [p for b in blocks for p in [_parse_block(b)] if p]

    return {
        "header":          header,
        "line_items":      line_items,
        "line_item_count": len(line_items),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input.pdf> [output.json]", file=sys.stderr)
        sys.exit(1)

    pdf_path = sys.argv[1]
    result   = parse(pdf_path)

    if len(sys.argv) >= 3:
        out_path = sys.argv[2]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"Saved → {out_path}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
