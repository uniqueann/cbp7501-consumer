#!/usr/bin/env python3
"""增强版 CAD PDF 解析器，支持从文本中提取字段值"""

import json
import re
import sys
from decimal import Decimal
import pdfplumber

def parse_enhanced(pdf_path: str) -> dict:
    """解析 CAD PDF，支持表格外的字段值"""
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            raise ValueError("Empty CAD PDF")
        
        # 获取第一页文本
        page_text = pdf.pages[0].extract_text() or ""
        lines = page_text.split('\n')
        
        # 检查是否是 CAD 表单
        if not ("Commercial Accounting Declaration" in page_text or 
                "Déclaration en détail commerciale" in page_text):
            raise ValueError("Not a recognized Canada CAD (BSF864) PDF")
        
        # 从文本中提取 CAD Transaction No (第4行通常是这个值)
        cad_transaction_no = ""
        for i, line in enumerate(lines[:10]):
            if re.match(r'^\d{5,}$', line.strip()):
                cad_transaction_no = line.strip()
                break
        
        # 提取进口商信息
        importer_name = ""
        importer_address_lines = []
        in_importer_block = False
        for i, line in enumerate(lines):
            if "Importer name" in line or "Importateur - nom" in line:
                in_importer_block = True
                continue
            if in_importer_block:
                line_stripped = line.strip()
                if line_stripped and not re.match(r'^\d+\s+[A-Z]', line):
                    if not importer_name:
                        importer_name = line_stripped
                    else:
                        importer_address_lines.append(line_stripped)
                        if re.search(r'\b[A-Z]{2}\s+[A-Z]\d[A-Z]\s?\d[A-Z]\d', line):
                            # 找到邮编行，结束
                            break
        
        # 解析地址
        importer = {
            "name": importer_name,
            "street": "",
            "city": "",
            "country": "CA",
            "zip": "",
        }
        
        if importer_address_lines:
            # 最后一行通常包含城市和邮编
            if len(importer_address_lines) > 0:
                last_line = importer_address_lines[-1]
                match = re.search(r'(.+?)\s+([A-Z]{2})\s+([A-Z]\d[A-Z]\s?\d[A-Z]\d)', last_line)
                if match:
                    importer["city"] = match.group(1).strip()
                    importer["zip"] = match.group(3).replace(' ', '')
                    # 街道是前面的行
                    importer["street"] = " ".join(importer_address_lines[:-1])
                else:
                    importer["street"] = " ".join(importer_address_lines)
        
        # 提取总计数据
        def extract_amount(text, field_number, field_name):
            """从文本中提取金额"""
            pattern = rf"{field_number}\s+{re.escape(field_name)}[^\n]*?\s+([\d,]+\.\d{{2}})"
            match = re.search(pattern, text, re.I)
            if match:
                return Decimal(match.group(1).replace(',', ''))
            return None
        
        total_value = extract_amount(page_text, 113, "Total Value for Duty")
        total_duty = extract_amount(page_text, 120, "Total Customs Duties")
        total_gst = extract_amount(page_text, 123, "Total GST")
        grand_total = extract_amount(page_text, 130, "TOTAL DUTIES & TAXES")
        
        if total_value is None or total_duty is None:
            raise ValueError("Could not parse CAD declaration totals")
        
        other_fees = Decimal('0')
        if grand_total:
            other_fees = grand_total - total_duty
        elif total_gst:
            other_fees = total_gst
        
        # 构建 header
        header = {
            "form": "CBSA BSF864",
            "currency": "CAD",
            "invoice_currency": "",
            "filer_code_entry_number": cad_transaction_no,
            "entry_type": "",
            "summary_date": "",
            "surety_number": "",
            "bond_type": "",
            "port_code": "",
            "entry_date": "",
            "importing_carrier": "",
            "country_of_origin": "",
            "import_date": "",
            "bl_number": "",
            "manufacturer_id": "",
            "exporting_country": "",
            "export_date": "",
            "foreign_port_of_lading": "",
            "us_port_of_unlading": "",
            "location_of_goods": "",
            "reference_number": "",
            "manifest_quantity": "",
            "consignee": {
                "name": "", "street": "", "city": "", "state": "", "zip": "", "customer_reference": "",
            },
            "importer_of_record": importer,
            "totals": {
                "entered_value_usd": float(total_value) if total_value else None,
                "duty_usd": float(total_duty) if total_duty else None,
                "other_fees_usd": float(other_fees) if other_fees else None,
                "grand_total_usd": float(grand_total) if grand_total else None,
                "mpf_usd": None,
                "hmf_usd": None,
            },
        }
        
        # 这里只提取表头信息，行项目需要更复杂的解析
        return {
            "header": header,
            "line_items": [],
            "line_item_count": 0,
            "note": "Enhanced parser - basic header extraction only"
        }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input.pdf> [output.json]", file=sys.stderr)
        sys.exit(1)
    
    result = parse_enhanced(sys.argv[1])
    if len(sys.argv) >= 3:
        with open(sys.argv[2], "w", encoding="utf-8") as file:
            json.dump(result, file, indent=2, ensure_ascii=False)
        print(f"Saved → {sys.argv[2]}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
