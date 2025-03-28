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

# ---------------------- Вспомогательная функция для чтения файлов ---------------------- #
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
credentials = service_account.Credentials.from_service_account_file(key_path)
translate_client = translate.TranslationServiceClient(credentials=credentials)

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
morph = MorphAnalyzer()

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-2.0-flash")

# ---------------------- Храним историю диалогов и файлы ---------------------- #
chat_history = {}
user_documents = {}

# ---------------------- Поддержка и статистика ---------------------- #
stats = {
    "messages_total": 0,
    "unique_users": set(),
    "files_received": 0,
    "commands_used": {}
}
support_mode_users = set()
support_reply_map = {}  # {admin_msg_id: user_id}

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

# Глобальное множество для хранения chat_id (текущая сессия)
all_chat_ids = set()

def thread_kwargs(message: Message) -> dict:
    if (message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]
            and message.message_thread_id is not None):
        return {"message_thread_id": message.message_thread_id}
    return {}

def _register_message_stats(message: Message):
    stats["messages_total"] += 1
    # Если личка, сохраняем ID пользователя
    if message.chat.type == ChatType.PRIVATE:
        if message.from_user.id not in unique_users:
            unique_users.add(message.from_user.id)
            save_unique_users(unique_users)
    # Если группа, сохраняем ID чата
    elif message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if message.chat.id not in unique_groups:
            unique_groups.add(message.chat.id)
            save_unique_groups(unique_groups)
    stats["unique_users"].add(message.from_user.id)
    if message.text and message.text.startswith('/'):
        cmd = message.text.split()[0]
        stats["commands_used"][cmd] = stats["commands_used"].get(cmd, 0) + 1

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

# ---------------------- Словари для преобразования валют и транслитерации ---------------------- #
CURRENCY_SYNONYMS = {
    "доллар": "USD",
    "долларов": "USD",
    "бакс": "USD",
    "баксов": "USD",
    "евро": "EUR",
    "вона": "KRW",
    "вон": "KRW",
    "вонах": "KRW",
    "иен": "JPY",
    "иена": "JPY",
    "йена": "JPY",
    "юань": "CNY",
    "юаней": "CNY",
    "юаня": "CNY",
    "рубль": "RUB",
    "рублей": "RUB",
    "рублях": "RUB",
    "₽": "RUB",
    # Добавляем синонимы для узбекского сума
    "сум": "UZS",
    "сумов": "UZS",
    "сумы": "UZS",
    "узс": "UZS",
    "uzs": "UZS",
}

def normalize_currency_name(name: str) -> str:
    name_clean = name.lower().strip()
    return CURRENCY_SYNONYMS.get(name_clean, name.upper())

# ---------------------- Функция для получения курса через Floatrates ---------------------- #
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

# ---------------------- Морфологическая нормализация города ---------------------- #
def morphological_normalize_city_name(city_name: str) -> str:
    """
    Нормализует каждое слово в названии города (пример: «в ташкенте» -> «ташкент»).
    """
    words = city_name.split()
    normalized_words = []
    for w in words:
        parsed = morph.parse(w)[0]
        normalized_words.append(parsed.normal_form)
    return " ".join(normalized_words)

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
    # Сначала морфологически нормализуем город (например, "в ташкенте" -> "ташкент")
    city_name_norm = morphological_normalize_city_name(city_name)
    data = await do_geocoding_request(city_name_norm)
    if data:
        return data
    # Если не нашли, пробуем перевод
    try:
        project_id = "gen-lang-client-0588633435"
        location = "global"
        parent = f"projects/{project_id}/locations/{location}"
        response = translate_client.translate_text(
            parent=parent,
            contents=[city_name_norm],
            mime_type="text/plain",
            source_language_code="ru",
            target_language_code="en",
        )
        en_city = response.translations[0].translated_text
        data = await do_geocoding_request(en_city)
        if data:
            return data
    except Exception as e:
        logging.warning(f"Не удалось перевести город {city_name_norm}: {e}")
    # Если всё ещё нет, пробуем простую транслитерацию
    translit_city = simple_transliterate(city_name_norm)
    data = await do_geocoding_request(translit_city)
    return data

