# ---------------------- Импорты ---------------------- #
import logging
import os
import traceback
import re
import random
import aiohttp
import requests
import pytesseract
from matplotlib import pyplot as plt
from matplotlib import rc
from PIL import Image
from io import BytesIO
from aiogram.types import FSInputFile
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

# Добавляем EasyOCR (если потребуется в дальнейшем, но для формул теперь используется pix2tex)
import easyocr
easyocr_reader = easyocr.Reader(['en'])  # Глобальный экземпляр

# ---------------------- CHANGED: Оптимизированный импорт для LatexOCR ---------------------- #
from pix2tex.cli import LatexOCR
ocr = LatexOCR()
logging.info("LatexOCR status: %s", "Loaded" if ocr else "Failed")

import matplotlib.pyplot as plt
from io import BytesIO

def latex_to_image(latex_code: str) -> BytesIO:
    try:
        latex_code = latex_code.strip().rstrip('.')
        fig, ax = plt.subplots()
        ax.text(0.5, 0.5, f"${latex_code}$", fontsize=24, ha='center', va='center')
        ax.axis('off')
        fig.tight_layout(pad=1)

        img_bytes = BytesIO()
        plt.savefig(img_bytes, format='png', bbox_inches='tight', dpi=200, transparent=True)
        plt.close(fig)
        img_bytes.seek(0)
        return img_bytes
    except Exception as e:
        logging.error(f"[LaTeX Render] Ошибка визуализации формулы: {e}")
        raise

def is_latex_valid(expr: str) -> bool:
    # Фильтруем явные ошибки
    if "\\out" in expr or "prime..{" in expr:
        return False
    try:
        latex_code = expr.strip().rstrip('.')
        fig, ax = plt.subplots()
        ax.text(0.5, 0.5, f"${latex_code}$", fontsize=20, ha='center', va='center')
        ax.axis('off')
        plt.close(fig)
        return True
    except Exception as e:
        logging.warning(f"[Latex Validate] Ошибка при отрисовке: {e}")
        return False

# ---------------------- Вспомогательная функция для чтения файлов ---------------------- #
from pathlib import Path
import tempfile
from docx import Document
from PyPDF2 import PdfReader

def extract_text_from_file(filename: str, file_bytes: bytes) -> str:
    if filename.endswith(".txt") or filename.endswith(".py"):
        return file_bytes.decode("utf-8", errors="ignore")
    elif filename.endswith(".pdf"):
        try:
            with BytesIO(file_bytes) as pdf_stream:
                reader = PdfReader(pdf_stream)
                return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return ""
    elif filename.endswith(".docx"):
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmpf:
                tmpf.write(file_bytes)
                tmp_path = tmpf.name
            doc = Document(tmp_path)
            os.remove(tmp_path)
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception:
            return ""
    return ""

# ---------------------- Инициализация ---------------------- #
key_path = '/root/vandili/gcloud-key.json'
from google.oauth2 import service_account
credentials = service_account.Credentials.from_service_account_file(key_path)
from google.cloud import translate
translate_client = translate.TranslationServiceClient(credentials=credentials)

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME")
WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
morph = MorphAnalyzer()

import google.generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-2.5-pro-exp-03-25")

# ---------------------- Загрузка/сохранение статистики ---------------------- #
STATS_FILE = "stats.json"

def load_stats() -> dict:
    if not os.path.exists(STATS_FILE):
        return {
            "messages_total": 0,
            "files_received": 0,
            "commands_used": {}
        }
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("messages_total", 0)
            data.setdefault("files_received", 0)
            data.setdefault("commands_used", {})
            return data
    except Exception as e:
        logging.warning(f"Не удалось загрузить stats.json: {e}")
        return {
            "messages_total": 0,
            "files_received": 0,
            "commands_used": {}
        }

def save_stats():
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Не удалось сохранить stats.json: {e}")

stats = load_stats()

