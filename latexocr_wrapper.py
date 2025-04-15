from pix2tex.cli import LatexOCR
from PIL import Image

model = LatexOCR()

def latex_ocr(img: Image.Image) -> str:
    try:
        return model(img)
    except Exception as e:
        return f"❌ Ошибка при распознавании: {e}"
