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
            print(f"  Matched cell at row {row_index}, col {col}: {cell[:100]}")
            lines = cell.splitlines()
            print(f"  Lines: {lines}")
            value_start = 1 if (number == 1 and label == "Type Code" and len(lines) == 2
                                and re.fullmatch(r"[A-Z]{2}", lines[1].strip())) else 2
            print(f"  value_start: {value_start}")
            value = " ".join(line.strip() for line in lines[value_start:] if line.strip())
            print(f"  Extracted value: '{value}'")
            if value:
                return value
            if row_index + 1 < len(table):
                for next_col in (col, col + 1):
                    if next_col < len(table[row_index + 1]):
                        below = (table[row_index + 1][next_col] or "").strip()
                        if below and not re.match(r"^\d{1,3}\s*[A-Za-z]", below):
                            print(f"  Found below at col {next_col}: '{below}'")
                            return below
            return ""
    return ""

with pdfplumber.open(pdf_path) as pdf:
    tables = [table for page in pdf.pages for table in page.extract_tables()]
    
    print("=== Testing with 'Type Code' ===")
    result = _field(tables[0], 1, "Type Code")
    print(f"Result: '{result}'\n")
    
    print("=== Testing with 'Type' ===")
    # 临时改一下正则，直接匹配 "1 Type"
    for row_index in range(len(tables[0])):
        for col, cell in enumerate(tables[0][row_index]):
            if cell and re.match(r"^\s*1\s*Type\s*$", cell, re.I | re.M):
                print(f"Found '1 Type' at row {row_index}, col {col}")
                print(f"Cell content:\n{cell}")
                if row_index + 1 < len(tables[0]):
                    below = tables[0][row_index + 1][col]
                    print(f"Cell below: {below}")
