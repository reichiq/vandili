# ---------------------- Импорты ---------------------- #
import logging
import matplotlib.pyplot as plt
import os
import re
from html import unescape, escape
import random
import aiohttp
import dateparser
import pytz
import requests
from datetime import datetime
from google.cloud import texttospeech
from io import BytesIO
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode, ChatType
from aiogram.types import (
    FSInputFile, Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, BufferedInputFile, ReplyKeyboardRemove
)
from aiogram.client.default import DefaultBotProperties
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
from collections import defaultdict
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
# ★ Добавляем хранилище для FSM
from aiogram.fsm.storage.memory import MemoryStorage

class ReminderAdd(StatesGroup):
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_text = State()
class ReminderEdit(StatesGroup):
    waiting_for_new_text = State()
    waiting_for_new_date = State()
    waiting_for_new_time = State()

def clean_for_tts(text: str) -> str:
    """
    Удаляет HTML-теги и заменяет спецсимволы (например, &nbsp; → пробел) для озвучки.
    """
    text = re.sub(r"<[^>]+>", "", text)   # удаляем HTML-теги
    return unescape(text).strip()

# ---------------------- Загрузка переменных окружения ---------------------- #
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/root/vandili/key.json"
credentials = service_account.Credentials.from_service_account_file("/root/vandili/key.json")
translate_client = translate.TranslationServiceClient(credentials=credentials)

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME")
# Приводим к строке для гарантии, что тип правильный (если вдруг значение None)
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY") or ""

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
# ★ Инициализируем диспетчер с MemoryStorage для FSM
dp = Dispatcher(storage=MemoryStorage())
morph = MorphAnalyzer()

genai.configure(api_key=GEMINI_API_KEY)
# Изменение модели на Gemini 2.5 Pro Experimental
model = genai.GenerativeModel(model_name="models/gemini-2.5-pro-exp-03-25")

# ---------------------- Загрузка и сохранение статистики ---------------------- #
STATS_FILE = "stats.json"
SUPPORT_MAP_FILE = "support_map.json"
NOTES_FILE = "notes.json"
REMINDERS_FILE = "reminders.json"
TIMEZONES_FILE = "timezones.json"

