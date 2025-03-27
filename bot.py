# ---------------------- Импорты ---------------------- #
import logging
import os
import re
import random
import aiohttp
import json
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
import tempfile
from aiogram.filters import Command
from pymorphy3 import MorphAnalyzer
from string import punctuation

import speech_recognition as sr
from pydub import AudioSegment
from gtts import gTTS

import datetime

from docx import Document
from PyPDF2 import PdfReader
from google.cloud import translate
from google.oauth2 import service_account


# ---------------------- ПУТИ К ФАЙЛАМ ---------------------- #
STATS_FILE = "stats.json"
DISABLED_CHATS_FILE = "disabled_chats.json"

# ---------------------- Инициализация переменных ---------------------- #
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

# Подключаем Gemini (Vertex AI)
import google.generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-2.0-flash")


# ---------------------- Загружаем/сохраняем статистику ---------------------- #
def load_stats() -> dict:
    if not os.path.exists(STATS_FILE):
        return {
            "messages_total": 0,
            "unique_users": set(),
            "files_received": 0,
            "commands_used": {}
        }
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data["unique_users"] = set(data.get("unique_users", []))
            return data
    except Exception as e:
        logging.warning(f"Не удалось загрузить {STATS_FILE}: {e}")
        return {
            "messages_total": 0,
            "unique_users": set(),
            "files_received": 0,
            "commands_used": {}
        }

def save_stats():
    data = {
        "messages_total": stats["messages_total"],
        "unique_users": list(stats["unique_users"]),
        "files_received": stats["files_received"],
        "commands_used": stats["commands_used"]
    }
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Не удалось сохранить {STATS_FILE}: {e}")

stats = load_stats()

def _register_message_stats(message: Message):
    """Добавляем ID чата или пользователя в уникальные."""
    stats["messages_total"] += 1

    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        stats["unique_users"].add(message.chat.id)
    else:
        stats["unique_users"].add(message.from_user.id)

    if message.text and message.text.startswith('/'):
        cmd = message.text.split()[0]
        stats["commands_used"][cmd] = stats["commands_used"].get(cmd, 0) + 1

    save_stats()

# ---------------------- Отключённые чаты ---------------------- #
def load_disabled_chats() -> set:
    if not os.path.exists(DISABLED_CHATS_FILE):
        return set()
    try:
        with open(DISABLED_CHATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        logging.warning(f"[BOT] Не удалось загрузить {DISABLED_CHATS_FILE}: {e}")
        return set()

def save_disabled_chats(chats: set):
    try:
        with open(DISABLED_CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(chats), f)
    except Exception as e:
        logging.warning(f"[BOT] Не удалось сохранить {DISABLED_CHATS_FILE}: {e}")

disabled_chats = load_disabled_chats()

ADMIN_ID = 1936733487

SUPPORT_PROMPT_TEXT = (
    "Отправьте любое сообщение (текст, фото, видео, файлы, аудио, голосовые) — всё дойдёт до поддержки."
)

def thread_kwargs(message: Message) -> dict:
    """Для супергрупп с топиками возвращаем словарь {message_thread_id: ...}."""
    if (
        message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]
        and message.message_thread_id is not None
    ):
        return {"message_thread_id": message.message_thread_id}
    return {}

# ---------------------- Поддержка ---------------------- #
support_mode_users = set()
support_reply_map = {}  # {admin_msg_id: user_id}

@dp.callback_query(F.data == "support_request")
async def handle_support_click(callback: CallbackQuery):
    await callback.answer()
    support_mode_users.add(callback.from_user.id)
    await callback.message.answer(SUPPORT_PROMPT_TEXT)

async def send_admin_reply_as_single_message(admin_message: Message, user_id: int):
    """Отправляет пользователю (или группе) одно сообщение: <b>Ответ от поддержки:</b> + контент."""
    prefix = "<b>Ответ от поддержки:</b>"

    if admin_message.text:
        reply_text = f"{prefix}\n{admin_message.text}"
        await bot.send_message(chat_id=user_id, text=reply_text)
    elif admin_message.photo:
        caption = prefix
        if admin_message.caption:
            caption += f"\n{admin_message.caption}"
        await bot.send_photo(chat_id=user_id, photo=admin_message.photo[-1].file_id, caption=caption)
    elif admin_message.voice:
        caption = prefix
        if admin_message.caption:
            caption += f"\n{admin_message.caption}"
        await bot.send_voice(chat_id=user_id, voice=admin_message.voice.file_id, caption=caption)
    elif admin_message.video:
        caption = prefix
        if admin_message.caption:
            caption += f"\n{admin_message.caption}"
        await bot.send_video(chat_id=user_id, video=admin_message.video.file_id, caption=caption)
    elif admin_message.document:
        caption = prefix
        if admin_message.caption:
            caption += f"\n{admin_message.caption}"
        await bot.send_document(chat_id=user_id, document=admin_message.document.file_id, caption=caption)
    elif admin_message.audio:
        caption = prefix
        if admin_message.caption:
            caption += f"\n{admin_message.caption}"
        await bot.send_audio(chat_id=user_id, audio=admin_message.audio.file_id, caption=caption)
    else:
        await bot.send_message(chat_id=user_id, text=f"{prefix}\n[Сообщение в неподдерживаемом формате]")