support_mode_users = set()
support_reply_map = {}
chat_history = {}
user_documents = {}
user_images_text = {}

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

from aiogram.enums import ChatType
from aiogram.types import Message

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
    normal_form = parsed[0].normal_form
    return normal_form

def normalize_city_name(raw_city: str) -> str:
    """
    Приводит "Москве" -> "москва", "Ташкенте" -> "ташкент" и т.д.
    Если морфопарсер даёт слишком короткую форму или ту же самую строку, оставляем оригинал.
    """
    words = raw_city.split()
    norm_words = []
    for w in words:
        w_clean = w.strip(punctuation).lower()
        parsed = morph.parse(w_clean)
        if not parsed:
            norm_words.append(w_clean)
            continue
        best = parsed[0]
        if best.normal_form == w_clean or len(best.normal_form) < 2:
            norm_words.append(w_clean)
        else:
            norm_words.append(best.normal_form)
    return " ".join(norm_words)

# ---------------------- Словарь базовых форм валют (расширенный) ---------------------- #
CURRENCY_SYNONYMS = {
    "доллар": "USD", "доллары": "USD", "долларов": "USD",
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
    return {
        "lat": best["latitude"],
        "lon": best["longitude"],
        "timezone": best.get("timezone", "Europe/Moscow")
    }

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

def emoji_for_condition(text: str) -> str:
    text = text.lower()
    if "ясно" in text:
        return "☀️"
    elif "облачно" in text or "пасмурно" in text:
        return "☁️"
    elif "туман" in text:
        return "🌫️"
    elif "гроза" in text:
        return "⛈️"
    elif "дождь" in text:
        return "🌧️"
    elif "снег" in text:
        return "🌨️"
    return ""

async def get_weather_info(city: str, days: int = 1, mode: str = "") -> str:
    if not WEATHERAPI_KEY:
        return "⛅️ API ключ для погоды не найден."

    try:
        async with aiohttp.ClientSession() as session:
            params = {
                "key": WEATHERAPI_KEY,
                "q": city,
                "days": max(1, min(days, 10)),
                "lang": "ru",
                "aqi": "no",
                "alerts": "no"
            }
            async with session.get("https://api.weatherapi.com/v1/forecast.json", params=params) as resp:
                if resp.status != 200:
                    return f"Ошибка запроса погоды: {resp.status}"
                data = await resp.json()
    except Exception as e:
        logging.error(f"[WEATHER] Ошибка получения погоды: {e}")
        return "Ошибка при получении прогноза погоды."

    location = data.get("location", {})
    forecast = data.get("forecast", {}).get("forecastday", [])
    current = data.get("current", {})

    location_name = location.get("name", city).capitalize()

    # Режим "завтра" или "послезавтра"
    if mode in ["завтра", "послезавтра"]:
        index = 1 if mode == "завтра" else 2
        if index >= len(forecast):
            return f"Нет данных о погоде в {location_name} на {mode} 😕"
        day = forecast[index]
        date = day["date"]
        text = day["day"]["condition"]["text"]
        tmin = day["day"]["mintemp_c"]
        tmax = day["day"]["maxtemp_c"]
        emoji = emoji_for_condition(text)
        return f"<b>Погода в {location_name} на {mode}:</b>\n{date}: {text} {emoji}, от {tmin}°C до {tmax}°C"

    # Прогноз на несколько дней
    if days > 1:
        lines = [f"<b>Прогноз погоды в {location_name}:</b>"]
        for day in forecast[:days]:
            date = day["date"]
            text = day["day"]["condition"]["text"]
            emoji = emoji_for_condition(text)
            tmin = day["day"]["mintemp_c"]
            tmax = day["day"]["maxtemp_c"]
            lines.append(f"• {date} — {text} {emoji}, {tmin}..{tmax}°C")
        return "\n".join(lines)

    # Текущая погода
    condition = current.get("condition", {}).get("text", "")
    emoji = emoji_for_condition(condition)
    temp = current.get("temp_c")
    wind = current.get("wind_kph")
    return f"Погода в {location_name} сейчас: {condition} {emoji}, температура {temp}°C, ветер {wind} км/ч."

# ---------------------- Функция для отправки голосового ответа ---------------------- #
async def send_voice_message(chat_id: int, text: str):
    clean_text = re.sub(r'<[^>]+>', '', text or "")
    if not clean_text.strip():
        clean_text = "Нет данных для голосового ответа."
    tts = gTTS(clean_text, lang='ru')
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_audio:
        tts.save(tmp_audio.name)
        mp3_path = tmp_audio.name
    audio = AudioSegment.from_file(mp3_path, format="mp3")
    ogg_path = mp3_path.replace(".mp3", ".ogg")
    audio.export(ogg_path, format="ogg")
    os.remove(mp3_path)
    await bot.send_voice(chat_id=chat_id, voice=FSInputFile(ogg_path, filename="voice.ogg"))
    os.remove(ogg_path)

# ---------------------- Вспомогательная функция для thread ---------------------- #
def thread(message: Message) -> dict:
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP] and message.message_thread_id:
        return {"message_thread_id": message.message_thread_id}
    return {}

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
        await bot.send_message(chat_id=message.chat.id, text="Если возник вопрос или хочешь сообщить об ошибке — напиши мне в личку:", reply_markup=keyboard, **thread(message))
    else:
        private_url = f"https://t.me/{BOT_USERNAME}?start=support"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✉️ Написать в поддержку", url=private_url)]]
        )
        await bot.send_message(chat_id=message.chat.id, text="Если возник вопрос или хочешь сообщить об ошибке — напиши мне в личку:", reply_markup=keyboard, **thread(message))

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
    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"Всего сообщений: {total_msgs}\n"
        f"Уникальных пользователей: {unique_users_count}\n"
        f"Получено файлов: {files_received}\n\n"
    )
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