def load_timezones() -> dict:
    if not os.path.exists(TIMEZONES_FILE):
        return {}
    try:
        with open(TIMEZONES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Не удалось загрузить timezones.json: {e}")
        return {}

def save_timezones(timezones: dict):
    try:
        with open(TIMEZONES_FILE, "w", encoding="utf-8") as f:
            json.dump(timezones, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Не удалось сохранить timezones.json: {e}")

# Глобальный словарь для часовых поясов пользователей
user_timezones = load_timezones()

def load_reminders():
    if not os.path.exists(REMINDERS_FILE):
        return []
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # data — список словарей [{"user_id": ..., "datetime_utc": ..., "text": ...}]
            # Превратим datetime_utc обратно в datetime
            out = []
            for item in data:
                dt_str = item["datetime_utc"]
                dt_obj = datetime.fromisoformat(dt_str)
                out.append((item["user_id"], dt_obj, item["text"]))
            return out
    except:
        return []

def save_reminders():
    data_to_save = []
    for (user_id, dt_obj, text) in reminders:
        data_to_save.append({
            "user_id": user_id,
            "datetime_utc": dt_obj.isoformat(),
            "text": text
        })
    try:
        with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"[BOT] Не удалось сохранить reminders: {e}")

def load_notes() -> dict:
    if not os.path.exists(NOTES_FILE):
        return defaultdict(list)
    try:
        with open(NOTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return defaultdict(list, {int(k): v for k, v in data.items()})
    except:
        return defaultdict(list)

def save_notes():
    try:
        with open(NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(user_notes, f, ensure_ascii=False)
    except Exception as e:
        logging.warning(f"[BOT] Не удалось сохранить заметки: {e}")

def load_support_map() -> dict:
    if not os.path.exists(SUPPORT_MAP_FILE):
        return {}
    try:
        with open(SUPPORT_MAP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_support_map():
    try:
        with open(SUPPORT_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(support_reply_map, f)
    except Exception as e:
        logging.warning(f"Ошибка при сохранении support_map: {e}")

def load_stats() -> dict:
    """
    Загружает основные метрики (messages_total, files_received, commands_used) из stats.json.
    """
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
    """
    Сохраняет текущие метрики (messages_total, files_received, commands_used) в stats.json.
    """
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Не удалось сохранить stats.json: {e}")

def normalize_command(command: str) -> str:
    """
    Приводит команду к виду без @username.
    Например, '/start@VandiliBot' -> '/start'
    """
    return command.split('@')[0]

def get_normalized_command_stats(stats_dict: dict) -> dict:
    """
    Объединяет статистику команд, убирая @username из ключей.
    """
    raw_commands = stats_dict.get("commands_used", {})
    normalized_stats = {}

    for command, count in raw_commands.items():
        norm_cmd = normalize_command(command)
        normalized_stats[norm_cmd] = normalized_stats.get(norm_cmd, 0) + count

    return normalized_stats

def render_top_commands_bar_chart(commands_dict: dict) -> str:
    import matplotlib.pyplot as plt
    import tempfile

    if not commands_dict:
        return None

    normalized_stats = get_normalized_command_stats({"commands_used": commands_dict})
    sorted_cmds = sorted(normalized_stats.items(), key=lambda x: x[1], reverse=True)[:5]
    commands = [cmd.replace("@VandiliBot", "") for cmd, _ in sorted_cmds]
    counts = [cnt for _, cnt in sorted_cmds]

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.bar(commands, counts)

    ax.set_title("📊 Топ-5 команд")
    ax.set_xlabel("Команды")
    ax.set_ylabel("Использований")

    for bar in bars:
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2, height + 0.5, f"{int(height)}",
                ha='center', va='bottom', fontsize=10)

    plt.tight_layout()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmpf:
        plt.savefig(tmpf.name)
        return tmpf.name

# ---------------------- Глобальные структуры ---------------------- #
stats = load_stats()  # подгружаем основные метрики
pending_note_or_reminder = {}
support_mode_users = set()
support_reply_map = load_support_map()
chat_history = {}
user_documents = {}
user_notes = load_notes()
reminders = []  # Список кортежей: (user_id, event_utc: datetime, text)
reminders = load_reminders()

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
EESKELA_ID = 6208034574
SUPPORT_IDS = {ADMIN_ID, EESKELA_ID}
SUPPORT_PROMPT_TEXT = ("Отправьте любое сообщение (текст, фото, видео, файлы, аудио, голосовые) — всё дойдёт до поддержки.")

# Глобальное множество для хранения chat_id (текущая сессия)
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
        cmd = message.text.split()[0].strip().lower()
        cmd = cmd.split("@")[0].lstrip("!/")  # удаляем / ! и @VandiliBot
        cmd = f"/{cmd}"  # нормализуем обратно с префиксом
        stats["commands_used"][cmd] = stats["commands_used"].get(cmd, 0) + 1
        save_stats()

# ---------------------- Функция отправки ответа админа одним сообщением ---------------------- #
async def send_admin_reply_as_single_message(admin_message: Message, user_id: int):
    sender_id = admin_message.from_user.id
    if sender_id == ADMIN_ID:
        prefix = "<b>📩 Ответ от службы поддержки. С вами — 👾 Admin:</b>"
    elif sender_id == EESKELA_ID:
        prefix = "<b>📩 Ответ от службы поддержки. С вами — 💭 eeskela:</b>"
    else:
        prefix = "<b>📩 Ответ от поддержки:</b>"

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
# Добавлено правило для "долар" с одной "л" для обработки опечаток
CURRENCY_SYNONYMS = {
    "доллар": "USD", "доллары": "USD", "долларов": "USD",
    "долар": "USD",
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
# Он обрабатывает запросы вида: "1 доллар сум" и "1 доллар в сум", а также с английскими обозначениями.
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
    city_name = city_name.strip().lower()

    # Исключения, чтобы не ломать явно известные города
    KNOWN_CITIES = {
        "москва": "Europe/Moscow",
        "ташкент": "Asia/Tashkent",
        "санкт-петербург": "Europe/Saint Petersburg",
        "петербург": "Europe/Saint Petersburg",
        "сеул": "Asia/Seoul",
        "алматы": "Asia/Almaty",
        "астана": "Asia/Astana"
    }

    if city_name in KNOWN_CITIES:
        return {
            "lat": 0,
            "lon": 0,
            "timezone": KNOWN_CITIES[city_name]
        }

    # 1. Сначала пробуем как есть
    result = await do_geocoding_request(city_name)
    if result:
        return result

    # 2. Пробуем перевод через Google Translate
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
        translated = response.translations[0].translated_text
        result = await do_geocoding_request(translated)
        if result:
            return result
    except Exception as e:
        logging.warning(f"[BOT] Ошибка перевода города '{city_name}': {e}")

    # 3. Пробуем транслитерацию
    translit_city = simple_transliterate(city_name)
    return await do_geocoding_request(translit_city)

# Новый вспомогательный метод для форматирования погодного описания с добавлением смайлика
def format_condition(condition_text: str) -> str:
    weather_emojis = {
        "ясно": "☀️",
        "солнечно": "☀️",
        "солнечная": "☀️",  # Добавлено для вариантов "солнечная"
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

# Новая функция получения погоды через WeatherAPI.com
async def get_weather_info(city: str, days: int = 1, mode: str = "") -> str:
    base_url = "http://api.weatherapi.com/v1/forecast.json"
    params = {
        "key": WEATHER_API_KEY,
        "q": city,
        "days": max(days, 1),
        "lang": "ru",
        "aqi": "no",
        "alerts": "no"
    }
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
    client = texttospeech.TextToSpeechClient()

    clean_text = clean_for_tts(text)  # 💥 вот эта строка — очищаем HTML

    synthesis_input = texttospeech.SynthesisInput(text=clean_text)

    voice = texttospeech.VoiceSelectionParams(
        language_code="ru-RU", name="ru-RU-Wavenet-C"
    )

    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.OGG_OPUS
    )

    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )

    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as out:
        out.write(response.audio_content)
        out_path = out.name

    await bot.send_voice(chat_id=chat_id, voice=FSInputFile(out_path, filename="voice.ogg"))
    os.remove(out_path)

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

    # 🔍 Обработка deep-link параметров
    if command.args == "mynotes":
        await show_notes(message.chat.id, message=message)
        return
    elif command.args == "myreminders":
        await show_reminders(message.chat.id)
        return
    elif command.args == "support":
        support_mode_users.add(message.from_user.id)
        await message.answer(SUPPORT_PROMPT_TEXT)
        return

    greet = """Привет! Я <b>VAI</b> — твой интеллектуальный помощник 🤖

•🔊Я могу отвечать не только текстом, но и голосовыми сообщениями. Скажи "ответь голосом" или "ответь войсом".
•📄Читаю PDF, DOCX, TXT и .py-файлы — просто отправь мне файл.
•❓Отвечаю на вопросы по содержимому файла.
•👨‍💻Помогаю с кодом — напиши #рефактор и вставь код.
•🏞Показываю изображения по ключевым словам.
•☀️Погода: спроси "погода в Москве" или "погода в Варшаве на 3 дня"
•💱Курс валют: узнай курс "100 долларов в рублях", "100 USD в KRW" и т.д. 
•📝 Заметки: используй команду /mynotes — ты сможешь добавлять, редактировать и удалять свои заметки через кнопки. 
•⏰ Напоминания: команда /myreminders — добавление, редактирование, удаление напоминаний. Добавление реализовано через пошаговые кнопки (дата → время → текст).
•🔎Поддерживаю команды /help и режим поддержки.

Всегда на связи!"""

    # 🌐 Если сообщение из группы
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if message.chat.id in disabled_chats:
            disabled_chats.remove(message.chat.id)
            save_disabled_chats(disabled_chats)
            logging.info(f"[BOT] Бот снова включён в группе {message.chat.id}")
        await message.answer("Бот включён ✅")
        await message.answer(greet)
        return

    # 📩 Если в ЛС
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
        await bot.send_message(chat_id=message.chat.id, text="Если возник вопрос или хочешь сообщить об ошибке — напишите нам:", reply_markup=keyboard, **thread(message))
    else:
        private_url = f"https://t.me/{BOT_USERNAME}?start=support"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✉️ Написать в поддержку", url=private_url)]]
        )
        await bot.send_message(chat_id=message.chat.id, text="Если возник вопрос или хочешь сообщить об ошибке — напишите нам:", reply_markup=keyboard, **thread(message))

@dp.message(Command("adminstats"))
async def cmd_adminstats(message: Message):
    _register_message_stats(message)
    if message.from_user.id not in SUPPORT_IDS:
        return

    total_msgs = stats.get("messages_total", 0)
    unique_users_list = stats.get("unique_users", [])
    unique_users_count = len(set(unique_users_list))
    files_received = stats.get("files_received", 0)
    cmd_usage = stats.get("commands_used", {})

    total_cmds = sum(cmd_usage.values())
    avg_per_user = round(total_msgs / unique_users_count, 2) if unique_users_count else "—"

    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"💬 Всего сообщений: <b>{total_msgs}</b>\n"
        f"👤 Уникальных пользователей: <b>{unique_users_count}</b>\n"
        f"📎 Получено файлов: <b>{files_received}</b>\n"
        f"🧠 Команд выполнено: <b>{total_cmds}</b>\n"
        f"📈 Среднее сообщений на пользователя: <b>{avg_per_user}</b>"
    )

    chart_path = render_top_commands_bar_chart(cmd_usage)
    if chart_path:
        await message.answer_photo(photo=FSInputFile(chart_path, filename="top_commands.png"), caption=text)
        os.remove(chart_path)
    else:
        await message.answer(text + "\nНет данных по командам.")

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

# ★ Изменён обработчик команды /mynotes – теперь без prefix, чтобы команда срабатывала корректно
@dp.message(Command("mynotes"))
async def show_notes_command(message: Message):
    _register_message_stats(message)
    if message.chat.type != ChatType.PRIVATE:
        private_url = f"https://t.me/{BOT_USERNAME}?start=mynotes"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Открыть мои заметки", url=private_url)]
        ])
        await message.answer("Эта команда доступна только в личных сообщениях.", reply_markup=keyboard)
        return
    await show_notes(message.chat.id)