def weather_code_to_description(code: int) -> str:
    if code == 0:
        return "Ясно ☀️"
    elif code in [1, 2, 3]:
        return "Облачно ☁️"
    elif code in [45, 48]:
        return "Туман 🌫️"
    elif code in [51, 53, 55]:
        return "Небольшой дождь 🌦️"
    elif code in [56, 57]:
        return "Холодный дождь ❄️"
    elif code in [61, 63, 65]:
        return "Дождь 🌧️"
    elif code in [66, 67]:
        return "Ледяной дождь 🌨️"
    elif code in [71, 73, 75]:
        return "Снег 🌨️"
    elif code == 77:
        return "Снежные зерна ❄️"
    elif code in [80, 81, 82]:
        return "Ливень 🌦️"
    elif code in [85, 86]:
        return "Снежные ливни ❄️"
    elif code == 95:
        return "Гроза ⛈️"
    elif code in [96, 99]:
        return "Сильная гроза ⛈️"
    else:
        return "Неизвестная погода"

async def get_weather_info(city: str, days: int = 1) -> str:
    geo_data = await geocode_city(city)
    if not geo_data:
        return f"Город {city} не найден."
    lat = geo_data["lat"]
    lon = geo_data["lon"]
    timezone = geo_data["timezone"]
    if days == 1:
        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true&timezone={timezone}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(weather_url) as resp:
                    if resp.status != 200:
                        logging.warning(f"Ошибка получения текущей погоды: статус {resp.status}")
                        return None
                    weather_data = await resp.json()
        except Exception as e:
            logging.error(f"Ошибка запроса погоды: {e}")
            return None
        current = weather_data.get("current_weather", {})
        temp = current.get("temperature")
        wind = current.get("windspeed")
        weather_code = current.get("weathercode")
        description = weather_code_to_description(weather_code)
        return f"Погода в {city.capitalize()} сейчас: {description}, температура {temp}°C, ветер {wind} км/ч."
    else:
        weather_url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                       f"&daily=weathercode,temperature_2m_max,temperature_2m_min&timezone={timezone}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(weather_url) as resp:
                    if resp.status != 200:
                        logging.warning(f"Ошибка получения прогноза погоды: статус {resp.status}")
                        return None
                    weather_data = await resp.json()
        except Exception as e:
            logging.error(f"Ошибка запроса прогноза погоды: {e}")
            return None
        daily = weather_data.get("daily", {})
        dates = daily.get("time", [])
        weathercodes = daily.get("weathercode", [])
        temps_max = daily.get("temperature_2m_max", [])
        temps_min = daily.get("temperature_2m_min", [])
        forecast_lines = [f"Прогноз погоды в {city.capitalize()}:"]
        for i in range(min(days, len(dates))):
            desc = weather_code_to_description(weathercodes[i])
            forecast_lines.append(f"{dates[i]}: {desc}, от {temps_min[i]}°C до {temps_max[i]}°C")
        return "\n".join(forecast_lines)

# ---------------------- Обработчики команд ---------------------- #
@dp.message(Command("start"))
async def cmd_start(message: Message):
    _register_message_stats(message)
    all_chat_ids.add(message.chat.id)
    text_lower = message.text.lower()
    if message.chat.type == ChatType.PRIVATE and "support" in text_lower:
        support_mode_users.add(message.from_user.id)
        await message.answer(SUPPORT_PROMPT_TEXT)
        return
    greet = """Привет! Я <b>VAI</b> — твой интеллектуальный помощник 🤖

•🔊Я могу отвечать не только текстом, но и голосовыми сообщениями. Скажи "ответь голосом" или "ответь войсом".
•📄Читаю PDF, DOCX, TXT и .py-файлы — просто отправь мне файл.
•❓Отвечаю на вопросы по содержимому файла.
•👨‍💻Помогаю с кодом — напиши #рефактор и вставь код.
•🏞Показываю изображения по ключевым словам.
•☀️Погода: спроси "погода в Москве" или "погода в Ташкенте на 3 дня" 
•💱Курс валют: узнай курс "100 долларов в рублях", "100 USD в KRW" и т.д. 💱
•🔎Поддерживаю команды /help и режим поддержки.

Всегда на связи!"""
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if message.chat.id in disabled_chats:
            disabled_chats.remove(message.chat.id)
            save_disabled_chats(disabled_chats)
            logging.info(f"[BOT] Бот снова включён в группе {message.chat.id}")
        await message.answer(greet, **thread_kwargs(message))
        return
    await message.answer(greet)

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    _register_message_stats(message)
    await message.answer("Бот отключён 🚫", **thread_kwargs(message))
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        disabled_chats.add(message.chat.id)
        save_disabled_chats(disabled_chats)
        logging.info(f"[BOT] Бот отключён в группе {message.chat.id}")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    _register_message_stats(message)
    all_chat_ids.add(message.chat.id)
    if message.chat.type == ChatType.PRIVATE:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✉️ Написать в поддержку", callback_data="support_request")]]
        )
        await bot.send_message(chat_id=message.chat.id, text="Если возник вопрос или хочешь сообщить об ошибке — напиши нам:", reply_markup=keyboard)
    else:
        private_url = f"https://t.me/{BOT_USERNAME}?start=support"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✉️ Написать в поддержку", url=private_url)]]
        )
        await bot.send_message(chat_id=message.chat.id, text="Если возник вопрос или хочешь сообщить об ошибке — напиши мне в личку:", reply_markup=keyboard, **thread_kwargs(message))