# ---------------------- Vandili Exchange (скрывает MOEX) ---------------------- #
VANDILI_API_URL = (
    "https://iss.moex.com/iss/engines/currency/markets/selt/"
    "securities/USD000UTSTOM.json?iss.meta=off&iss.only=marketdata"
)

async def get_vandili_exchange_rate():
    """Возвращает (price, update_time) с фактической МОЕХ, но без упоминания."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(VANDILI_API_URL) as response:
                text_data = await response.text()
                data = json.loads(text_data)
        md = data.get("marketdata", {})
        columns = md.get("columns", [])
        values = md.get("data", [])
        if not values:
            return None, None
        row = values[0]

        price = None
        updatetime = None

        if "LAST" in columns:
            idx_price = columns.index("LAST")
            price = row[idx_price]
        if "UPDATETIME" in columns:
            idx_time = columns.index("UPDATETIME")
            updatetime = row[idx_time]

        return price, updatetime
    except Exception as e:
        logging.warning(f"[Vandili Exchange] Ошибка при запросе: {e}")
        return None, None

async def process_vandili_currency_query(query: str) -> str | None:
    """
    Парсит '100 долларов в рубли', вызывает get_vandili_exchange_rate().
    Возвращает готовую строку для ответа.
    """
    pattern = re.compile(
        r'(\d+(?:[.,]\d+)?)\s*([a-zA-Zа-яА-ЯёЁ]+)\s*(?:в|to|->)\s*([a-zA-Zа-яА-ЯёЁ]+)',
        re.IGNORECASE
    )
    match = pattern.search(query)
    if not match:
        return None

    amount_str, src_raw, tgt_raw = match.groups()
    try:
        amount = float(amount_str.replace(',', '.'))
    except:
        return None

    # Упростим: (доллар -> рубли)
    src_candidates = ["usd", "доллар", "доллары", "долларов"]
    tgt_candidates = ["rub", "руб", "рубли", "рублей"]

    if src_raw.lower() in src_candidates and tgt_raw.lower() in tgt_candidates:
        price, updatetime = await get_vandili_exchange_rate()
        if not price:
            return (
                "Не удалось получить курс Vandili Exchange.\n"
                "Попробуйте позже."
            )

        now = datetime.datetime.now().strftime("%d %B %Y, %H:%M MSK")
        total = amount * float(price)

        return (
            f"На данный момент ({now}), {amount} USD ≈ {total:.2f} RUB.\n\n"
            "Курс может отличаться в банках и обменниках."
        )

    return None

# ---------------------- Погода (wttr.in) ---------------------- #
async def process_weather_query(query: str) -> str | None:
    """Определяем, спрашивают ли о погоде, и если да, пробуем взять с wttr.in."""
    if "погода" not in query.lower():
        return None
    m = re.search(r"(?:погода\s*(?:в|на)?\s*)([a-zа-яё -]+)", query, re.IGNORECASE)
    if not m:
        return None

    city_part = m.group(1).strip()
    forecast_3d = re.search(r"на\s*(3\s*дня|три\s*дня)", query, re.IGNORECASE)
    forecast_7d = re.search(r"на\s*(неделю|7\s*дней)", query, re.IGNORECASE)
    city_clean = re.sub(r"(на\s*\d+\s*дня|на\s*неделю|\d+\s*дней)", "", city_part, flags=re.IGNORECASE).strip()
    if not city_clean:
        return None

    if forecast_7d:
        days = 7
    elif forecast_3d:
        days = 3
    else:
        days = 1

    url = f"https://wttr.in/{city_clean}?format=j1&lang=ru"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return f"Не удалось получить данные о погоде для {city_clean}."
                data = await response.json()
    except Exception as e:
        logging.error(f"Ошибка при запросе погоды (wttr.in): {e}")
        return f"Ошибка при получении погоды для {city_clean}."

    current = data.get("current_condition", [])
    weather = data.get("weather", [])

    if days == 1:
        if not current:
            return f"Нет данных о текущей погоде для {city_clean}."
        cond = current[0]
        temp_c = cond.get("temp_C")
        feels_c = cond.get("FeelsLikeC")
        desc = cond.get("weatherDesc", [{}])[0].get("value", "")
        wind_speed = cond.get("windspeedKmph", "0")
        return (
            f"Сейчас в {city_clean.capitalize()}: {desc.lower()}, "
            f"температура {temp_c}°C (ощущается как {feels_c}°C), "
            f"ветер {wind_speed} км/ч."
        )
    else:
        if not weather:
            return f"Нет данных о прогнозе погоды для {city_clean}."
        forecast_lines = [f"Прогноз погоды для {city_clean.capitalize()}:"]
        for wday in weather[:days]:
            date_str = wday.get("date")
            mintemp = wday.get("mintempC")
            maxtemp = wday.get("maxtempC")
            hourly = wday.get("hourly", [])
            descs = []
            for hour_data in hourly:
                desc_val = hour_data.get("weatherDesc", [{}])[0].get("value", "")
                descs.append(desc_val)
            if descs:
                desc_common = max(set(descs), key=descs.count)
            else:
                desc_common = ""
            forecast_lines.append(
                f"{date_str}: от {mintemp}°C до {maxtemp}°C, {desc_common.lower()}"
            )
        return "\n".join(forecast_lines)

# ---------------------- Извлечение текста из файла ---------------------- #
user_documents = {}

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

# ---------------------- "Вай покажи ..." ---------------------- #
# Функция parse_russian_show_request должна стоять ДО handle_msg
IMAGE_TRIGGERS_RU = ["покажи", "покажи мне", "хочу увидеть", "пришли фото", "фото"]

def parse_russian_show_request(user_text: str):
    lower_text = user_text.lower()
    triggered = any(trig in lower_text for trig in IMAGE_TRIGGERS_RU)
    if not triggered:
        return (False, "", "", user_text)

    from string import punctuation
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

    leftover = user_text
    if raw_rus_word:
        pattern_remove = rf"(покажи( мне)?|хочу увидеть|пришли фото)\s+{re.escape(raw_rus_word)}"
        leftover = re.sub(pattern_remove, "", user_text, flags=re.IGNORECASE).strip()

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

    def fallback_translate_to_english(rus_w: str) -> str:
        try:
            project_id = "gen-lang-client-0588633435"
            location = "global"
            parent = f"projects/{project_id}/locations/{location}"
            response = translate_client.translate_text(
                parent=parent,
                contents=[rus_w],
                mime_type="text/plain",
                source_language_code="ru",
                target_language_code="en",
            )
            return response.translations[0].translated_text
        except Exception as e:
            logging.warning(f"Ошибка при переводе слова '{rus_w}': {e}")
            return rus_w

    if rus_word in RU_EN_DICT_CUSTOM:
        en_word = RU_EN_DICT_CUSTOM[rus_word]
    else:
        en_word = fallback_translate_to_english(rus_word)

    return (True, rus_word, en_word, leftover) if rus_word else (False, "", "", user_text)

# ---------------------- Получение изображения Unsplash ---------------------- #
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

def generate_short_caption(rus_word: str) -> str:
    short_prompt = (
        "ИНСТРУКЦИЯ: Ты — творческий помощник, который умеет писать очень короткие, дружелюбные подписи "
        "на русском языке. Не упоминай, что ты ИИ. Старайся не превышать 15 слов.\n\n"
        f"ЗАДАЧА: Придумай одну короткую, дружелюбную подпись для картинки с «{rus_word}». "
        "Можно с лёгкой эмоцией или юмором, не более 15 слов."
    )
    try:
        resp = model.generate_content([
            {"role": "user", "parts": [short_prompt]}
        ])
        raw_text = resp.text.strip()
        return format_gemini_response(raw_text)
    except Exception as e:
        logging.error(f"[BOT] Error generating short caption: {e}")
        return rus_word.capitalize()

# ---------------------- Голосовые сообщения ---------------------- #
@dp.message(lambda message: message.voice is not None)
async def handle_voice_message(message: Message):
    ...
    # (осталось то же - выше)

# ---------------------- Остальной код handle_all_messages, handle_msg, Gemini... ---------------------- #
# (Тот же код, но главное, что parse_russian_show_request уже объявлена.)

chat_history = {}

def format_gemini_response(text: str) -> str:
    ...
    # (тот же код)

async def generate_and_send_gemini_response(cid, full_prompt, show_image, rus_word, leftover):
    ...
    # (тот же код)

@dp.message()
async def handle_all_messages(message: Message):
    ...
    # (не меняем, всё осталось)
    await handle_msg(message)

async def handle_msg(message: Message, recognized_text: str = None):
    # <-- Тут мы уже можем безопасно вызвать parse_russian_show_request
    #    потому что она объявлена ВЫШЕ.
    ...
    # (тот же код: process_weather_query, process_vandili_currency_query, parse_russian_show_request, и т.д.)

# ---------------------- /broadcast ---------------------- #
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    ...
    # (тот же код)

# ---------------------- Запуск ---------------------- #
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