@dp.message(Command("myreminders", prefix="/!"))
async def show_reminders_command(message: Message):
    _register_message_stats(message)
    if message.chat.type != ChatType.PRIVATE:
        private_url = f"https://t.me/{BOT_USERNAME}?start=myreminders"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Открыть мои напоминания", url=private_url)]
        ])
        await message.answer("Эта команда доступна только в личных сообщениях.", reply_markup=keyboard)
        return
    await show_reminders(message.chat.id)


@dp.callback_query(F.data.startswith("note_type:"))
async def handle_note_type_choice(callback: CallbackQuery):
    user_id = callback.from_user.id
    choice = callback.data.split(":")[1]
    original_text = pending_note_or_reminder.pop(user_id, None)

    if not original_text:
        await callback.message.edit_text("Нет ожидающего текста для обработки.")
        return

    await callback.answer()

    if choice == "note":
        user_notes[user_id].append(original_text)
        save_notes()
        await callback.message.edit_text("📝 Сохранил как заметку.")
    elif choice == "reminder":
        await callback.message.edit_text(
            "Хорошо, напомни мне так: «напомни {текст} по Москве», чтобы я установил напоминание.\n\n"
            "Например: <i>напомни завтра в 10:00 купить хлеб по Москве</i>"
        )

@dp.message(lambda message: message.text and message.text.lower().startswith("мой"))
async def handle_timezone_setting(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    tz_match = re.match(r"(?i)^мой\s+(город|часовой\s+пояс)\s*[:\-—]?\s*(.+?)\s*[!.\-…]*\s*$", text)
    if not tz_match:
        await message.answer(
            "Чтобы установить часовой пояс, напишите сообщение в формате:\n"
            "<b>Мой часовой пояс: Europe/Moscow</b>\n"
            "или\n"
            "<b>Мой город: Москва</b>",
            parse_mode="HTML"
        )
        return

    setting_type = tz_match.group(1).lower()
    value = tz_match.group(2).strip()

    if "город" in setting_type:
        value = normalize_city_name(value)
        geo = await geocode_city(value)
        if not geo or "timezone" not in geo:
            await message.answer(
                f"❌ Не удалось определить часовой пояс для <b>{value}</b>.\n"
                "Попробуй указать другой город или написать: <code>Мой часовой пояс: Europe/Warsaw</code>"
            )
            return
        tz_str = geo["timezone"]

        user_timezones[user_id] = tz_str
        save_timezones(user_timezones)

        await message.answer(
            f"Запомнил: <b>{value.capitalize()}</b> ✅\n"
            f"Теперь я буду использовать часовой пояс: <code>{tz_str}</code> для напоминаний."
        )

    else:
        tz_str = value
        user_timezones[user_id] = tz_str
        save_timezones(user_timezones)

        await message.answer(
            f"Часовой пояс установлен: <code>{tz_str}</code>. "
            f"Теперь я буду использовать его для напоминаний."
        )

    # 🔧 ШАГ 2: если раньше было ожидающее напоминание — обрабатываем его
    reminder_data = pending_note_or_reminder.get(user_id)
    if reminder_data and not reminder_data.get("was_retried"):
        pending_note_or_reminder[user_id]["was_retried"] = True
        prev_text = reminder_data["text"]
        await handle_reminder(
            type("FakeMessage", (object,), {
                "from_user": type("U", (), {"id": user_id})(),
                "text": prev_text,
                "answer": message.answer
            })
        )

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
    if not recognized_text:
        await message.answer("Извините, я не смог распознать голосовое сообщение 😔")
        return
    voice_regex = re.compile(r"(ответь\s+(войсом|голосом)|голосом\s+ответь)", re.IGNORECASE)
    voice_response_requested = bool(voice_regex.search(recognized_text))
    cleaned_text = voice_regex.sub("", recognized_text).strip()
    await handle_msg(message, recognized_text=cleaned_text, voice_response_requested=voice_response_requested)

@dp.callback_query(F.data.startswith("note_delete:"))
async def delete_note(callback: CallbackQuery):
    uid = callback.from_user.id
    index = int(callback.data.split(":")[1])
    notes = user_notes.get(uid, [])
    if 0 <= index < len(notes):
        notes.pop(index)
        save_notes()
    await show_notes(uid)

@dp.callback_query(F.data == "note_delete_all")
async def delete_all_notes(callback: CallbackQuery):
    uid = callback.from_user.id
    user_notes[uid] = []
    save_notes()
    await show_notes(uid, callback=callback)

@dp.callback_query(F.data == "note_add")
async def ask_add_note(callback: CallbackQuery):
    await callback.message.delete()
    uid = callback.from_user.id
    pending_note_or_reminder[uid] = {"type": "note"}
    await callback.message.answer("✍️ Введи новую заметку.")

@dp.callback_query(F.data.startswith("note_edit:"))
async def ask_edit_note(callback: CallbackQuery):
    uid = callback.from_user.id
    index = int(callback.data.split(":")[1])
    notes = user_notes.get(uid, [])
    if 0 <= index < len(notes):
        pending_note_or_reminder[uid] = {"type": "edit_note", "index": index}
        await callback.message.answer(f"✏️ Отправь новый текст для заметки №{index+1}.")
    else:
        await callback.message.answer("Такой заметки не найдено.")

@dp.callback_query(F.data == "note_close")
async def close_notes(callback: CallbackQuery):
    await callback.message.delete()

@dp.callback_query(F.data.startswith("reminder_delete:"))
async def delete_reminder(callback: CallbackQuery):
    uid = callback.from_user.id
    index = int(callback.data.split(":")[1])
    user_reminders = [(i, r) for i, r in enumerate(reminders) if r[0] == uid]
    if 0 <= index < len(user_reminders):
        real_index = user_reminders[index][0]
        reminders.pop(real_index)
        save_reminders()
    await show_reminders(uid, callback=callback)

@dp.callback_query(F.data.startswith("reminder_edit:"))
async def ask_edit_reminder(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    index = int(callback.data.split(":")[1])
    user_reminders = [(i, r) for i, r in enumerate(reminders) if r[0] == uid]

    if 0 <= index < len(user_reminders):
        real_index = user_reminders[index][0]
        old_uid, old_dt, old_text = reminders[real_index]

        await state.update_data(reminder_index=real_index, old_text=old_text, old_dt=old_dt)
        await state.set_state(ReminderEdit.waiting_for_new_text)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="edit_skip_text")]
        ])
        await callback.message.answer(
            f"✏️ Введи новый текст напоминания или нажми <b>Пропустить</b>:\n\n"
            f"📌 <i>{old_text}</i>",
            reply_markup=keyboard
        )

    else:
        await callback.message.answer("Такого напоминания нет.")