@dp.message(Command("adminstats"))
async def cmd_adminstats(message: Message):
    _register_message_stats(message)
    if message.from_user.id != ADMIN_ID:
        return
    total_msgs = stats["messages_total"]
    unique_users_count = len(stats["unique_users"])
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
    # Команда доступна только админу
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

    # Объединяем сохранённые уникальные группы и пользователей
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
    await message.answer("Секундочку, я обрабатываю ваше голосовое сообщение...", **thread_kwargs(message))
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
        await handle_msg(message, recognized_text=recognized_text)

@dp.message()
async def handle_all_messages(message: Message):
    all_chat_ids.add(message.chat.id)
    if message.chat.id == ADMIN_ID and message.reply_to_message:
        original_id = message.reply_to_message.message_id
        if original_id in support_reply_map:
            user_id = support_reply_map[original_id]
            try:
                await send_admin_reply_as_single_message(message, user_id)
            except Exception as e:
                logging.warning(f"[BOT] Ошибка при отправке ответа админа пользователю: {e}")
        return
    _register_message_stats(message)
    uid = message.from_user.id
    cid = message.chat.id
    if uid in support_mode_users:
        support_mode_users.discard(uid)
        try:
            caption = message.caption or message.text or "[Без текста]"
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
    logging.info(f"[DEBUG] Message from {uid}: content_type={message.content_type}, text={message.text!r}")

    # Обработка запроса курса валют (например, "100 долларов в рублях")
    user_input = (message.text or "").strip()
    lower_input = user_input.lower()
    exchange_match = re.search(r"(\d+(?:[.,]\d+)?)\s*([a-zа-яё]+)\s*(в|to)\s*([a-zа-яё₽]+)", lower_input)
    if exchange_match:
        amount_str = exchange_match.group(1).replace(',', '.')
        try:
            amount = float(amount_str)
        except:
            amount = 0
        from_curr_raw = exchange_match.group(2)
        to_curr_raw = exchange_match.group(4)
        from_curr = normalize_currency_name(from_curr_raw)
        to_curr = normalize_currency_name(to_curr_raw)
        exchange_text = await get_exchange_rate(amount, from_curr, to_curr)
        if exchange_text:
            await message.answer(exchange_text, **thread_kwargs(message))
        else:
            await message.answer("Не удалось получить курс валюты.", **thread_kwargs(message))
        return

    # Обработка запроса погоды (например, "погода в ташкенте")
    if "погода" in lower_input:
        weather_match = re.search(r"погода(?:\s+в)?\s+([а-яё\s]+)(?:\s+на\s+(\d+|неделю))?", lower_input)
        if weather_match:
            city = weather_match.group(1).strip()
            days_str = weather_match.group(2)
            if days_str == "неделю":
                days = 7
            elif days_str and days_str.isdigit():
                days = int(days_str)
            else:
                days = 1
            weather_info = await get_weather_info(city, days)
            if weather_info:
                await message.answer(weather_info, **thread_kwargs(message))
            else:
                await message.answer("Не удалось получить данные о погоде.", **thread_kwargs(message))
            return

    await handle_msg(message)

