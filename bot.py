# ---------------------- Импорты ---------------------- #
import logging
import os
import re
import random
import aiohttp
import requests
from io import BytesIO
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode, ChatType
from aiogram.types import (
    FSInputFile, Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, BufferedInputFile
)
from aiogram.client.default import DefaultBotProperties
from html import escape
from dotenv import load_dotenv
from pathlib import Path
import asyncio
import google.generativeai as genai
import tempfile
from aiogram.filters import Command
from pymorphy3 import MorphAnalyzer
from string import punctuation
from google.cloud import translate
from google.oauth2 import service_account
from docx import Document
from PyPDF2 import PdfReader
import json
import speech_recognition as sr
from pydub import AudioSegment
from gtts import gTTS
from datetime import datetime
import subprocess

# Дополнительные импорты для обработки изображений и OCR
import pytesseract
from PIL import Image

# === Новое: импорт Pix2Tex для распознавания математических формул ===
# Для работы необходимо установить пакет: pip install pix2tex
from pix2tex.cli import LatexOCR
pix2tex_model = None

def init_pix2tex_model():
    """
    Инициализирует глобальную модель Pix2Tex для распознавания LaTeX.
    Загружается лишь один раз для экономии ресурсов.
    """
    global pix2tex_model
    if pix2tex_model is None:
        logging.info("[Pix2Tex] Инициализация модели...")
        pix2tex_model = LatexOCR()