@dp.message(ReminderEdit.waiting_for_new_text)
async def edit_reminder_text(message: Message, state: FSMContext):
    new_text = message.text.strip()
    data = await state.get_data()
    await state.update_data(new_text=None if new_text.lower() == "пропустить" else new_text)
    await state.set_state(ReminderEdit.waiting_for_new_date)

    old_dt = data.get("old_dt")
    old_local = old_dt.astimezone(pytz.timezone(user_timezones.get(message.from_user.id, "UTC")))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data="edit_skip_date")]
    ])
    await message.answer(
        f"📅 Введи новую дату в формате <code>ДД.ММ.ГГГГ</code>\nили нажми <b>Пропустить</b>.\n\n"
        f"Текущая дата: <code>{old_local.strftime('%d.%m.%Y')}</code>",
        reply_markup=keyboard
    )


@dp.message(ReminderEdit.waiting_for_new_date)
async def edit_reminder_date(message: Message, state: FSMContext):
    raw = message.text.strip()
    data = await state.get_data()

    if raw.lower() == "пропустить":
        await state.update_data(new_date=None)
        await state.set_state(ReminderEdit.waiting_for_new_time)
        old_dt = data.get("old_dt")
        old_local = old_dt.astimezone(pytz.timezone(user_timezones.get(message.from_user.id, "UTC")))
        await message.answer(
            f"⏰ Введи новое время в формате <code>ЧЧ:ММ</code>,\nили напиши <b>Пропустить</b>.\n\n"
            f"Текущее время: <code>{old_local.strftime('%H:%M')}</code>"
        )
        return

    try:
        date_obj = datetime.strptime(raw, "%d.%m.%Y").date()
        await state.update_data(new_date=date_obj)
        await state.set_state(ReminderEdit.waiting_for_new_time)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="edit_skip_time")]
        ])
        await message.answer(
            "⏰ Введи новое время в формате <code>ЧЧ:ММ</code>\nили нажми <b>Пропустить</b>.",
            reply_markup=keyboard
        )
    except ValueError:
        await message.answer("⚠️ Неверный формат даты. Пример: <code>12.04.2025</code>")