@dp.message(F.photo)
async def handle_photo_message(message: Message):
    _register_message_stats(message)
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                photo_bytes = await resp.read()

        image_rgb = Image.open(BytesIO(photo_bytes)).convert("RGB")

        # 1. Пробуем распознать текст
        text_raw = pytesseract.image_to_string(image_rgb, lang="rus+eng").strip()

        # 2. Пробуем распознать формулу
        extracted_latex = ""
        if ocr:
            try:
                extracted_latex = ocr(image_rgb).strip()
            except Exception as e:
                logging.error(f"LatexOCR error: {traceback.format_exc()}")

        # 3. Проверяем, похоже ли на формулу и валидно ли LaTeX
        is_formula_like = bool(re.search(r'\$|\\\(|\\\[|\^|_', extracted_latex))
        if is_formula_like:
            if not is_latex_valid(extracted_latex):
                user_images_text[message.from_user.id] = extracted_latex
                await message.answer(
                    "⚠️ Формула распознана, но содержит ошибки и не может быть отображена.\n\n"
                    f"<b>Распознанная формула:</b>\n<code>{escape(extracted_latex)}</code>\n\n"
                    "Попробуй вручную подправить формулу и напиши, например:\n<code>реши: исправленная_формула</code>"
                )
                    
                return
            user_images_text[message.from_user.id] = extracted_latex
            try:
                img_bytes = latex_to_image(extracted_latex)
                latex_file = FSInputFile(img_bytes, filename="formula.png")
                await bot.send_photo(
                    chat_id=message.chat.id,
                    photo=latex_file,
                    caption="🧾 Текст с картинки считан. Задайте ваш вопрос по картинке."
                )
            except Exception as e:
                await message.answer(f"⚠️ Ошибка визуализации формулы: <code>{escape(str(e))}</code>")
        elif text_raw:
            user_images_text[message.from_user.id] = text_raw
            prompt = f"Распознанный текст:\n{text_raw}\nОтветь по содержанию:"
            answer = await generate_and_send_gemini_response(message.chat.id, prompt, False, "", "")
            await message.answer(answer)
        else:
            await message.answer("❌ Не удалось распознать текст или формулу на изображении.")

    except Exception as e:
        logging.error(f"PHOTO PROCESSING ERROR: {traceback.format_exc()}")
        await message.answer("⚠️ Ошибка обработки изображения. Проверь его формат.")