# ---------------------- Функция рендеринга LaTeX в изображение ---------------------- #
def render_latex_to_image(latex_code: str) -> str:
    """
    Принимает LaTeX-формулу (или документ) и возвращает путь к сгенерированному PNG-изображению.
    Требует установленного pdflatex и convert (ImageMagick).
    """
    with tempfile.TemporaryDirectory() as tmpdirname:
        tex_file = os.path.join(tmpdirname, "formula.tex")
        pdf_file = os.path.join(tmpdirname, "formula.pdf")
        png_file = os.path.join(tmpdirname, "formula.png")
        # Минимальный LaTeX-документ
        with open(tex_file, "w", encoding="utf-8") as f:
            f.write(r"""\documentclass[preview]{standalone}
\usepackage{amsmath, amssymb}
\begin{document}
$$
""")
            f.write(latex_code)
            f.write(r"""$$
\end{document}
""")
        try:
            subprocess.run(["pdflatex", "-interaction=nonstopmode", tex_file],
                           cwd=tmpdirname, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["convert", "-density", "300", pdf_file, "-quality", "90", png_file],
                           cwd=tmpdirname, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # Сохраняем изображение во временное место, чтобы можно было отправить
            final_path = os.path.join(tempfile.gettempdir(), "final_formula.png")
            with open(png_file, "rb") as f:
                image_data = f.read()
            with open(final_path, "wb") as f:
                f.write(image_data)
            return final_path
        except Exception as e:
            logging.error(f"Error rendering LaTeX: {e}")
            return None

# ---------------------- Загрузка переменных окружения ---------------------- #
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME")
# Приводим к строке для гарантии, что тип правильный (если вдруг значение None)
WEATHER_API_KEY = str(os.getenv("WEATHER_API_KEY"))

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
morph = MorphAnalyzer()

genai.configure(api_key=GEMINI_API_KEY)
# Изменение модели на Gemini 2.5 Pro Experimental
model = genai.GenerativeModel(model_name="models/gemini-2.5-pro-exp-03-25")

# ---------------------- Загрузка и сохранение статистики ---------------------- #
STATS_FILE = "stats.json"

def load_stats() -> dict:
    if not os.path.exists(STATS_FILE):
        return {"messages_total": 0, "files_received": 0, "commands_used": {}}
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("messages_total", 0)
            data.setdefault("files_received", 0)
            data.setdefault("commands_used", {})
            return data
    except Exception as e:
        logging.warning(f"Не удалось загрузить stats.json: {e}")
        return {"messages_total": 0, "files_received": 0, "commands_used": {}}

def save_stats():
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Не удалось сохранить stats.json: {e}")

stats = load_stats()

support_mode_users = set()
support_reply_map = {}  # {admin_msg_id: user_id}
chat_history = {}
user_documents = {}

# ---------------------- Работа с отключёнными чатами ---------------------- #
DISABLED_CHATS_FILE = "disabled_chats.json"

def load_disabled_chats() -> set:
    if not os.path.exists(DISABLED_CHATS_FILE):
        return set()
    try:
        with open(DISABLED_CHATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        logging.warning(f"[BOT] Не удалось загрузить disabled_chats: {e}")
        return set()

def save_disabled_chats(chats: set):
    try:
        with open(DISABLED_CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(chats), f)
    except Exception as e:
        logging.warning(f"[BOT] Не удалось сохранить disabled_chats: {e}")

disabled_chats = load_disabled_chats()

# ---------------------- Persistent Unique Users и Groups ---------------------- #
UNIQUE_USERS_FILE = "unique_users.json"
UNIQUE_GROUPS_FILE = "unique_groups.json"

def load_unique_users() -> set:
    if not os.path.exists(UNIQUE_USERS_FILE):
        return set()
    try:
        with open(UNIQUE_USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        logging.warning(f"Не удалось загрузить уникальных пользователей: {e}")
        return set()

def save_unique_users(users: set):
    try:
        with open(UNIQUE_USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(users), f)
    except Exception as e:
        logging.warning(f"Не удалось сохранить уникальных пользователей: {e}")

def load_unique_groups() -> set:
    if not os.path.exists(UNIQUE_GROUPS_FILE):
        return set()
    try:
        with open(UNIQUE_GROUPS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        logging.warning(f"Не удалось загрузить уникальные группы: {e}")
        return set()

def save_unique_groups(groups: set):
    try:
        with open(UNIQUE_GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(groups), f)
    except Exception as e:
        logging.warning(f"Не удалось сохранить уникальные группы: {e}")

unique_users = load_unique_users()
unique_groups = load_unique_groups()

ADMIN_ID = 1936733487
SUPPORT_PROMPT_TEXT = ("Отправьте любое сообщение (текст, фото, видео, файлы, аудио, голосовые) — всё дойдёт до поддержки.")
all_chat_ids = set()

def _register_message_stats(message: Message):
    stats["messages_total"] += 1
    save_stats()
    if message.chat.type == ChatType.PRIVATE:
        if message.from_user.id not in unique_users:
            unique_users.add(message.from_user.id)
            save_unique_users(unique_users)
    elif message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if message.chat.id not in unique_groups:
            unique_groups.add(message.chat.id)
            save_unique_groups(unique_groups)
    if message.text and message.text.startswith('/'):
        cmd = message.text.split()[0]
        stats["commands_used"][cmd] = stats["commands_used"].get(cmd, 0) + 1
        save_stats()

# ---------------------- Функция отправки ответа админа одним сообщением ---------------------- #
async def send_admin_reply_as_single_message(admin_message: Message, user_id: int):
    prefix = "<b>Ответ от поддержки:</b>"
    if admin_message.text:
        reply_text = f"{prefix}\n{admin_message.text}"
        await bot.send_message(chat_id=user_id, text=reply_text)
    elif admin_message.photo:
        caption = prefix + ("\n" + admin_message.caption if admin_message.caption else "")
        await bot.send_photo(chat_id=user_id, photo=admin_message.photo[-1].file_id, caption=caption)
    elif admin_message.voice:
        caption = prefix + ("\n" + admin_message.caption if admin_message.caption else "")
        await bot.send_voice(chat_id=user_id, voice=admin_message.voice.file_id, caption=caption)
    elif admin_message.video:
        caption = prefix + ("\n" + admin_message.caption if admin_message.caption else "")
        await bot.send_video(chat_id=user_id, video=admin_message.video.file_id, caption=caption)
    elif admin_message.document:
        caption = prefix + ("\n" + admin_message.caption if admin_message.caption else "")
        await bot.send_document(chat_id=user_id, document=admin_message.document.file_id, caption=caption)
    elif admin_message.audio:
        caption = prefix + ("\n" + admin_message.caption if admin_message.caption else "")
        await bot.send_audio(chat_id=user_id, audio=admin_message.audio.file_id, caption=caption)
    else:
        await bot.send_message(chat_id=user_id, text=f"{prefix}\n[Сообщение в неподдерживаемом формате]")

# ---------------------- Морфологическая нормализация для валют и городов ---------------------- #
def normalize_currency_rus(word: str) -> str:
    word_clean = word.strip().lower()
    parsed = morph.parse(word_clean)
    if not parsed:
        return word_clean
    return parsed[0].normal_form

def normalize_city_name(raw_city: str) -> str:
    words = raw_city.split()
    norm_words = []
    for w in words:
        w_clean = w.strip(punctuation).lower()
        parsed = morph.parse(w_clean)
        if not parsed:
            norm_words.append(w_clean)
        else:
            best = parsed[0]
            if best.normal_form == w_clean or len(best.normal_form) < 2:
                norm_words.append(w_clean)
            else:
                norm_words.append(best.normal_form)
    return " ".join(norm_words)

# ---------------------- Словарь базовых форм валют (расширенный) ---------------------- #
CURRENCY_SYNONYMS = {
    "доллар": "USD", "доллары": "USD", "долларов": "USD",
    "долар": "USD",  # для опечатки с одной "л"
    "евро": "EUR",
    "рубль": "RUB", "рубли": "RUB", "рублей": "RUB",
    "юань": "CNY", "юани": "CNY",
    "иена": "JPY", "иены": "JPY", "йена": "JPY",
    "вон": "KRW", "воны": "KRW",
    "сум": "UZS", "сума": "UZS", "сумы": "UZS", "сумов": "UZS",
    "тенге": "KZT",
    "$": "USD",
    "€": "EUR",
    "₽": "RUB",
    "¥": "JPY",
}

async def get_floatrates_rate(from_curr: str, to_curr: str) -> float:
    from_curr = from_curr.lower()
    to_curr = to_curr.lower()
    url = f"https://www.floatrates.com/daily/{from_curr}.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logging.warning(f"Floatrates вернул статус {resp.status} для {url}")
                    return None
                data = await resp.json()
    except Exception as e:
        logging.error(f"Ошибка при запросе к Floatrates: {e}")
        return None
    if to_curr not in data:
        return None
    rate = data[to_curr].get("rate")
    if rate is None:
        return None
    return float(rate)

async def get_exchange_rate(amount: float, from_curr: str, to_curr: str) -> str:
    rate = await get_floatrates_rate(from_curr, to_curr)
    if rate is None:
        return None
    result = amount * rate
    today = datetime.now().strftime("%Y-%m-%d")
    return (f"Курс {amount:.0f} {from_curr.upper()} – {result:.2f} {to_curr.upper()} на {today} 😊\n"
            "Курс в банках и на биржах может отличаться.")

# Новый универсальный шаблон для запроса курса валют
EXCHANGE_PATTERN = re.compile(
    r"(?i)(\d+(?:[.,]\d+)?)[ \t]+([a-zа-яё$€₽¥]+)(?:\s+(?:в|to))?\s+([a-zа-яё$€₽¥]+)"
)

# ---------------------- Функции для погоды ---------------------- #
async def do_geocoding_request(name: str) -> dict:
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={name}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logging.warning(f"Ошибка геокодинга для {name}: статус {resp.status}")
                    return None
                geo_data = await resp.json()
    except Exception as e:
        logging.error(f"Ошибка запроса геокодинга: {e}")
        return None
    if "results" not in geo_data or not geo_data["results"]:
        return None
    best = geo_data["results"][0]
    return {"lat": best["latitude"], "lon": best["longitude"], "timezone": best.get("timezone", "Europe/Moscow")}

def simple_transliterate(s: str) -> str:
    translit_map = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd',
        'е': 'e', 'ё': 'yo','ж': 'zh','з': 'z', 'и': 'i',
        'й': 'j', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n',
        'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't',
        'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts','ч': 'ch',
        'ш': 'sh','щ': 'sch','ъ': '',  'ы': 'y', 'ь': '',
        'э': 'e', 'ю': 'yu','я': 'ya'
    }
    result = []
    for ch in s:
        lower_ch = ch.lower()
        result.append(translit_map.get(lower_ch, ch))
    return "".join(result)

async def geocode_city(city_name: str) -> dict:
    data = await do_geocoding_request(city_name)
    if data:
        return data
    try:
        project_id = "gen-lang-client-0588633435"
        location = "global"
        parent = f"projects/{project_id}/locations/{location}"
        response = translate_client.translate_text(
            parent=parent,
            contents=[city_name],
            mime_type="text/plain",
            source_language_code="ru",
            target_language_code="en",
        )
        en_city = response.translations[0].translated_text
        data = await do_geocoding_request(en_city)
        if data:
            return data
    except Exception as e:
        logging.warning(f"Не удалось перевести город {city_name}: {e}")
    translit_city = simple_transliterate(city_name)
    data = await do_geocoding_request(translit_city)
    return data

def format_condition(condition_text: str) -> str:
    weather_emojis = {
        "ясно": "☀️",
        "солнечно": "☀️",
        "солнечная": "☀️",
        "облачно": "☁️",
        "пасмурно": "☁️",
        "туман": "🌫️",
        "дождь": "🌧️",
        "ливень": "🌦️",
        "снег": "🌨️",
        "гроза": "⛈️"
    }
    lower = condition_text.lower()
    for key, emoji in weather_emojis.items():
        if key in lower:
            return f"{condition_text.capitalize()} {emoji}"
    return f"{condition_text.capitalize()} 🙂"

async def get_weather_info(city: str, days: int = 1, mode: str = "") -> str:
    base_url = "http://api.weatherapi.com/v1/forecast.json"
    params = {"key": WEATHER_API_KEY, "q": city, "days": max(days, 1), "lang": "ru", "aqi": "no", "alerts": "no"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(base_url, params=params) as resp:
                if resp.status != 200:
                    logging.warning(f"Ошибка получения погоды: статус {resp.status}")
                    return "Не удалось получить данные о погоде."
                data = await resp.json()
    except Exception as e:
        logging.error(f"Ошибка запроса погоды: {e}")
        return "Ошибка при получении данных о погоде."
    if days == 1 and not mode:
        current = data.get("current", {})
        if not current:
            return "Не удалось получить текущую погоду."
        condition_text = current.get("condition", {}).get("text", "Неизвестно")
        formatted_condition = format_condition(condition_text)
        temp = current.get("temp_c", "?")
        wind = current.get("wind_kph", "?")
        return f"Погода в {city.capitalize()} сейчас: {formatted_condition}, температура {temp}°C, ветер {wind} км/ч."
    else:
        forecast_days = data.get("forecast", {}).get("forecastday", [])
        if mode in ["завтра", "послезавтра"]:
            index = 1 if mode == "завтра" else 2
            if len(forecast_days) > index:
                day_info = forecast_days[index]
                date = day_info.get("date", "")
                day = day_info.get("day", {})
                condition_text = day.get("condition", {}).get("text", "Неизвестно")
                formatted_condition = format_condition(condition_text)
                mintemp = day.get("mintemp_c", "?")
                maxtemp = day.get("maxtemp_c", "?")
                return f"{date}: {formatted_condition}, температура от {mintemp}°C до {maxtemp}°C."
            else:
                return "Нет данных на выбранный день."
        else:
            forecast_lines = [f"<b>Прогноз погоды в {city.capitalize()}:</b>"]
            available_days = min(len(forecast_days), days)
            for i in range(available_days):
                day_info = forecast_days[i]
                date = day_info.get("date", "")
                day = day_info.get("day", {})
                condition_text = day.get("condition", {}).get("text", "Неизвестно")
                formatted_condition = format_condition(condition_text)
                mintemp = day.get("mintemp_c", "?")
                maxtemp = day.get("maxtemp_c", "?")
                forecast_lines.append(f"• {date}: {formatted_condition}, от {mintemp}°C до {maxtemp}°C")
            return "\n".join(forecast_lines)

# ---------------------- Функция для отправки голосового ответа ---------------------- #
async def send_voice_message(chat_id: int, text: str):
    clean_text = re.sub(r'<[^>]+>', '', text or "")
    if not clean_text.strip():
        clean_text = "Нет данных для голосового ответа."
    tts = gTTS(clean_text, lang='ru')
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmpf:
        tts.save(tmpf.name)
        mp3_path = tmpf.name
    audio = AudioSegment.from_file(mp3_path, format="mp3")
    ogg_path = mp3_path.replace(".mp3", ".ogg")
    audio.export(ogg_path, format="ogg")
    os.remove(mp3_path)
    await bot.send_voice(chat_id=chat_id, voice=FSInputFile(ogg_path, filename="voice.ogg"))
    os.remove(ogg_path)

def thread(message: Message) -> dict:
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP] and message.message_thread_id:
        return {"message_thread_id": message.message_thread_id}
    return {}

# ---------------------- Обработчик фотографий (если пользователь присылает фото) ---------------------- #
@dp.message(lambda message: message.photo is not None and not message.document)
async def handle_photo(message: Message):
    _register_message_stats(message)
    # Вместо упоминания Pix2Tex — просто "Обрабатываю изображение... ⏳"
    status_message = await message.answer("Обрабатываю изображение... ⏳")
    try:
        # Инициализируем модель (в коде остаётся, но в сообщениях для пользователя не упоминаем)
        init_pix2tex_model()
        file = await bot.get_file(message.photo[-1].file_id)
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                image_bytes = await resp.read()
        tmp_path = os.path.join(tempfile.gettempdir(), "incoming_image.png")
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
        image = Image.open(tmp_path).convert("RGB")
        latex_code = pix2tex_model.predict(image)
        os.remove(tmp_path)
        if latex_code and latex_code.strip():
            latex_img_path = render_latex_to_image(latex_code)
            if latex_img_path:
                caption_msg = f"Распознана формула:\n<code>{latex_code}</code>\n\nСчитал, задавайте ваш вопрос."
                await bot.send_photo(
                    chat_id=message.chat.id,
                    photo=FSInputFile(latex_img_path, filename="formula.png"),
                    caption=caption_msg
                )
                os.remove(latex_img_path)
                # Удаляем сообщение "Обрабатываю изображение..."
                await status_message.delete()
            else:
                await message.answer("Код LaTeX получен, но не удалось отрисовать формулу. Попробуйте повторить.")
                await status_message.delete()
        else:
            await message.answer("Формула не обнаружена или не распознана. Задавайте ваш вопрос.")
            await status_message.delete()
    except Exception as e:
        logging.error(f"Ошибка при обработке фото: {e}")
        await message.answer("Произошла ошибка при обработке изображения.")
        await status_message.delete()

# ---------------------- Обработчики команд ---------------------- #
from aiogram.filters import CommandObject

@dp.message(Command("start", prefix="/!"))
async def cmd_start(message: Message, command: CommandObject):
    _register_message_stats(message)
    all_chat_ids.add(message.chat.id)
    greet = """Привет! Я <b>VAI</b> — твой интеллектуальный помощник 🤖

•🔊Я могу отвечать не только текстом, но и голосовыми сообщениями. Скажи "ответь голосом" или "ответь войсом".
•📄Читаю PDF, DOCX, TXT и .py-файлы — просто отправь мне файл.
•❓Отвечаю на вопросы по содержимому файла.
•👨‍💻Помогаю с кодом — напиши #рефактор и вставь код.
•🏞Показываю изображения по ключевым словам.
•☀️Погода: спроси "погода в Москве" или "погода в Варшаве на 3 дня" 
•💱Курс валют: узнай курс "100 долларов в рублях", "100 USD в KRW" и т.д. 
•🔎Поддерживаю команды /help и режим поддержки.

Всегда на связи!"""
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if message.chat.id in disabled_chats:
            disabled_chats.remove(message.chat.id)
            save_disabled_chats(disabled_chats)
            logging.info(f"[BOT] Бот снова включён в группе {message.chat.id}")
        await message.answer("Бот включён ✅")
        await message.answer(greet)
        return
    await message.answer(greet)

@dp.message(Command("stop", prefix="/!"))
async def cmd_stop(message: Message, command: CommandObject):
    _register_message_stats(message)
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        disabled_chats.add(message.chat.id)
        save_disabled_chats(disabled_chats)
        logging.info(f"[BOT] Бот отключён в группе {message.chat.id}")
        await message.answer("Бот отключён в группе 🚫")
    else:
        await message.answer("Бот отключён 🚫")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    _register_message_stats(message)
    all_chat_ids.add(message.chat.id)
    if message.chat.type == ChatType.PRIVATE:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✉️ Написать в поддержку", callback_data="support_request")]]
        )
        await bot.send_message(chat_id=message.chat.id,
                               text="Если возник вопрос или хочешь сообщить об ошибке — напиши мне в личку:",
                               reply_markup=keyboard, **thread(message))
    else:
        private_url = f"https://t.me/{BOT_USERNAME}?start=support"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✉️ Написать в поддержку", url=private_url)]]
        )
        await bot.send_message(chat_id=message.chat.id,
                               text="Если возник вопрос или хочешь сообщить об ошибке — напиши мне в личку:",
                               reply_markup=keyboard, **thread(message))

@dp.message(Command("adminstats"))
async def cmd_adminstats(message: Message):
    _register_message_stats(message)
    if message.from_user.id != ADMIN_ID:
        return
    total_msgs = stats["messages_total"]
    unique_users_count = len(unique_users)
    files_received = stats["files_received"]
    cmd_usage = stats["commands_used"]
    if not cmd_usage:
        top_commands = []
    else:
        top_commands = sorted(cmd_usage.items(), key=lambda x: x[1], reverse=True)[:3]
    text = (f"📊 <b>Статистика бота</b>\n\n"
            f"Всего сообщений: {total_msgs}\n"
            f"Уникальных пользователей: {unique_users_count}\n"
            f"Получено файлов: {files_received}\n\n")
    if top_commands:
        text += "Топ команд:\n"
        for cmd, cnt in top_commands:
            text += f"  {cmd}: {cnt}\n"
    else:
        text += "Команды ещё не использовались."
    await message.answer(text)

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    _register_message_stats(message)
    if message.from_user.id != ADMIN_ID:
        return
    broadcast_prefix = "<b>Admin Message:</b>"
    if message.reply_to_message:
        broadcast_msg = message.reply_to_message
    else:
        text_parts = message.text.split(maxsplit=1)
        if len(text_parts) < 2:
            await message.answer("Нет текста для рассылки.")
            return
        broadcast_text = text_parts[1]
        broadcast_msg = None
    recipients = unique_users.union(unique_groups)
    for recipient in recipients:
        try:
            if broadcast_msg:
                if broadcast_msg.text:
                    await bot.send_message(chat_id=recipient, text=f"{broadcast_prefix}\n{broadcast_msg.text}")
                elif broadcast_msg.photo:
                    caption = broadcast_msg.caption or ""
                    caption = f"{broadcast_prefix}\n{caption}"
                    await bot.send_photo(chat_id=recipient, photo=broadcast_msg.photo[-1].file_id, caption=caption)
                elif broadcast_msg.video:
                    caption = broadcast_msg.caption or ""
                    caption = f"{broadcast_prefix}\n{caption}"
                    await bot.send_video(chat_id=recipient, video=broadcast_msg.video.file_id, caption=caption)
                elif broadcast_msg.voice:
                    caption = broadcast_msg.caption or ""
                    caption = f"{broadcast_prefix}\n{caption}"
                    await bot.send_voice(chat_id=recipient, voice=broadcast_msg.voice.file_id, caption=caption)
                elif broadcast_msg.document:
                    caption = broadcast_msg.caption or ""
                    caption = f"{broadcast_prefix}\n{caption}"
                    await bot.send_document(chat_id=recipient, document=broadcast_msg.document.file_id, caption=caption)
                elif broadcast_msg.audio:
                    caption = broadcast_msg.caption or ""
                    caption = f"{broadcast_prefix}\n{caption}"
                    await bot.send_audio(chat_id=recipient, audio=broadcast_msg.audio.file_id, caption=caption)
                else:
                    await bot.send_message(chat_id=recipient, text=f"{broadcast_prefix}\n[Сообщение в неподдерживаемом формате]")
            else:
                await bot.send_message(chat_id=recipient, text=f"{broadcast_prefix}\n{broadcast_text}")
        except Exception as e:
            logging.warning(f"[BROADCAST] Ошибка при отправке в чат {recipient}: {e}")
    await message.answer("Рассылка завершена.")

@dp.callback_query(F.data == "support_request")
async def handle_support_click(callback: CallbackQuery):
    await callback.answer()
    support_mode_users.add(callback.from_user.id)
    await callback.message.answer(SUPPORT_PROMPT_TEXT)

@dp.message(lambda message: message.voice is not None)
async def handle_voice_message(message: Message):
    _register_message_stats(message)
    await message.answer("Секундочку, я обрабатываю ваше голосовое сообщение...")
    try:
        file = await bot.get_file(message.voice.file_id)
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                voice_bytes = await resp.read()
    except Exception as e:
        logging.error(f"Ошибка скачивания голосового файла: {e}")
        return
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmpf:
            tmpf.write(voice_bytes)
            ogg_path = tmpf.name
    except Exception as e:
        logging.error(f"Ошибка сохранения файла: {e}")
        return
    try:
        audio = AudioSegment.from_file(ogg_path, format="ogg")
        wav_path = ogg_path.replace(".ogg", ".wav")
        audio.export(wav_path, format="wav")
    except Exception as e:
        logging.error(f"Ошибка конвертации аудио: {e}")
        os.remove(ogg_path)
        return
    finally:
        os.remove(ogg_path)
    recognizer = sr.Recognizer()
    recognized_text = ""
    try:
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            recognized_text = recognizer.recognize_google(audio_data, language="ru-RU")
    except Exception as e:
        logging.error(f"Ошибка распознавания голосового сообщения: {e}")
    os.remove(wav_path)
    if recognized_text:
        await handle_all_messages_impl(message, recognized_text)

@dp.message()
async def handle_all_messages(message: Message):
    user_input = (message.text or "").strip()
    await handle_all_messages_impl(message, user_input)

async def handle_all_messages_impl(message: Message, user_input: str):
    _register_message_stats(message)
    all_chat_ids.add(message.chat.id)
    uid = message.from_user.id
    cid = message.chat.id
    voice_response_requested = False
    if message.chat.id == ADMIN_ID and message.reply_to_message:
        original_id = message.reply_to_message.message_id
        if original_id in support_reply_map:
            user_id = support_reply_map[original_id]
            try:
                await send_admin_reply_as_single_message(message, user_id)
            except Exception as e:
                logging.warning(f"[BOT] Ошибка при отправке ответа админа пользователю: {e}")
        return
    if uid in support_mode_users:
        support_mode_users.discard(uid)
        try:
            caption = message.caption or user_input or "[Без текста]"
            username_part = f" (@{message.from_user.username})" if message.from_user.username else ""
            content = (f"\u2728 <b>Новое сообщение в поддержку</b> от <b>{message.from_user.full_name}</b>{username_part} "
                       f"(id: <code>{uid}</code>):\n\n{caption}")
            sent_msg = None
            if message.photo:
                file = await bot.get_file(message.photo[-1].file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        photo_bytes = await resp.read()
                sent_msg = await bot.send_photo(chat_id=ADMIN_ID, photo=BufferedInputFile(photo_bytes, filename="image.jpg"), caption=content)
            elif message.video:
                file = await bot.get_file(message.video.file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        video_bytes = await resp.read()
                sent_msg = await bot.send_video(chat_id=ADMIN_ID, video=BufferedInputFile(video_bytes, filename="video.mp4"), caption=content)
            else:
                sent_msg = await bot.send_message(chat_id=ADMIN_ID, text=content)
            if sent_msg:
                support_reply_map[sent_msg.message_id] = uid
            await message.answer("Сообщение отправлено в поддержку.")
        except Exception as e:
            logging.warning(f"[BOT] Ошибка при пересылке в поддержку: {e}")
            await message.answer("Произошла ошибка при отправке сообщения в поддержку.")
        return
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if cid in disabled_chats:
            return
        lower_text = user_input.lower()
        mentioned = any(keyword in lower_text for keyword in ["вай", "vai", "вэй"])
        reply_to_bot = (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.username
            and message.reply_to_message.from_user.username.lower() == BOT_USERNAME.lower()
        )
        if not (mentioned or reply_to_bot):
            return
    if message.document:
        stats["files_received"] += 1
        file = await bot.get_file(message.document.file_id)
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                file_bytes = await resp.read()
        text = extract_text_from_file(message.document.file_name, file_bytes)
        if text:
            user_documents[uid] = text
            await message.answer("✅ Файл получен! Можешь задать вопрос по его содержимому.")
        else:
            await message.answer("⚠️ Не удалось извлечь текст из файла.")
        return
    voice_regex = re.compile(r"(ответь\s+(войсом|голосом)|голосом\s+ответь)", re.IGNORECASE)
    if voice_regex.search(user_input):
        voice_response_requested = True
        user_input = voice_regex.sub("", user_input).strip()
    lower_input = user_input.lower()
    logging.info(f"[DEBUG] cid={cid}, text='{user_input}'")
    exchange_match = EXCHANGE_PATTERN.search(lower_input)
    if exchange_match:
        amount_str = exchange_match.group(1).replace(',', '.')
        try:
            amount = float(amount_str)
        except:
            amount = 0
        from_curr_raw = exchange_match.group(2)
        to_curr_raw = exchange_match.group(3)
        from_curr_lemma = normalize_currency_rus(from_curr_raw)
        to_curr_lemma = normalize_currency_rus(to_curr_raw)
        from_curr = CURRENCY_SYNONYMS.get(from_curr_lemma, from_curr_lemma.upper())
        to_curr = CURRENCY_SYNONYMS.get(to_curr_lemma, to_curr_lemma.upper())
        exchange_text = await get_exchange_rate(amount, from_curr, to_curr)
        if exchange_text is not None:
            if voice_response_requested:
                await send_voice_message(cid, exchange_text)
            else:
                await message.answer(exchange_text)
            return
    weather_pattern = r"погода(?:\s+в)?\s+([a-zа-яё\-\s]+?)(?:\s+(?:на\s+(\d+)\s+дн(?:я|ей)|на\s+(неделю)|завтра|послезавтра))?$"
    weather_match = re.search(weather_pattern, lower_input, re.IGNORECASE)
    if weather_match:
        city_raw = weather_match.group(1).strip()
        days_part = weather_match.group(2)
        week_flag = weather_match.group(3)
        mode_flag = re.search(r"(завтра|послезавтра)", lower_input)
        city_norm = normalize_city_name(city_raw)
        if week_flag:
            days = 7
            mode = ""
        elif mode_flag:
            days = 2
            mode = mode_flag.group(1)
        else:
            days = int(days_part) if days_part else 1
            mode = ""
        weather_info = await get_weather_info(city_norm, days, mode)
        if not weather_info:
            weather_info = "Не удалось получить данные о погоде."
        if voice_response_requested:
            await send_voice_message(cid, weather_info)
        else:
            await message.answer(weather_info)
        return
    if uid in user_documents:
        file_content = user_documents[uid]
        prompt_with_file = (f"Пользователь отправил файл со следующим содержимым:\n\n{file_content}\n\n"
                            f"Теперь пользователь задаёт вопрос:\n\n{user_input}\n\n"
                            f"Ответь чётко и кратко, основываясь на содержимом файла.")
        gemini_text = await generate_and_send_gemini_response(cid, prompt_with_file, False, "", "")
        if voice_response_requested:
            await send_voice_message(cid, gemini_text)
        else:
            await message.answer(gemini_text)
        return
    gemini_text = await handle_msg(message, user_input, voice_response_requested)
    if not gemini_text:
        return
    if gemini_text and any(x in gemini_text for x in ["$", "\\(", "\\[", "\\begin{equation}"]):
        image_path = render_latex_to_image(gemini_text)
        if image_path:
            await bot.send_photo(chat_id=cid, photo=FSInputFile(image_path, filename="formula.png"),
                                 caption="Вот ваша формула/уравнение. Задайте ваш вопрос.")
            os.remove(image_path)
            return
    if voice_response_requested:
        await send_voice_message(cid, gemini_text)
    else:
        await message.answer(gemini_text)
    return

def split_smart(text: str, limit: int) -> list[str]:
    results = []
    start = 0
    length = len(text)
    while start < length:
        remain = length - start
        if remain <= limit:
            results.append(text[start:].strip())
            break
        candidate = text[start: start+limit]
        cut_pos = candidate.rfind('. ')
        if cut_pos == -1:
            cut_pos = candidate.rfind(' ')
            if cut_pos == -1:
                cut_pos = len(candidate)
        else:
            cut_pos += 1
        chunk = text[start: start+cut_pos].strip()
        if chunk:
            results.append(chunk)
        start += cut_pos
    return [x for x in results if x]

CAPTION_LIMIT = 950
TELEGRAM_MSG_LIMIT = 4096

def split_caption_and_text(text: str) -> tuple[str, list[str]]:
    if len(text) <= CAPTION_LIMIT:
        return text, []
    chunks_950 = split_smart(text, CAPTION_LIMIT)
    caption = chunks_950[0]
    leftover = " ".join(chunks_950[1:]).strip()
    if not leftover:
        return caption, []
    rest = split_smart(leftover, TELEGRAM_MSG_LIMIT)
    return caption, rest

def format_gemini_response(text: str) -> str:
    code_blocks = {}
    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        return placeholder
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)
    text = escape(text)
    for placeholder, block_html in code_blocks.items():
        text = text.replace(escape(placeholder), block_html)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r"\[.*?(изображение|рисунок).+?\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(Я являюсь текстовым ассистентом.*выводить графику\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I am a text-based model.*cannot directly show images\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I can’t show images directly\.)", "", text, flags=re.IGNORECASE)

    lines = text.split('\n')
    new_lines = []
    for line in lines:
        stripped = line.lstrip()
        prefix_len = len(line) - len(stripped)
        if stripped.startswith('* ') and not stripped.startswith('**'):
            replaced_line = (' ' * prefix_len) + '• ' + stripped[2:]
            new_lines.append(replaced_line)
        else:
            new_lines.append(line)
    text = '\n'.join(new_lines).strip()

    text = re.sub(r"(?i)\bi am a large language model\b", "I am VAI, created by Vandili", text)
    text = re.sub(r"(?i)\bi'm a large language model\b", "I'm VAI, created by Vandili", text)
    text = re.sub(r"(?i)\bgoogle\b", "Vandili", text)
    text = re.sub(r"я большая языковая модель(?:.*?)(?=\.)", "Я VAI, создан командой Vandili", text, flags=re.IGNORECASE)
    text = re.sub(r"я большая языковая модель", "Я VAI, создан командой Vandili", text, flags=re.IGNORECASE)
    text = re.sub(r"я\s*—\s*большая языковая модель", "Я — VAI, создан командой Vandili", text, flags=re.IGNORECASE)
    return text

# ---------------------- Новый блок: Инициализация Pix2Tex ---------------------- #
from pix2tex.cli import LatexOCR
pix2tex_model = None

def init_pix2tex_model():
    """
    Инициализирует глобальную модель Pix2Tex для распознавания LaTeX из изображений.
    Загружается один раз.
    """
    global pix2tex_model
    if pix2tex_model is None:
        logging.info("[Pix2Tex] Инициализация модели...")
        pix2tex_model = LatexOCR()

# ---------------------- Основной функционал ---------------------- #
@dp.message(F.text.lower().startswith("вай покажи"))
async def group_show_request(message: Message):
    user_input = message.text.strip()
    await handle_msg(message, recognized_text=user_input, voice_response_requested=False)

@dp.message(lambda message: message.voice is not None)
async def handle_voice_message(message: Message):
    _register_message_stats(message)
    await message.answer("Секундочку, я обрабатываю ваше голосовое сообщение...")
    try:
        file = await bot.get_file(message.voice.file_id)
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                voice_bytes = await resp.read()
    except Exception as e:
        logging.error(f"Ошибка скачивания голосового файла: {e}")
        return
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmpf:
            tmpf.write(voice_bytes)
            ogg_path = tmpf.name
    except Exception as e:
        logging.error(f"Ошибка сохранения файла: {e}")
        return
    try:
        audio = AudioSegment.from_file(ogg_path, format="ogg")
        wav_path = ogg_path.replace(".ogg", ".wav")
        audio.export(wav_path, format="wav")
    except Exception as e:
        logging.error(f"Ошибка конвертации аудио: {e}")
        os.remove(ogg_path)
        return
    finally:
        os.remove(ogg_path)
    recognizer = sr.Recognizer()
    recognized_text = ""
    try:
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            recognized_text = recognizer.recognize_google(audio_data, language="ru-RU")
    except Exception as e:
        logging.error(f"Ошибка распознавания голосового сообщения: {e}")
    os.remove(wav_path)
    if recognized_text:
        await handle_all_messages_impl(message, recognized_text)

@dp.message()
async def handle_all_messages(message: Message):
    user_input = (message.text or "").strip()
    await handle_all_messages_impl(message, user_input)

async def handle_all_messages_impl(message: Message, user_input: str):
    _register_message_stats(message)
    all_chat_ids.add(message.chat.id)
    uid = message.from_user.id
    cid = message.chat.id
    voice_response_requested = False
    if message.chat.id == ADMIN_ID and message.reply_to_message:
        original_id = message.reply_to_message.message_id
        if original_id in support_reply_map:
            user_id = support_reply_map[original_id]
            try:
                await send_admin_reply_as_single_message(message, user_id)
            except Exception as e:
                logging.warning(f"[BOT] Ошибка при отправке ответа админа пользователю: {e}")
        return
    if uid in support_mode_users:
        support_mode_users.discard(uid)
        try:
            caption = message.caption or user_input or "[Без текста]"
            username_part = f" (@{message.from_user.username})" if message.from_user.username else ""
            content = (f"\u2728 <b>Новое сообщение в поддержку</b> от <b>{message.from_user.full_name}</b>{username_part} "
                       f"(id: <code>{uid}</code>):\n\n{caption}")
            sent_msg = None
            if message.photo:
                file = await bot.get_file(message.photo[-1].file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        photo_bytes = await resp.read()
                sent_msg = await bot.send_photo(chat_id=ADMIN_ID, photo=BufferedInputFile(photo_bytes, filename="image.jpg"), caption=content)
            elif message.video:
                file = await bot.get_file(message.video.file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        video_bytes = await resp.read()
                sent_msg = await bot.send_video(chat_id=ADMIN_ID, video=BufferedInputFile(video_bytes, filename="video.mp4"), caption=content)
            else:
                sent_msg = await bot.send_message(chat_id=ADMIN_ID, text=content)
            if sent_msg:
                support_reply_map[sent_msg.message_id] = uid
            await message.answer("Сообщение отправлено в поддержку.")
        except Exception as e:
            logging.warning(f"[BOT] Ошибка при пересылке в поддержку: {e}")
            await message.answer("Произошла ошибка при отправке сообщения в поддержку.")
        return
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if cid in disabled_chats:
            return
        lower_text = user_input.lower()
        mentioned = any(keyword in lower_text for keyword in ["вай", "vai", "вэй"])
        reply_to_bot = (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.username
            and message.reply_to_message.from_user.username.lower() == BOT_USERNAME.lower()
        )
        if not (mentioned or reply_to_bot):
            return
    if message.document:
        stats["files_received"] += 1
        file = await bot.get_file(message.document.file_id)
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                file_bytes = await resp.read()
        text = extract_text_from_file(message.document.file_name, file_bytes)
        if text:
            user_documents[uid] = text
            await message.answer("✅ Файл получен! Можешь задать вопрос по его содержимому.")
        else:
            await message.answer("⚠️ Не удалось извлечь текст из файла.")
        return
    voice_regex = re.compile(r"(ответь\s+(войсом|голосом)|голосом\s+ответь)", re.IGNORECASE)
    if voice_regex.search(user_input):
        voice_response_requested = True
        user_input = voice_regex.sub("", user_input).strip()
    lower_input = user_input.lower()
    logging.info(f"[DEBUG] cid={cid}, text='{user_input}'")
    exchange_match = EXCHANGE_PATTERN.search(lower_input)
    if exchange_match:
        amount_str = exchange_match.group(1).replace(',', '.')
        try:
            amount = float(amount_str)
        except:
            amount = 0
        from_curr_raw = exchange_match.group(2)
        to_curr_raw = exchange_match.group(3)
        from_curr_lemma = normalize_currency_rus(from_curr_raw)
        to_curr_lemma = normalize_currency_rus(to_curr_raw)
        from_curr = CURRENCY_SYNONYMS.get(from_curr_lemma, from_curr_lemma.upper())
        to_curr = CURRENCY_SYNONYMS.get(to_curr_lemma, to_curr_lemma.upper())
        exchange_text = await get_exchange_rate(amount, from_curr, to_curr)
        if exchange_text is not None:
            if voice_response_requested:
                await send_voice_message(cid, exchange_text)
            else:
                await message.answer(exchange_text)
            return
    weather_pattern = r"погода(?:\s+в)?\s+([a-zа-яё\-\s]+?)(?:\s+(?:на\s+(\d+)\s+дн(?:я|ей)|на\s+(неделю)|завтра|послезавтра))?$"
    weather_match = re.search(weather_pattern, lower_input, re.IGNORECASE)
    if weather_match:
        city_raw = weather_match.group(1).strip()
        days_part = weather_match.group(2)
        week_flag = weather_match.group(3)
        mode_flag = re.search(r"(завтра|послезавтра)", lower_input)
        city_norm = normalize_city_name(city_raw)
        if week_flag:
            days = 7
            mode = ""
        elif mode_flag:
            days = 2
            mode = mode_flag.group(1)
        else:
            days = int(days_part) if days_part else 1
            mode = ""
        weather_info = await get_weather_info(city_norm, days, mode)
        if not weather_info:
            weather_info = "Не удалось получить данные о погоде."
        if voice_response_requested:
            await send_voice_message(cid, weather_info)
        else:
            await message.answer(weather_info)
        return
    if uid in user_documents:
        file_content = user_documents[uid]
        prompt_with_file = (f"Пользователь отправил файл со следующим содержимым:\n\n{file_content}\n\n"
                            f"Теперь пользователь задаёт вопрос:\n\n{user_input}\n\n"
                            f"Ответь чётко и кратко, основываясь на содержимом файла.")
        gemini_text = await generate_and_send_gemini_response(cid, prompt_with_file, False, "", "")
        if voice_response_requested:
            await send_voice_message(cid, gemini_text)
        else:
            await message.answer(gemini_text)
        return
    gemini_text = await handle_msg(message, user_input, voice_response_requested)
    if not gemini_text:
        return
    if gemini_text and any(x in gemini_text for x in ["$", "\\(", "\\[", "\\begin{equation}"]):
        image_path = render_latex_to_image(gemini_text)
        if image_path:
            await bot.send_photo(chat_id=cid, photo=FSInputFile(image_path, filename="formula.png"),
                                 caption="Вот ваша формула/уравнение. Задайте ваш вопрос.")
            os.remove(image_path)
            return
    if voice_response_requested:
        await send_voice_message(cid, gemini_text)
    else:
        await message.answer(gemini_text)
    return

def split_smart(text: str, limit: int) -> list[str]:
    results = []
    start = 0
    length = len(text)
    while start < length:
        remain = length - start
        if remain <= limit:
            results.append(text[start:].strip())
            break
        candidate = text[start: start+limit]
        cut_pos = candidate.rfind('. ')
        if cut_pos == -1:
            cut_pos = candidate.rfind(' ')
            if cut_pos == -1:
                cut_pos = len(candidate)
        else:
            cut_pos += 1
        chunk = text[start: start+cut_pos].strip()
        if chunk:
            results.append(chunk)
        start += cut_pos
    return [x for x in results if x]

CAPTION_LIMIT = 950
TELEGRAM_MSG_LIMIT = 4096

def split_caption_and_text(text: str) -> tuple[str, list[str]]:
    if len(text) <= CAPTION_LIMIT:
        return text, []
    chunks_950 = split_smart(text, CAPTION_LIMIT)
    caption = chunks_950[0]
    leftover = " ".join(chunks_950[1:]).strip()
    if not leftover:
        return caption, []
    rest = split_smart(leftover, TELEGRAM_MSG_LIMIT)
    return caption, rest

def format_gemini_response(text: str) -> str:
    code_blocks = {}
    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        return placeholder
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)
    text = escape(text)
    for placeholder, block_html in code_blocks.items():
        text = text.replace(escape(placeholder), block_html)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r"\[.*?(изображение|рисунок).+?\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(Я являюсь текстовым ассистентом.*выводить графику\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I am a text-based model.*cannot directly show images\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I can’t show images directly\.)", "", text, flags=re.IGNORECASE)

    lines = text.split('\n')
    new_lines = []
    for line in lines:
        stripped = line.lstrip()
        prefix_len = len(line) - len(stripped)
        if stripped.startswith('* ') and not stripped.startswith('**'):
            replaced_line = (' ' * prefix_len) + '• ' + stripped[2:]
            new_lines.append(replaced_line)
        else:
            new_lines.append(line)
    text = '\n'.join(new_lines).strip()

    text = re.sub(r"(?i)\bi am a large language model\b", "I am VAI, created by Vandili", text)
    text = re.sub(r"(?i)\bi'm a large language model\b", "I'm VAI, created by Vandili", text)
    text = re.sub(r"(?i)\bgoogle\b", "Vandili", text)
    text = re.sub(r"я большая языковая модель(?:.*?)(?=\.)", "Я VAI, создан командой Vandili", text, flags=re.IGNORECASE)
    text = re.sub(r"я большая языковая модель", "Я VAI, создан командой Vandili", text, flags=re.IGNORECASE)
    text = re.sub(r"я\s*—\s*большая языковая модель", "Я — VAI, создан командой Vandili", text, flags=re.IGNORECASE)
    return text

async def extract_text_from_file(filename: str, file_bytes: bytes) -> str:
    if filename.endswith(".txt") or filename.endswith(".py"):
        return file_bytes.decode("utf-8", errors="ignore")
    elif filename.endswith(".pdf"):
        try:
            from io import BytesIO
            from PyPDF2 import PdfReader
            pdf_stream = BytesIO(file_bytes)
            reader = PdfReader(pdf_stream)
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return ""
    elif filename.endswith(".docx"):
        try:
            import tempfile
            from docx import Document
            with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmpf:
                tmpf.write(file_bytes)
                tmp_path = tmpf.name
            doc = Document(tmp_path)
            os.remove(tmp_path)
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception:
            return ""
    return ""

# ---------------------- Здесь должны быть функции handle_msg и generate_and_send_gemini_response ---------------------- #
# Обратите внимание: в оригинальном коде они не были показаны полностью.
# Если они у вас реализованы отдельно, подключите их соответственно.
# (В данном фрагменте важно было внести правки в блоках handle_photo и пр.)

# ---------------------- Запуск бота ---------------------- #
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
