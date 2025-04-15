import sys
sys.path.append("/root/vandili/LaTeX-OCR")

from pix2tex.cli import LatexOCR


model = LatexOCR()

def latex_ocr(img):
    return model(img)