@dp.message()
async def handle_all_messages(message: Message):
    user_input = (message.text or "").strip()
    formula = ""
    voice_response_requested = False
    cid = message.chat.id

    if user_input.lower().startswith("реши:"):
        formula = user_input[5:].strip()

    if formula:
        prompt = (
            f"Реши следующий интеграл или математическое выражение, представленное в LaTeX:\n\n"
            f"\\[{formula}\\]\n\n"
            f"Покажи решение пошагово. Объясни каждый шаг, если он неочевиден. "
            f"Не добавляй преобразований, если они не нужны. Не пиши модуль, если это не вытекает из условия."
        )

        gemini_text = await generate_and_send_gemini_response(cid, prompt, False, "", "")
        if voice_response_requested:
            await send_voice_message(cid, gemini_text)
        else:
            await message.answer(gemini_text)
        return

    await handle_all_messages_impl(message, user_input)

async def handle_all_messages_impl(message: Message, user_input: str):
    _register_message_stats(message)
    all_chat_ids.add(message.chat.id)
    uid = message.from_user.id
    cid = message.chat.id

    voice_response_requested = False  # исправление UnboundLocalError

    # Если админ отвечает на сообщение поддержки
    if message.chat.id == ADMIN_ID and message.reply_to_message:
        original_id = message.reply_to_message.message_id
        if original_id in support_reply_map:
            user_id = support_reply_map[original_id]
            try:
                await send_admin_reply_as_single_message(message, user_id)
            except Exception as e:
                logging.warning(f"[BOT] Ошибка при отправке ответа админа пользователю: {e}")
        return

    # Если пользователь только что нажал кнопку "Написать в поддержку"
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

    # Если бот отключён в группе
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if cid in disabled_chats:
            return
        lower_text = user_input.lower()
        mentioned = any(keyword in lower_text for keyword in ["вай", "vai", "вэй"])
        reply_to_bot = (message.reply_to_message and 
                        message.reply_to_message.from_user and 
                        message.reply_to_message.from_user.username and 
                        message.reply_to_message.from_user.username.lower() == BOT_USERNAME.lower())
        if not (mentioned or reply_to_bot):
            return

    # Если пользователь отправил документ
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

    # Проверка запроса на ответ голосом
    voice_regex = re.compile(r"(ответь\s+(войсом|голосом)|голосом\s+ответь)", re.IGNORECASE)
    if voice_regex.search(user_input):
        voice_response_requested = True
        user_input = voice_regex.sub("", user_input).strip()
    
    lower_input = user_input.lower()

    logging.info(f"[DEBUG] cid={cid}, text='{user_input}'")

    # Запрос курса валют
    exchange_match = re.search(r"(\d+(?:[.,]\d+)?)\s*([a-zа-яё$€₽¥]+)\s*(в|to)\s*([a-zа-яё$€₽¥]+)", lower_input)
    if exchange_match:
        amount_str = exchange_match.group(1).replace(',', '.')
        try:
            amount = float(amount_str)
        except:
            amount = 0
        from_curr_raw = exchange_match.group(2)
        to_curr_raw = exchange_match.group(4)

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

    # Исправленная обработка запроса погоды
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
        return

        # ======= Распознан текст с изображения =======
    if uid in user_images_text:
        extracted = user_images_text[uid]
        is_latex_formula = bool(re.search(r'[=+\-\^\\]', extracted)) and extracted.startswith("\\")  # грубая эвристика
        del user_images_text[uid]

        if is_latex_formula:
            question_lower = user_input.lower()

            if question_lower.startswith("реши") or "распиши" in question_lower or "помоги" in question_lower:
                prompt = (
                    f"Реши следующее математическое выражение в LaTeX:\n\n"
                    f"\\[{extracted}\\]\n\n"
                    f"Покажи решение пошагово, с объяснениями. Не добавляй преобразований, если они не нужны. "
                )
            else:
                prompt = (
                    f"Формула, распознанная с изображения:\n\n\\[{extracted}\\]\n\n"
                    f"Пользователь спрашивает: {user_input}\n\n"
                    f"Ответь строго по теме, используй формулу как контекст."
                )

            response = await generate_and_send_gemini_response(cid, prompt, False, "", "")
            try:
                img_bytes = latex_to_image(extracted)
                latex_file = FSInputFile(img_bytes, filename="formula.png")
                caption, rest = split_caption_and_text(response or "...")
                await bot.send_photo(chat_id=cid, photo=latex_file, caption=caption, **thread(message))
                for c in rest:
                    await message.answer(c)
            except Exception as e:
                logging.warning(f"[BOT] Ошибка отрисовки формулы: {e}")
                await message.answer(response)
        else:
            prompt = (
                f"На изображении был распознан следующий текст:\n\n{extracted}\n\n"
                f"Пользователь спрашивает: {user_input}\n\n"
                f"Ответь по его содержимому."
            )
            response = await generate_and_send_gemini_response(cid, prompt, False, "", "")
            await message.answer(response)

        del user_images_text[uid]
        return

    # Все остальные запросы идут сюда:
    gemini_text = await handle_msg(message, user_input, voice_response_requested)
    if not gemini_text:
        return

    if voice_response_requested:
        await send_voice_message(cid, gemini_text)
    else:
        chunks = split_smart(gemini_text, TELEGRAM_MSG_LIMIT)
        for c in chunks:
            await message.answer(c)
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
        candidate = text[start : start+limit]
        cut_pos = candidate.rfind('. ')
        if cut_pos == -1:
            cut_pos = candidate.rfind(' ')
            if cut_pos == -1:
                cut_pos = len(candidate)
        else:
            cut_pos += 1
        chunk = text[start : start+cut_pos].strip()
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

