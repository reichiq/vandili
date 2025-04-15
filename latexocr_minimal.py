# latexocr_minimal.py
import cv2
import numpy as np
from PIL import Image
from pix2tex.run import predict  # импорт функции для распознавания
import os

# Если необходимо, можно задать путь к модели через переменную окружения
# Например: os.environ["PIX2TEX_MODEL"] = "/root/vandili/models/latexocr.pth"

def preprocess_image(img: Image.Image) -> np.ndarray:
    """
    Преобразует изображение из формата PIL в формат numpy (BGR),
    подходящий для модели pix2tex.
    """
    # Преобразуем PIL-изображение в массив numpy (RGB)
    img_np = np.array(img)
    # Конвертируем в формат BGR (OpenCV работает с BGR)
    img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
    # При необходимости можно изменить размер или выполнить дополнительную предобработку.
    return img_bgr

def latex_ocr(img: Image.Image) -> str:
    """
    Использует функцию predict из pix2tex для распознавания формулы.
    Обратите внимание – здесь model_name можно задать, если требуется другая конфигурация.
    """
    # Предобработка: преобразуем PIL-изображение в формат, удобный для модели (например, BGR)
    processed_img = preprocess_image(img)
    
    # Вызов функции predict.
    # Параметры могут различаться в зависимости от версии pix2tex.
    # Здесь model_name может быть, например, "latext2tex" или другой (проверьте в документации).
    try:
        result = predict(processed_img, model_name="latext2tex")
    except Exception as e:
        # Если возникает ошибка, можно записать её в лог
        result = f"Ошибка при распознавании: {e}"
    return result