@dp.message(ReminderEdit.waiting_for_new_time)
async def edit_reminder_time(message: Message, state: FSMContext):
    user_id = message.from_user.id
    raw = message.text.strip()
    data = await state.get_data()

    old_dt: datetime = data.get("old_dt")
    old_text: str = data.get("old_text")
    index: int = data.get("reminder_index")
    new_text = data.get("new_text") or old_text
    new_date = data.get("new_date") or old_dt.astimezone(pytz.timezone(user_timezones.get(user_id, "UTC"))).date()

    if raw.lower() == "пропустить":
        new_time = old_dt.astimezone(pytz.timezone(user_timezones.get(user_id, "UTC"))).time()
    else:
        try:
            new_time = datetime.strptime(raw, "%H:%M").time()
        except ValueError:
            await message.answer("⚠️ Неверный формат времени. Пример: <code>15:30</code>")
            return

    tz_str = user_timezones.get(user_id)
    if not tz_str:
        await message.answer("❌ Не найден часовой пояс. Напиши: <code>Мой город: Москва</code>")
        await state.clear()
        return

    try:
        local_tz = pytz.timezone(tz_str)
        dt_local = datetime.combine(new_date, new_time)
        dt_localized = local_tz.localize(dt_local)
        dt_utc = dt_localized.astimezone(pytz.utc)

        reminders[index] = (user_id, dt_utc, new_text)
        save_reminders()
        await message.answer(f"✅ Напоминание обновлено: <b>{new_text}</b> — <code>{dt_local.strftime('%d.%m.%Y %H:%M')}</code> ({tz_str})")
    except Exception as e:
        logging.warning(f"[REMINDER_EDIT] Ошибка: {e}")
        await message.answer("❌ Не удалось обновить напоминание.")
    await state.clear()


@dp.callback_query(F.data == "edit_skip_text")
async def skip_edit_text(callback: CallbackQuery, state: FSMContext):
    await state.update_data(new_text=None)
    data = await state.get_data()
    old_dt = data.get("old_dt")
    old_local = old_dt.astimezone(pytz.timezone(user_timezones.get(callback.from_user.id, "UTC")))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data="edit_skip_date")]
    ])
    await state.set_state(ReminderEdit.waiting_for_new_date)
    await callback.message.edit_text(
        f"📅 Введи новую дату в формате <code>ДД.ММ.ГГГГ</code>\nили нажми <b>Пропустить</b>.\n\n"
        f"Текущая дата: <code>{old_local.strftime('%d.%m.%Y')}</code>",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "edit_skip_date")
