import sys
sys.path.append("/root/vandili/LaTeX-OCR")

from pix2tex.model import LatexOCR
latex_ocr = LatexOCR(model_name="models/gemini-2.5-pro-exp-03-25")
