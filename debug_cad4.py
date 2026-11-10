#!/usr/bin/env python3
import pdfplumber
import re

pdf_path = r"D:\Downloads\202611-DFSU7422980-CAD.pdf"

with pdfplumber.open(pdf_path) as pdf:
    # 获取第一页的文本
    if pdf.pages:
        text = pdf.pages[0].extract_text() or ""
        
        # 查找字段值，看看是在文本中还是表格中
        print("=== Looking for field values in page text ===")
        
        # 查找类似 "1 Type XX" 这样的模式
        type_match = re.search(r"1\s+Type\s*\n\s*Type\s*\n\s*(\S+)", text, re.I)
        if type_match:
            print(f"Type value: {type_match.group(1)}")
        
        # 查找 CAD Transaction No.
        trans_match = re.search(r"4\s+CAD Transaction No\.[^\n]*\n[^\n]*\n\s*(\S+)", text, re.I)
        if trans_match:
            print(f"CAD Transaction No.: {trans_match.group(1)}")
        
        # 打印包含数字的行，看看实际数据在哪里
        print("\n=== Lines with numeric values ===")
        for i, line in enumerate(text.split('\n')[:30]):
            if re.search(r'\d{4,}', line):
                print(f"Line {i+1}: {line}")