async def skip_edit_date(callback: CallbackQuery, state: FSMContext):
    await state.update_data(new_date=None)
    data = await state.get_data()
    old_dt = data.get("old_dt")
    old_local = old_dt.astimezone(pytz.timezone(user_timezones.get(callback.from_user.id, "UTC")))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data="edit_skip_time")]
    ])
    await state.set_state(ReminderEdit.waiting_for_new_time)
    await callback.message.edit_text(
        f"⏰ Введи новое время в формате <code>ЧЧ:ММ</code>\nили нажми <b>Пропустить</b>.\n\n"
        f"Текущее время: <code>{old_local.strftime('%H:%M')}</code>",
        reply_markup=keyboard
    )

@dp.callback_query(F.data == "edit_skip_time")
async def skip_edit_time(callback: CallbackQuery, state: FSMContext):
    message = callback.message
    message.from_user = callback.from_user  # чтобы переиспользовать message-хендлер
    await edit_reminder_time(message, state)


@dp.callback_query(F.data == "reminder_delete_all")
async def confirm_delete_all_reminders(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="reminder_cancel_delete_all"),
            InlineKeyboardButton(text="✅ Удалить всё", callback_data="reminder_confirm_delete_all")
        ]
    ])
    await callback.message.answer("Ты точно хочешь удалить <b>все</b> напоминания?", reply_markup=keyboard)

@dp.callback_query(F.data == "reminder_confirm_delete_all")
async def do_delete_all_reminders(callback: CallbackQuery):
    uid = callback.from_user.id
    global reminders
    reminders = [r for r in reminders if r[0] != uid]
    save_reminders()
    await show_reminders(uid, callback=callback)

@dp.callback_query(F.data == "reminder_cancel_delete_all")
async def cancel_delete_all_reminders(callback: CallbackQuery):
    await callback.message.answer("Удаление отменено.")

@dp.callback_query(F.data == "reminder_close")
async def close_reminders(callback: CallbackQuery):
    await callback.message.delete()