IMAGE_TRIGGERS_RU = ["покажи", "покажи мне", "хочу увидеть", "пришли фото", "фото"]
NAME_COMMANDS = ["как тебя зовут", "твое имя", "твоё имя", "what is your name", "who are you"]
INFO_COMMANDS = ["кто тебя создал", "кто ты", "кто разработчик", "кто твой автор",
                 "кто твой создатель", "чей ты бот", "кем ты был создан",
                 "кто хозяин", "кто твой владелец", "в смысле кто твой создатель"]
OWNER_REPLIES = [
    "Я — <b>VAI</b>, создан командой <i>Vandili</i> 😎",
    "Мой создатель — <b>Vandili</b>. Я работаю для них 😉",
    "Я принадлежу <i>Vandili</i>, они моя команда ✨",
    "Создан <b>Vandili</b> — именно они дали мне жизнь 🤝",
    "Я бот <b>Vandili</b>. Всё просто 🤗",
    "Я продукт <i>Vandili</i>. Они мои создатели 😇"
]

RU_EN_DICT = {
    "обезьяна": "monkey",
    "тигр": "tiger",
    "кошка": "cat",
    "собака": "dog",
    "пейзаж": "landscape",
    "чайка": "seagull",
    "париж": "paris",
    "утконос": "platypus",
    "пудель": "poodle",
    "медоед": "honey badger"
}

async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    if not prompt:
        return None
    url = f"https://api.unsplash.com/photos/random?query={prompt}&client_id={access_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    logging.warning(f"Unsplash returned status {response.status} for prompt '{prompt}'")
                    return None
                data = await response.json()
                if "urls" not in data or "regular" not in data["urls"]:
                    logging.warning(f"No 'regular' URL in response for '{prompt}': {data}")
                    return None
                return data["urls"]["regular"]
    except Exception as e:
        logging.warning(f"Ошибка при получении изображения: {e}")
    return None

