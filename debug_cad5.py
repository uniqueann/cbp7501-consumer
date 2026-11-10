#!/usr/bin/env python3
import pdfplumber

pdf_path = r"D:\Downloads\202611-DFSU7422980-CAD.pdf"

with pdfplumber.open(pdf_path) as pdf:
    if pdf.pages:
        text = pdf.pages[0].extract_text() or ""
        
        # 打印前50行，看清楚结构
        lines = text.split('\n')
        for i, line in enumerate(lines[:50]):
            print(f"{i+1:3d}: {line}")