@dp.callback_query(F.data == "reminder_add")
async def start_reminder_add(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer("📅 Введи дату напоминания в формате <b>ДД.ММ.ГГГГ</b>\n\nПример: <code>12.04.2025</code>")
    await state.set_state(ReminderAdd.waiting_for_date)

@dp.message(ReminderAdd.waiting_for_date)
async def process_reminder_date(message: Message, state: FSMContext):
    try:
        date_obj = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
        await state.update_data(date=date_obj)
        await message.answer("⏰ Теперь введи время в формате <b>ЧЧ:ММ</b>\nПример: <code>15:30</code>")
        await state.set_state(ReminderAdd.waiting_for_time)
    except ValueError:
        await message.answer("⚠️ Неверный формат даты. Попробуй снова. Пример: <code>12.04.2025</code>")

@dp.message(ReminderAdd.waiting_for_time)
async def process_reminder_time(message: Message, state: FSMContext):
    try:
        time_obj = datetime.strptime(message.text.strip(), "%H:%M").time()
        await state.update_data(time=time_obj)
        await message.answer("✍️ Введи текст напоминания (что нужно напомнить)")
        await state.set_state(ReminderAdd.waiting_for_text)
    except ValueError:
        await message.answer("⚠️ Неверный формат времени. Пример: <code>15:30</code>")

@dp.message(ReminderAdd.waiting_for_text)
async def process_reminder_text(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    date = data.get("date")
    time = data.get("time")
    text = message.text.strip()

    if not date or not time:
        await message.answer("❌ Ошибка: отсутствуют дата или время. Попробуй снова.")
        await state.clear()
        return

    dt_local = datetime.combine(date, time)
    tz_str = user_timezones.get(user_id)
    if not tz_str:
        await message.answer("⏳ Чтобы установить напоминание, напиши:\n<code>Мой город: Москва</code>")
        pending_note_or_reminder[user_id] = {
            "text": text,
            "type": "reminder",
            "date": date,
            "time": time
        }
        await state.clear()
        return

    try:
        local_tz = pytz.timezone(tz_str)
        dt_localized = local_tz.localize(dt_local)
        dt_utc = dt_localized.astimezone(pytz.utc)
    except Exception as e:
        logging.warning(f"[FSM] Ошибка при преобразовании даты: {e}")
        await message.answer("❌ Не удалось определить время. Убедись, что всё введено корректно.")
        await state.clear()
        return

    reminders.append((user_id, dt_utc, text))
    save_reminders()
    await message.answer(f"✅ Напоминание установлено на <code>{dt_local.strftime('%Y-%m-%d %H:%M')}</code> ({tz_str})")
    await state.clear()

from datetime import timedelta

async def handle_reminder(message: Message):
    user_id = message.from_user.id
    reminder_data = pending_note_or_reminder.pop(user_id, None)
    if not reminder_data:
        await message.answer("❌ Не удалось обработать напоминание.")
        return

    tz_str = user_timezones.get(user_id)
    if not tz_str:
        await message.answer("❌ Не удалось найти часовой пояс.")
        return

    try:
        local_tz = pytz.timezone(tz_str)

        # Если есть введённые дата и время — используем их
        date = reminder_data.get("date")
        time = reminder_data.get("time")
        if date and time:
            dt_local = datetime.combine(date, time)
        else:
            # Иначе — ближайшая минута
            dt_local = datetime.now(local_tz) + timedelta(minutes=1)

        dt_localized = local_tz.localize(dt_local)
        dt_utc = dt_localized.astimezone(pytz.utc)

        reminders.append((user_id, dt_utc, reminder_data["text"]))
        save_reminders()
        await message.answer(f"✅ Напоминание установлено на <code>{dt_local.strftime('%Y-%m-%d %H:%M')}</code> ({tz_str})")
    except Exception as e:
        logging.warning(f"[DELAYED_REMINDER] Ошибка: {e}")
        await message.answer("❌ Не удалось установить напоминание.")

@dp.message()
async def handle_all_messages(message: Message):
    user_input = (message.text or "").strip()
    await handle_all_messages_impl(message, user_input)

# ★ Изменена функция show_notes – если нет заметок, всегда отправляем сообщение
async def show_notes(uid: int, callback: CallbackQuery = None, message: Message = None):
    notes = user_notes.get(uid, [])

    # Удаляем предыдущее сообщение (если оно есть)
    try:
        if callback:
            await callback.message.delete()
        elif message:
            await message.delete()
    except:
        pass

    # Если нет заметок, отправляем сообщение с кнопками
    if not notes:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить", callback_data="note_add")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="note_close")]
        ])
        await bot.send_message(uid, "📭 У тебя пока нет заметок.", reply_markup=keyboard)
        return

    text = "<b>Твои заметки:</b>\n"
    buttons = []
    for i, note in enumerate(notes):
        text += f"{i+1}. {note}\n"
        buttons.append([
            InlineKeyboardButton(text=f"✏️ {i+1}", callback_data=f"note_edit:{i}"),
            InlineKeyboardButton(text=f"🗑 {i+1}", callback_data=f"note_delete:{i}")
        ])
    buttons.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="note_add"),
        InlineKeyboardButton(text="🧹 Удалить все", callback_data="note_delete_all")
    ])
    buttons.append([
        InlineKeyboardButton(text="❌ Закрыть", callback_data="note_close")
    ])
    await bot.send_message(uid, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

async def show_reminders(uid: int, callback: CallbackQuery = None):
    # Удаляем устаревшие напоминания
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    global reminders
    reminders = [r for r in reminders if not (r[0] == uid and r[1] <= now_utc)]
    save_reminders()
    
    user_rem = [(i, r) for i, r in enumerate(reminders) if r[0] == uid]
    if callback:
        try:
            await callback.message.delete()
        except:
            pass

    if not user_rem:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить", callback_data="reminder_add")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="reminder_close")]
        ])
        await bot.send_message(uid, "📭 У тебя пока нет напоминаний.", reply_markup=keyboard)
        return
    text = "<b>Твои напоминания:</b>\n"
    buttons = []
    for i, (real_i, (_, dt, msg)) in enumerate(user_rem):
        local = dt.astimezone(pytz.timezone(user_timezones.get(uid, "UTC")))
        text += f"{i+1}. {msg} — <code>{local.strftime('%d.%m.%Y %H:%M')}</code>\n"
        buttons.append([
            InlineKeyboardButton(text=f"✏️ {i+1}", callback_data=f"reminder_edit:{i}"),
            InlineKeyboardButton(text=f"🗑 {i+1}", callback_data=f"reminder_delete:{i}")
        ])
    buttons.append([
        InlineKeyboardButton(text="➕ Добавить", callback_data="reminder_add"),
        InlineKeyboardButton(text="🧹 Удалить все", callback_data="reminder_delete_all")
    ])
    buttons.append([
        InlineKeyboardButton(text="❌ Закрыть", callback_data="reminder_close")
    ])
    await bot.send_message(uid, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

async def handle_all_messages_impl(message: Message, user_input: str):
    _register_message_stats(message)
    all_chat_ids.add(message.chat.id)
    uid = message.from_user.id
    cid = message.chat.id

    voice_response_requested = False  # исправление UnboundLocalError
    if uid in pending_note_or_reminder:
        data = pending_note_or_reminder.pop(uid)
        if data["type"] == "note":
            user_notes[uid].append(user_input)
            save_notes()
            await show_notes(uid)
            return
        elif data["type"] == "edit_note":
            index = data.get("index")
            if index is not None and 0 <= index < len(user_notes.get(uid, [])):
                user_notes[uid][index] = user_input
                save_notes()
                await show_notes(uid)
            else:
                await message.answer("Не удалось найти заметку для редактирования.")
            return

    # Если админ отвечает на сообщение поддержки
    if message.from_user.id in SUPPORT_IDS and message.reply_to_message:
        original_id = message.reply_to_message.message_id
        if (message.chat.id, original_id) in support_reply_map:
            user_id = support_reply_map[(message.chat.id, original_id)]
            try:
                await send_admin_reply_as_single_message(message, user_id)
                if message.from_user.id != ADMIN_ID:
                    sender = message.from_user
                    sender_name = sender.full_name
                    sender_username = f"@{sender.username}" if sender.username else f"(ID: <code>{sender.id}</code>)"
                    try:
                        user = await bot.get_chat(user_id)
                        user_name = user.full_name
                        user_username = f"@{user.username}" if user.username else f"(ID: <code>{user.id}</code>)"
                    except Exception:
                        user_name = "пользователь"
                        user_username = f"(ID: <code>{user_id}</code>)"
                    text_preview = message.text or "[медиа]"
                    await bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            f"👁 <b>{sender_name}</b> {sender_username} ответил <b>{user_name}</b> {user_username}:\n\n"
                            f"{escape(text_preview)}"
                        )
                    )
                    
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
                for support_id in SUPPORT_IDS:
                    try:
                        sent_msg = await bot.send_message(chat_id=support_id, text=content)
                        support_reply_map[(sent_msg.chat.id, sent_msg.message_id)] = uid
                        save_support_map()
                    except Exception as e:
                        logging.warning(f"[BOT] Не удалось отправить сообщение в поддержку ({support_id}): {e}")
            await message.answer("Сообщение отправлено в поддержку.")
        except Exception as e:
            logging.warning(f"[BOT] Ошибка при пересылке в поддержку: {e}")
            await message.answer("Произошла ошибка при отправке сообщения в поддержку.")
        return

    # Если бот отключён в группе
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if cid in disabled_chats:
            return
        # Новое условие: бот отвечает в группах только при упоминании его имени или при reply на его сообщение
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
        user_input = voice_regex.sub("", user_input)
    
    lower_input = user_input.lower()

    logging.info(f"[DEBUG] cid={cid}, text='{user_input}'")

    # Новый блок для запроса курса валют, использующий универсальное регулярное выражение
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

    # Исправленная обработка запроса погоды с использованием WeatherAPI
    weather_pattern = r"погода(?:\s+в)?\s+([a-zа-яё\-\s]+?)(?:\s+(?:на\s+(\d+)\s+дн(?:я|ей)|на\s+(неделю)|завтра|послезавтра))?$"
    weather_match = re.search(weather_pattern, lower_input, re.IGNORECASE)
    if weather_match:
        city_raw = weather_match.group(1).strip()
        days_part = weather_match.group(2)
        week_flag = weather_match.group(3)
        mode_flag = re.search(r"(завтра|послезавтра)", lower_input)  # отдельным поиском
        
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

    # Проверка на вопрос по файлу (исправленная позиция, после return)
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

    # Все остальные запросы идут сюда:
    gemini_text = await handle_msg(message, user_input, voice_response_requested)
    if not gemini_text:
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