def fallback_translate_to_english(rus_word: str) -> str:
    try:
        project_id = "gen-lang-client-0588633435"
        location = "global"
        parent = f"projects/{project_id}/locations/{location}"
        response = translate_client.translate_text(
            parent=parent,
            contents=[rus_word],
            mime_type="text/plain",
            source_language_code="ru",
            target_language_code="en",
        )
        return response.translations[0].translated_text
    except Exception as e:
        logging.warning(f"Ошибка при переводе слова '{rus_word}': {e}")
        return rus_word

def generate_short_caption(rus_word: str) -> str:
    short_prompt = (
        "ИНСТРУКЦИЯ: Ты — творческий помощник, который умеет писать очень короткие, дружелюбные подписи "
        "на русском языке. Не упоминай, что ты ИИ или Google. Старайся не превышать 15 слов.\n\n"
        f"ЗАДАЧА: Придумай одну короткую, дружелюбную подпись для картинки с «{rus_word}». "
        "Можно с лёгкой эмоцией или юмором, не более 15 слов."
    )
    try:
        response = model.generate_content([
            {"role": "user", "parts": [short_prompt]}
        ])
        caption = format_gemini_response(response.text.strip())
        return caption
    except Exception as e:
        logging.error(f"[BOT] Error generating short caption: {e}")
        return rus_word.capitalize()

def parse_russian_show_request(user_text: str):
    lower_text = user_text.lower()
    triggered = any(trig in lower_text for trig in IMAGE_TRIGGERS_RU)
    if not triggered:
        return (False, "", "", user_text)
    match = re.search(r"(покажи( мне)?|хочу увидеть|пришли фото)\s+([\w\d]+)", lower_text)
    if match:
        raw_rus_word = match.group(3)
        raw_rus_word_clean = raw_rus_word.strip(punctuation)
        parsed = morph.parse(raw_rus_word_clean)
        if parsed:
            rus_normal = parsed[0].normal_form
        else:
            rus_normal = raw_rus_word_clean
        rus_word = rus_normal
    else:
        rus_word = ""
        raw_rus_word = ""
    if raw_rus_word:
        pattern_remove = rf"(покажи( мне)?|хочу увидеть|пришли фото)\s+{re.escape(raw_rus_word)}"
        leftover = re.sub(pattern_remove, "", user_text, flags=re.IGNORECASE).strip()
    else:
        leftover = user_text
    RU_EN_DICT_CUSTOM = {
        "обезьяна": "monkey",
        "тигр": "tiger",
        "кошка": "cat",
        "собака": "dog",
        "пейзаж": "landscape",
        "чайка": "seagull",
        "париж": "paris",
        "утконос": "platypus",
        "пудель": "poodle",
        "медоед": "honey badger"
    }
    if rus_word in RU_EN_DICT_CUSTOM:
        en_word = RU_EN_DICT_CUSTOM[rus_word]
    else:
        en_word = fallback_translate_to_english(rus_word)
    return (True, rus_word, en_word, leftover) if rus_word else (False, "", "", user_text)

