import sys
sys.path.append("/root/vandili/LaTeX-OCR")
# latexocr_minimal.py
import cv2
import numpy as np
from PIL import Image
from pix2tex.run import predict  # теперь этот импорт должен работать

def preprocess_image(img: Image.Image) -> np.ndarray:
    """
    Преобразует изображение из формата PIL (RGB) в массив NumPy (BGR),
    подходящий для модели.
    """
    img_np = np.array(img)
    img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
    return img_bgr

def latex_ocr(img: Image.Image) -> str:
    """
    Вызывает функцию predict из pix2tex для распознавания формулы.
    """
    processed_img = preprocess_image(img)
    try:
        # Параметр model_name можно скорректировать в зависимости от модели
        result = predict(processed_img, model_name="latext2tex")
    except Exception as e:
        result = f"Ошибка при распознавании: {e}"
    return result