async def generate_short_caption(rus_word: str) -> str:
    short_prompt = (
        "ИНСТРУКЦИЯ: Ты — творческий помощник, который умеет писать очень короткие, дружелюбные подписи "
        "на русском языке. Не упоминай, что ты ИИ или Google. Старайся не превышать 15 слов.\n\n"
        f"ЗАДАЧА: Придумай одну короткую, дружелюбную подпись для картинки с «{rus_word}». "
        "Можно с лёгкой эмоцией или юмором, не более 15 слов."
    )
    try:
        response = await model.generate_content_async([
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
                            await bot.send_message(chat_id=cid, text=c, **thread(message))
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
        gemini_text = await generate_short_caption(rus_word)
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
            gemini_text = f"⚠️ Запрос отклонён. Возможная причина: <b>{reason}</b>.\nПопробуйте переформулировать запрос."
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

# ---------------------- Новый блок для обработки запроса курса валют ---------------------- #
# Используем универсальное регулярное выражение, чтобы поддержать варианты типа "1 доллар сум" или "1 долар в сум".
EXCHANGE_PATTERN = re.compile(
    r"(?i)(\d+(?:[.,]\d+)?)[ \t]+([a-zа-яё$€₽¥]+)(?:\s+(?:в|to))?\s+([a-zа-яё$€₽¥]+)"
)

async def reminder_loop():
    global reminders
    import pytz
    from datetime import datetime
    while True:
        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
        to_send = []
        to_remove = []

        for i, (user_id, remind_dt_utc, note_text) in enumerate(reminders):
            if remind_dt_utc <= now_utc:
                to_send.append((user_id, note_text))
                to_remove.append(i)

        for i in reversed(to_remove):
            reminders.pop(i)
        if to_remove:
            save_reminders()

        for user_id, text in to_send:
            try:
                if "войс" in text.lower() or "голосом" in text.lower():
                    await send_voice_message(user_id, f"🔔 Напоминание!\n{text}")
                else:
                    await bot.send_message(user_id, f"🔔 Напоминание!\n{text}")
            except Exception as e:
                logging.warning(f"[REMINDER] Не удалось отправить напоминание: {e}")

        
        await asyncio.sleep(30)  # каждые 30 секунд проверяем

# ---------------------- Запуск бота ---------------------- #
async def main():
    asyncio.create_task(reminder_loop())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
