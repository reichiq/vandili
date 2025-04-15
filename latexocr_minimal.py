import sys
sys.path.append("/root/vandili/LaTeX-OCR")
# latexocr_minimal.py
import cv2
import numpy as np
from PIL import Image
from pix2tex.cli import LatexOCR

def preprocess_image(img: Image.Image) -> np.ndarray:
    """
    Преобразует изображение из PIL (RGB) в массив NumPy (BGR),
    подходящий для модели.
    """
    img_np = np.array(img)
    img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
    return img_bgr

def latex_ocr(img: Image.Image) -> str:
    """
    Создает объект OCR и распознаёт LaTeX-формулу.
    """
    processed_img = preprocess_image(img)
    try:
        # Создаем экземпляр OCR. Можно указать model_name, если требуется.
        ocr = LatexOCR(model_name="latext2tex")
        result = ocr.run(processed_img)
    except Exception as e:
        result = f"Ошибка при распознавании: {e}"
    return result