@dp.message(F.text.lower().startswith("вай покажи"))
async def group_show_request(message: Message):
    await handle_msg(message)

# ---------------------- Генерация ответа через Gemini ---------------------- #
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

    # Если просто «покажи <слово>», без текста — сгенерируем короткую подпись
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
            conversation.append({"role": "assistant", "parts": [raw_model_text]})
            if len(conversation) > 8:
                conversation.pop(0)
    except Exception as e:
        logging.error(f"[BOT] Ошибка при обращении к Gemini: {e}")
        gemini_text = ("⚠️ Произошла ошибка при генерации ответа. Попробуйте ещё раз позже.")
    return gemini_text

CAPTION_LIMIT = 950
TELEGRAM_MSG_LIMIT = 4096
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

def get_prepositional_form(rus_word: str) -> str:
    parsed = morph.parse(rus_word)
    if not parsed:
        return rus_word
    p = parsed[0]
    loct = p.inflect({"loct"})
    return loct.word if loct else rus_word

def replace_pronouns_morph(leftover: str, rus_word: str) -> str:
    word_prep = get_prepositional_form(rus_word)
    pronoun_map = {
        r"\bо\s+нем\b":  f"о {word_prep}",
        r"\bо\s+нём\b":  f"о {word_prep}",
        r"\bо\s+ней\b":  f"о {word_prep}",
    }
    for pattern, repl in pronoun_map.items():
        leftover = re.sub(pattern, repl, leftover, flags=re.IGNORECASE)
    return leftover

def format_gemini_response(text: str) -> str:
    code_blocks = {}
    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        return placeholder

    # Вырезаем блоки ```lang\n...\n```
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)
    # Экранируем HTML
    text = escape(text)

    # Возвращаем код-блоки на место
    for placeholder, block_html in code_blocks.items():
        text = text.replace(escape(placeholder), block_html)

    # Форматирование **жирный**, *курсив*, `код`
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)

    # Убираем упоминания об изображениях, которых модель "не может вывести"
    text = re.sub(r"\[.*?(изображение|рисунок).+?\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(Я являюсь текстовым ассистентом.*выводить графику\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I am a text-based model.*cannot directly show images\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I can’t show images directly\.)", "", text, flags=re.IGNORECASE)

    # Преобразование списков * в «• »
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

    # Заменяем упоминания "I am a large language model" и "google" на что-то своё
    text = re.sub(r"(?i)\bi am a large language model\b", "I am VAI, created by Vandili", text)
    text = re.sub(r"(?i)\bi'm a large language model\b", "I'm VAI, created by Vandili", text)
    text = re.sub(r"(?i)\bgoogle\b", "Vandili", text)
    text = re.sub(r"я большая языковая модель(?:.*?)(?=\.)", "Я VAI, создан командой Vandili", text, flags=re.IGNORECASE)
    text = re.sub(r"я большая языковая модель", "Я VAI, создан командой Vandili", text, flags=re.IGNORECASE)
    text = re.sub(r"я\s*—\s*большая языковая модель", "Я — VAI, создан командой Vandili", text, flags=re.IGNORECASE)

    return text

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

