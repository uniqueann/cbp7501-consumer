#!/usr/bin/env python3
import pdfplumber

pdf_path = r"D:\Downloads\202611-DFSU7422980-CAD.pdf"

with pdfplumber.open(pdf_path) as pdf:
    print(f"Total pages: {len(pdf.pages)}\n")
    
    # 打印第一页的文本
    if pdf.pages:
        print("=== First page text (first 2000 chars) ===")
        text = pdf.pages[0].extract_text() or ""
        print(text[:2000])
        print("\n")
        
        # 打印表格信息
        print("=== Tables on first page ===")
        tables = pdf.pages[0].extract_tables()
        print(f"Number of tables: {len(tables)}")
        
        for i, table in enumerate(tables[:3]):  # 只看前3个表格
            print(f"\n--- Table {i+1} ---")
            print(f"Rows: {len(table)}, Columns: {len(table[0]) if table else 0}")
            # 打印前几行
            for j, row in enumerate(table[:5]):
                print(f"Row {j+1}: {row}")