# ---------------------- Основная функция handle_msg ---------------------- #
async def handle_msg(message: Message, recognized_text: str = None, voice_response_requested: bool = False):
    cid = message.chat.id
    user_input = recognized_text or (message.text or "").strip()

    lower_inp = user_input.lower()
    if any(nc in lower_inp for nc in NAME_COMMANDS):
        answer = "Меня зовут <b>VAI</b>! 🤖"
        if voice_response_requested:
            await send_voice_message(cid, answer)
        else:
            await message.answer(answer)
        return

    if any(ic in lower_inp for ic in INFO_COMMANDS):
        reply_text = random.choice(OWNER_REPLIES)
        if voice_response_requested:
            await send_voice_message(cid, reply_text)
        else:
            await message.answer(reply_text)
        return

    show_image, rus_word, image_en, leftover = parse_russian_show_request(user_input)
    if show_image and rus_word:
        leftover = re.sub(r"\b(вай|vai)\b", "", leftover, flags=re.IGNORECASE).strip()

    leftover = leftover.strip()
    full_prompt = f"{rus_word} {leftover}".strip() if rus_word else leftover

    image_url = None
    if show_image:
        image_url = await get_unsplash_image_url(image_en, UNSPLASH_ACCESS_KEY)

    gemini_text = await generate_and_send_gemini_response(cid, full_prompt, show_image, rus_word, leftover)

    if voice_response_requested:
        if not gemini_text:
            gemini_text = "Нет ответа для голосового сообщения."
        await send_voice_message(cid, gemini_text)
        return

    if image_url:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(image_url) as r:
                if r.status == 200:
                    photo_bytes = await r.read()
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmpf:
                        tmpf.write(photo_bytes)
                        tmp_path = tmpf.name
                    try:
                        await bot.send_chat_action(chat_id=cid, action="upload_photo")
                        file = FSInputFile(tmp_path, filename="image.jpg")
                        caption, rest = split_caption_and_text(gemini_text or "...")
                        await bot.send_photo(chat_id=cid, photo=file, caption=caption if caption else "...", **thread(message))
                        for c in rest:
                            await message.answer(c, **thread(message))
                    finally:
                        os.remove(tmp_path)
    elif gemini_text:
        chunks = split_smart(gemini_text, TELEGRAM_MSG_LIMIT)
        for c in chunks:
            await message.answer(c)

@dp.message(F.text.lower().startswith("вай покажи"))
async def group_show_request(message: Message):
    user_input = message.text.strip()
    await handle_msg(message, recognized_text=user_input, voice_response_requested=False)

async def generate_and_send_gemini_response(cid, full_prompt, show_image, rus_word, leftover):
    gemini_text = ""
    analysis_keywords = [
        "почему", "зачем", "на кого", "кто", "что такое", "влияние",
        "философ", "отрицал", "повлиял", "смысл", "экзистенциализм", "опроверг"
    ]
    needs_expansion = any(k in full_prompt.lower() for k in analysis_keywords)
    if needs_expansion:
        smart_prompt = ("Ответь чётко и по делу. Если в вопросе несколько частей — ответь на каждую. "
                        "Приводи имена и конкретные примеры, если они есть. Не повторяй вопрос, просто ответь:\n\n")
        full_prompt = smart_prompt + full_prompt

    if show_image and rus_word and not leftover:
        gemini_text = generate_short_caption(rus_word)
        return gemini_text

    conversation = chat_history.setdefault(cid, [])
    conversation.append({"role": "user", "parts": [full_prompt]})
    if len(conversation) > 8:
        conversation.pop(0)
    try:
        await bot.send_chat_action(chat_id=cid, action="typing")
        resp = model.generate_content(conversation)
        if not resp.candidates:
            reason = getattr(resp.prompt_feedback, "block_reason", "неизвестна")
            logging.warning(f"[BOT] Запрос заблокирован Gemini: причина — {reason}")
            gemini_text = ("⚠️ Запрос отклонён. Возможно, он содержит недопустимый или чувствительный контент.")
        else:
            raw_model_text = resp.text
            gemini_text = format_gemini_response(raw_model_text)
            conversation.append({"role": "model", "parts": [raw_model_text]})
            if len(conversation) > 8:
                conversation.pop(0)
    except Exception as e:
        logging.error(f"[BOT] Ошибка при обращении к Gemini: {e}")
        gemini_text = ("⚠️ Произошла ошибка при генерации ответа. Попробуйте ещё раз позже.")
    return gemini_text

# ---------------------- Запуск бота ---------------------- #
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