async def handle_msg(message: Message, recognized_text: str = None):
    cid = message.chat.id
    user_input = recognized_text or (message.text or "").strip()
    voice_response_requested = False
    if user_input:
        lower_input = user_input.lower()
        if "ответь войсом" in lower_input or "ответь голосом" in lower_input or "голосом ответь" in lower_input:
            voice_response_requested = True
            user_input = re.sub(r"(ответь (войсом|голосом)|голосом ответь)", "", user_input, flags=re.IGNORECASE).strip()

    # Если пользователь загрузил файл и спрашивает "что в файле?" или "файл ..."
    if "файл" in user_input.lower() and message.from_user.id in user_documents:
        text = user_documents[message.from_user.id]
        short_summary_prompt = ("Кратко и по делу объясни, что делает этот код или что содержится в этом файле. "
                                "Изложи это для пользователя, который только что загрузил файл:\n\n" + text)
        gemini_response = await generate_and_send_gemini_response(cid, short_summary_prompt, False, "", "")
        await bot.send_message(chat_id=cid, text=gemini_response, **thread_kwargs(message))
        return

    # Если бот в группе, отвечаем только при упоминании
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        text_lower = user_input.lower()
        mention_bot = BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in text_lower
        is_reply_to_bot = (message.reply_to_message and message.reply_to_message.from_user and 
                           (message.reply_to_message.from_user.id == bot.id))
        mention_keywords = ["вай", "вэй", "vai"]
        if not mention_bot and not is_reply_to_bot and not any(k in text_lower for k in mention_keywords):
            return

    logging.info(f"[BOT] cid={cid}, text='{user_input}'")
    lower_inp = user_input.lower()

    # Быстрые ответы про имя бота
    if any(nc in lower_inp for nc in NAME_COMMANDS):
        await bot.send_message(chat_id=cid, text="Меня зовут <b>VAI</b>! 🤖", **thread_kwargs(message))
        return

    # Быстрые ответы про создателя
    if any(ic in lower_inp for ic in INFO_COMMANDS):
        await bot.send_message(chat_id=cid, text=random.choice(OWNER_REPLIES), **thread_kwargs(message))
        return

    # Проверяем, просит ли показать картинку
    show_image, rus_word, image_en, leftover = parse_russian_show_request(user_input)
    if show_image and rus_word:
        leftover = re.sub(r"\b(вай|vai)\b", "", leftover, flags=re.IGNORECASE).strip()
        leftover = replace_pronouns_morph(leftover, rus_word)
    leftover = leftover.strip()
    full_prompt = f"{rus_word} {leftover}".strip() if rus_word else leftover

    # Получаем ссылку на случайное фото с Unsplash, если нужно
    image_url = None
    if show_image:
        image_url = await get_unsplash_image_url(image_en, UNSPLASH_ACCESS_KEY)
    has_image = bool(image_url)

    # Генерируем текст через Gemini
    gemini_text = await generate_and_send_gemini_response(cid, full_prompt, show_image, rus_word, leftover)

    # Если пользователь просил ответить голосом
    if voice_response_requested:
        if not gemini_text:
            await bot.send_message(chat_id=cid, text="Нет ответа для голосового сообщения.", **thread_kwargs(message))
            return
        try:
            clean_text = re.sub(r'<[^>]+>', '', gemini_text)
            tts = gTTS(clean_text, lang='ru')
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_audio:
                tts.save(tmp_audio.name)
                mp3_path = tmp_audio.name

            audio = AudioSegment.from_file(mp3_path, format="mp3")
            ogg_path = mp3_path.replace(".mp3", ".ogg")
            audio.export(ogg_path, format="ogg")
            os.remove(mp3_path)

            await bot.send_voice(chat_id=cid, voice=FSInputFile(ogg_path, filename="voice.ogg"), **thread_kwargs(message))
            os.remove(ogg_path)
        except Exception as e:
            logging.error(f"Ошибка генерации голосового ответа: {e}")
            await bot.send_message(chat_id=cid, text="Произошла ошибка при генерации голосового ответа.", **thread_kwargs(message))
        return

    # Если нужно отправить фото
    if has_image:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(image_url) as r:
                if r.status == 200:
                    photo_bytes = await r.read()
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmpf:
                        tmpf.write(photo_bytes)
                        tmp_path = tmpf.name
                    try:
                        await bot.send_chat_action(chat_id=cid, action="upload_photo", **thread_kwargs(message))
                        file = FSInputFile(tmp_path, filename="image.jpg")
                        caption, rest = split_caption_and_text(gemini_text)
                        await bot.send_photo(chat_id=cid, photo=file, caption=caption if caption else "...", **thread_kwargs(message))
                        for c in rest:
                            await bot.send_message(chat_id=cid, text=c, **thread_kwargs(message))
                    finally:
                        os.remove(tmp_path)
    elif gemini_text:
        # Иначе просто отправляем текст
        chunks = split_smart(gemini_text, TELEGRAM_MSG_LIMIT)
        for c in chunks:
            await bot.send_message(chat_id=cid, text=c, **thread_kwargs(message))

# ---------------------- Запуск бота ---------------------- #
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
