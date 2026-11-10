#!/usr/bin/env python3
import pdfplumber
import re

pdf_path = r"D:\Downloads\202611-DFSU7422980-CAD.pdf"

def _field(table, number, label, start=0, end=None):
    """Read the value printed inside a numbered form cell or directly below it."""
    label_pattern = r"Type(?: Code)?" if number == 1 and label == "Type Code" else re.escape(label)
    for row_index in range(start, len(table) if end is None else end):
        for col, cell in enumerate(table[row_index]):
            if not cell or not re.match(rf"^\s*{number}\s*{label_pattern}", cell, re.I):
                continue
            lines = cell.splitlines()
            value_start = 1 if (number == 1 and label == "Type Code" and len(lines) == 2
                                and re.fullmatch(r"[A-Z]{2}", lines[1].strip())) else 2
            value = " ".join(line.strip() for line in lines[value_start:] if line.strip())
            if value:
                return value
            if row_index + 1 < len(table):
                for next_col in (col, col + 1):
                    if next_col < len(table[row_index + 1]):
                        below = (table[row_index + 1][next_col] or "").strip()
                        if below and not re.match(r"^\d{1,3}\s*[A-Za-z]", below):
                            return below
            return ""
    return ""

with pdfplumber.open(pdf_path) as pdf:
    tables = [table for page in pdf.pages for table in page.extract_tables()]
    print(f"Total tables across all pages: {len(tables)}\n")
    
    # 查找包含 "Type Code" 的表格
    print("=== Looking for header table with 'Type Code' ===")
    for i, table in enumerate(tables):
        result = _field(table, 1, "Type Code")
        if result:
            print(f"Found in table {i+1}: '{result}'")
            print(f"Table has {len(table)} rows, {len(table[0]) if table else 0} columns")
            
            # 尝试提取一些关键字段
            print(f"  CAD Transaction No.: '{_field(table, 4, 'CAD Transaction No.')}'")
            print(f"  Office No.: '{_field(table, 5, 'Office No.')}'")
            print(f"  Release Date: '{_field(table, 7, 'Release Date')}'")
            break
    else:
        print("No table with 'Type Code' found")
        print("\n=== Checking what's in table 1 ===")
        if tables:
            for j, row in enumerate(tables[0][:8]):
                print(f"Row {j+1}:")
                for k, cell in enumerate(row):
                    if cell:
                        print(f"  Col {k+1}: {cell[:100]}")
