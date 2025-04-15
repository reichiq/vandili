# latexocr_minimal.py

import sys
sys.path.append("/root/vandili/LaTeX-OCR")

from pix2tex.cli import LatexOCR

# ⚠️ без model_name и других аргументов, они сейчас не нужны
model = LatexOCR()

def latex_ocr(img):
    return model(img)
