# ---------------------- Импорты ---------------------- #
import logging
import os
import re, textwrap
from html import unescape, escape
import random
import aiohttp
import pytz
import html as _html
from pix2text import Pix2Text
from PIL import Image
from datetime import datetime
from google.cloud import texttospeech
from io import BytesIO
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode, ChatType
from aiogram.types import (
    FSInputFile, Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, BufferedInputFile, ReplyKeyboardRemove,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
from pathlib import Path

# Централизованный путь к директории данных
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

REMINDERS_FILE = DATA_DIR / "reminders.json"
STATS_FILE = DATA_DIR / "stats.json"
NOTES_FILE = DATA_DIR / "notes.json"
SUPPORT_MAP_FILE = DATA_DIR / "support_map.json"
TIMEZONES_FILE = DATA_DIR / "timezones.json"
PROGRESS_FILE = DATA_DIR / "progress.json"
VOCAB_FILE = DATA_DIR / "vocab.json"
WORD_OF_DAY_HISTORY_FILE = DATA_DIR / "word_of_day_per_user.json"
REVIEW_STATS_FILE = DATA_DIR / "review_stats.json"
ACHIEVEMENTS_FILE = DATA_DIR / "achievements.json"
VOCAB_REMINDERS_FILE = DATA_DIR / "vocab_reminders.json"
DISABLED_CHATS_FILE = DATA_DIR / "disabled_chats.json"
UNIQUE_USERS_FILE = DATA_DIR / "unique_users.json"
UNIQUE_GROUPS_FILE = DATA_DIR / "unique_groups.json"

import asyncio
import google.generativeai as genai
import tempfile
import requests
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
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
dialogue_stats = defaultdict(int)
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
class VocabAdd(StatesGroup):
    waiting_for_word = State()
class VocabEdit(StatesGroup):
    waiting_for_field = State()
    waiting_for_new_value = State()
class GrammarExercise(StatesGroup):
    waiting_for_answer = State()
class VocabReview(StatesGroup):
    reviewing = State()
class QuizStates(StatesGroup):
    quiz = State()
    score = State()
    current_question = State()
    questions = State()
    level = State()

BOT_ID: int = None
VOICE_MAP = {
    "en": {"lang": "en-US", "name": "en-US-Wavenet-D"},
    "ru": {"lang": "ru-RU", "name": "ru-RU-Wavenet-C"},
}

async def safe_send(chat_id: int, text: str, *, reply_to: int | None = None):
    """
    Пытается отправить text c parse_mode=HTML.
    Если Telegram ругается — пробуем более «безопасные» варианты.
    """
    try:
        # 1‑я попытка — как есть (красивый HTML)
        await bot.send_message(chat_id,
                               text=text,
                               parse_mode="HTML",
                               reply_to_message_id=reply_to)
    except TelegramBadRequest:
        # 2‑я попытка — вырезаем все теги, кроме <code>/<pre>
        no_tags = re.sub(r'</?(?!code|pre)[a-zA-Z][^>]*>', '', text)
        try:
            await bot.send_message(chat_id,
                                   text=no_tags,
                                   parse_mode="HTML",
                                   reply_to_message_id=reply_to)
        except TelegramBadRequest:
            # 3‑я попытка — полностью экранируем, отключаем parse_mode
            await bot.send_message(chat_id,
                                   text=_html.escape(text),
                                   parse_mode=None,
                                   reply_to_message_id=reply_to)

def web_search(query: str, num_results: int = 5) -> str:
    """
    Делает запрос в Google Custom Search JSON API и возвращает
    конкатенированные сниппеты результатов.
    """
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_SEARCH_API_KEY,
        "cx": GOOGLE_CX,
        "q": query,
        "num": num_results,
    }
    resp = requests.get(url, params=params)
    data = resp.json()
    snippets = []
    for item in data.get("items", []):
        snippets.append(f"- {item['snippet']}")
    return "\n".join(snippets)

def detect_lang(text: str) -> str:
    return "ru" if re.search(r"[а-яА-Я]", text) else "en"

def detect_dominant_lang(text: str) -> str:
    """
    Определяет язык по количеству букв. Если в строке больше 30% латиницы и не менее 5 символов — английский.
    Если больше 30% кириллицы и не менее 5 символов — русский.
    Иначе — по умолчанию 'ru'.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return "ru"

    en_count = sum(1 for ch in letters if 'a' <= ch.lower() <= 'z')
    ru_count = sum(1 for ch in letters if 'а' <= ch.lower() <= 'я')
    total = en_count + ru_count

    if total == 0:
        return "ru"

    en_ratio = en_count / total
    ru_ratio = ru_count / total

    if en_ratio >= 0.3 and en_count >= 5:
        return "en"
    elif ru_ratio >= 0.3 and ru_count >= 5:
        return "ru"
    return "ru"

def strip_html(text: str) -> str:
    text = re.sub(r"</?[^>]+>", "", text)
    text = text.replace("•", "").strip()
    return text

def _clean_explain(text: str) -> str:
    """
    Превращает LaTeX‑формулы в «читаемый» русский текст.
    Избавляемся от того, что Telegram не умеет рендерить.
    """
    # убираем заголовок‑заглушку Gemini
    text = text.replace("Подробное пояснение:", "").strip()

    # выкидываем всё, что в $$ … $$
    text = re.sub(r"\$\$.*?\$\$", "", text, flags=re.S)

    # \frac{a}{b} → «дробь a / b»
    text = re.sub(r"\\frac\{([^}]+)\}\{([^}]+)\}", r"дробь \1 / \2", text)

    # \sqrt{x} или \sqrt{a^2+b^2} → «корень из ( … )»
    text = re.sub(r"\\sqrt\{([^}]+)\}", r"корень из (\1)", text)

    # x^{2} → «x в степени 2»
    text = re.sub(r"([a-zA-Z\d]+)\^\{([^}]+)\}", r"\1 в степени \2", text)

    # x_1 → «x внизу 1»  (можно подобрать другое слово, если хочется)
    text = re.sub(r"([a-zA-Z\d]+)_\{([^}]+)\}", r"\1 внизу \2", text)

    # убираем остальные управляющие команды (\sin, \alpha …),
    # фигурные скобки и лишние пробелы
    text = re.sub(r"\\[a-zA-Z]+", "", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()

def clean_for_tts(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)  # удаление HTML
    text = unescape(text)
    text = re.sub(r"[«»„“”\"']", '', text)  # удалить все типы кавычек
    text = text.replace("—", "-")
    
    # (ИЗМЕНЕНО) убираем markdown
    text = re.sub(r"[*_`]+", "", text)
    return text.strip()

def load_dialogues():
    with open("learning/dialogues.json", "r", encoding="utf-8") as f:
        raw = json.load(f)
    # Если это dict, то делаем нормализацию ключей
    if isinstance(raw, dict):
        return {k.lower(): v for k, v in raw.items()}
    else:
        # если список словарей с полем "topic"
        return raw

dialogues = load_dialogues()

# ---------------------- Загрузка переменных окружения ---------------------- #
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/root/vandili/key2.json"
credentials = service_account.Credentials.from_service_account_file("/root/vandili/key.json")
translate_client = translate.TranslationServiceClient(credentials=credentials)

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME")
GOOGLE_SEARCH_API_KEY = os.getenv("GOOGLE_SEARCH_API_KEY")
GOOGLE_CX = os.getenv("GOOGLE_CX")
# Приводим к строке для гарантии, что тип правильный (если вдруг значение None)
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY") or ""

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
# Клавиатура с основными действиями
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="📝 Мои заметки"),
            KeyboardButton(text="⏰ Мои напоминания")
        ],
        [
            KeyboardButton(text="🇬🇧 Изучение английского"),
            KeyboardButton(text="🆘 Помощь")
        ]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)
# ★ Инициализируем диспетчер с MemoryStorage для FSM
dp = Dispatcher(storage=MemoryStorage())
morph = MorphAnalyzer()

genai.configure(api_key=GEMINI_API_KEY)
# Изменение модели на Gemini 2.5 Pro Experimental
model = genai.GenerativeModel(model_name="models/gemini-2.5-pro-preview-03-25")


if os.path.exists(ACHIEVEMENTS_FILE):
    with open(ACHIEVEMENTS_FILE, "r", encoding="utf-8") as f:
        user_achievements = json.load(f)
else:
    user_achievements = {}

if os.path.exists(REVIEW_STATS_FILE):
    with open(REVIEW_STATS_FILE, "r", encoding="utf-8") as f:
        review_stats = json.load(f)
else:
    review_stats = {}

def load_timezones() -> dict:
    if not os.path.exists(TIMEZONES_FILE):
        return {}
    try:
        with open(TIMEZONES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.exception(f"Не удалось загрузить timezones.json: {e}")
        return {}

def save_timezones(timezones: dict):
    try:
        with open(TIMEZONES_FILE, "w", encoding="utf-8") as f:
            json.dump(timezones, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.exception(f"Не удалось сохранить timezones.json: {e}")

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
        logging.exception(f"[BOT] Не удалось сохранить reminders: {e}")

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
        logging.exception(f"[BOT] Не удалось сохранить заметки: {e}")

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
        logging.exception(f"Ошибка при сохранении support_map: {e}")

def load_stats() -> dict:
    """
    Загружает основные метрики (messages_total, files_received, commands_used, unique_users) из stats.json.
    """
    if not os.path.exists(STATS_FILE):
        return {
            "messages_total": 0,
            "files_received": 0,
            "commands_used": {},
            "unique_users": []
        }
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("messages_total", 0)
            data.setdefault("files_received", 0)
            data.setdefault("commands_used", {})
            data.setdefault("unique_users", [])  
            return data
    except Exception as e:
        logging.exception(f"Не удалось загрузить stats.json: {e}")
        return {
            "messages_total": 0,
            "files_received": 0,
            "commands_used": {},
            "unique_users": []  
        }

def save_stats():
    """
    Сохраняет текущие метрики (messages_total, files_received, commands_used) в stats.json.
    """
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.exception(f"Не удалось сохранить stats.json: {e}")

def load_progress() -> dict:
    if not os.path.exists(PROGRESS_FILE):
        return {}
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.exception(f"[BOT] Не удалось загрузить progress.json: {e}")
        return {}

def save_progress(progress: dict):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.exception(f"[BOT] Не удалось сохранить progress.json: {e}")

def save_achievements():
    with open(ACHIEVEMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(user_achievements, f, ensure_ascii=False, indent=2)

async def check_achievements(user_id: int, message_target):
    uid = str(user_id)
    achieved = user_achievements.get(uid, [])
    new_achievements = []

    total_words = len(user_vocab.get(user_id, []))
    reviewed_5 = sum(1 for e in user_vocab.get(user_id, []) if e.get("review_level", 0) >= 5)
    correct = review_stats.get(uid, {}).get("correct", 0)
    dialog_words = dialogue_stats.get(uid, 0)

    if total_words >= 10 and "📘 10 слов добавлено" not in achieved:
        new_achievements.append("📘 10 слов добавлено")
    if reviewed_5 >= 3 and "🎓 3 слова выучено" not in achieved:
        new_achievements.append("🎓 3 слова выучено")
    if correct >= 10 and "🧠 10 правильных ответов" not in achieved:
        new_achievements.append("🧠 10 правильных ответов")
    if dialog_words >= 5 and "🗣 5 слов из диалогов" not in achieved:
        new_achievements.append("🗣 5 слов из диалогов")

    if new_achievements:
        achieved.extend(new_achievements)
        user_achievements[uid] = achieved
        save_achievements()
        await message_target.answer(
            f"🏆 Новое достижение:\n" + "\n".join(f"• {a}" for a in new_achievements),
            show_alert=True
        )

def load_vocab() -> dict[int, list[dict]]:
    if not os.path.exists(VOCAB_FILE):
        return {}
    try:
        with open(VOCAB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {int(k): v for k, v in data.items()}
    except Exception as e:
        logging.exception(f"[BOT] Не удалось загрузить vocab: {e}")
        return {}

def save_vocab(vocab: dict[int, list[dict]]):
    try:
        with open(VOCAB_FILE, "w", encoding="utf-8") as f:
            json.dump(vocab, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.exception(f"[BOT] Не удалось сохранить vocab: {e}")

def save_review_stats():
    with open(REVIEW_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(review_stats, f, ensure_ascii=False, indent=2)

def load_word_of_day_history() -> dict[int, list[str]]:
    if not os.path.exists(WORD_OF_DAY_HISTORY_FILE):
        return {}
    try:
        with open(WORD_OF_DAY_HISTORY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return {int(k): v for k, v in raw.items()}
    except Exception as e:
        logging.exception(f"[BOT] Не удалось загрузить историю слов дня: {e}")
        return {}

def save_word_of_day_history(history: dict[int, list[str]]):
    try:
        with open(WORD_OF_DAY_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.exception(f"[BOT] Не удалось сохранить историю слов дня: {e}")

def normalize_text(text: str) -> str:
    return re.sub(r"[^\w]", "", text.strip().lower())

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

    ax.set_title("Топ-5 команд")
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
# Включено ли автонапоминание по повторению слов для каждого пользователя
vocab_reminders_enabled = {}
VOCAB_REMINDERS_FILE = VOCAB_REMINDERS_FILE

def load_vocab_reminder_settings():
    if not os.path.exists(VOCAB_REMINDERS_FILE):
        return {}
    try:
        with open(VOCAB_REMINDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.exception(f"[BOT] Не удалось загрузить {VOCAB_REMINDERS_FILE}: {e}")
        return {}

def save_vocab_reminder_settings():
    try:
        with open(VOCAB_REMINDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(vocab_reminders_enabled, f)
    except Exception as e:
        logging.exception(f"[BOT] Не удалось сохранить {VOCAB_REMINDERS_FILE}: {e}")

vocab_reminders_enabled = load_vocab_reminder_settings()
stats = load_stats()  # подгружаем основные метрики
pending_note_or_reminder = {}
support_mode_users = set()
support_reply_map = load_support_map()
chat_history = {}
user_documents = {}
user_notes = load_notes()
reminders = []  # Список кортежей: (user_id, event_utc: datetime, text)
reminders = load_reminders()
quiz_storage = {}
user_progress = load_progress()
reminder_status = {}
user_vocab: dict[int, list[dict]] = load_vocab()
user_word_of_day_history = load_word_of_day_history()
user_images_text = {}
_p2t = Pix2Text(use_fast=True)

async def recognize_formula(image_bytes: bytes) -> str | None:
    """
    Извлекает LaTeX из картинки с формулой.
    Работает как с новыми (Page), так и со старыми (list[dict]) ответами pix2text.
    """
    img = Image.open(BytesIO(image_bytes)).convert("RGB")

    # ⚠️   В новых версиях лучше пользоваться готовой обёрткой:
    try:
        latex = _p2t.recognize_formula(img)          # >=1.1 возвращает str
        return latex.strip() if latex else None
    except AttributeError:
        # fallback на старое API
        pass

    #  ---- старый формат (<1.1)  -----------------
    preds = _p2t(img, return_text=False)             # отдаёт list
    if not preds:
        return None

    block = preds[0]

    # Page/Block  (>=1.1)  -------------------------
    if hasattr(block, "formula"):
        return getattr(block, "formula", None)

    # dict (<1.1) ---------------------------------
    if isinstance(block, dict):
        return block.get("formula") or block.get("text")

    return None

# --- Рендер LaTeX в PNG (для превью) ---
import matplotlib
matplotlib.use("Agg")          # отключаем GUI‑бэкэнд
import matplotlib.pyplot as plt
import tempfile, os

def latex_to_png(latex: str) -> str:
    """
    Рисует формулу и возвращает путь к временному .png
    """
    fig = plt.figure()
    fig.text(0.1, 0.5, f"${latex}$", fontsize=24)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    fig.savefig(tmp.name, bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    return tmp.name

# --- Заменяем все $$...$$ на PNG и возвращаем текст + список картинок ---
def replace_latex_with_png(text: str) -> tuple[str, list[str]]:
    """
    Находит фрагменты $$ ... $$, рендерит их в PNG через latex_to_png()
    и возвращает:
      • text  – строку без LaTeX (на месте каждой формулы – пометка [см. картинку N])
      • images – список путей к временным PNG‑файлам
    """
    import re
    images: list[str] = []

    def _repl(match):
        latex = match.group(1).strip()
        path  = latex_to_png(latex)
        images.append(path)
        idx = len(images)       # 1‑based нумерация
        return f"[см. картинку {idx}]"

    new_text = re.sub(r"\$\$(.+?)\$\$", _repl, text, flags=re.S)
    return new_text, images

# --- «чинить» LaTeX, который не понимает matplotlib.mathtext ----------
def _sanitize_for_png(lx: str) -> str:
    """
    Заменяем команды, которых нет в mathtext.
    Добавляй сюда по мере необходимости.
    """
    replacements = {
        r"\implies": r"\Rightarrow",
        r"\iff": r"\Leftrightarrow",
        r"\Longrightarrow": r"\Rightarrow",
        r"\longrightarrow": r"\rightarrow",
        r"\longleftarrow": r"\leftarrow",
    }
    for bad, good in replacements.items():
        lx = lx.replace(bad, good)
    return lx

STEP_RE = re.compile(
    r"(?:Шаг|Step)\s*[\.\:№]?\s*(\d+)[\.\:]*\s*.*?"  # «Шаг 1», «Step 1:»
    r"\$\$(.+?)\$\$"                                # сам LaTeX
    r"(.*?)(?=(?:Шаг|Step)\s*\d+|$)",               # пояснение до следующего шага
    flags=re.S | re.I
)

def split_steps(raw: str) -> list[tuple[str, str, str]]:
    """
    Разбивает ответ Gemini на шаги.
    Возвращает [(latex, header, explain), …],
    где header — строка «Шаг N: …», explain — полное пояснение без урезания.
    """
    out = []
    for m in STEP_RE.finditer(raw):
        header  = m.group(1).strip()     # «Шаг 3: …»
        latex   = m.group(2).strip()
        explain = m.group(3).strip()
        out.append((latex, header, explain))
    return out
    
# ---------------------- Работа с отключёнными чатами ---------------------- #
DISABLED_CHATS_FILE = DISABLED_CHATS_FILE

def load_disabled_chats() -> set:
    if not os.path.exists(DISABLED_CHATS_FILE):
        return set()
    try:
        with open(DISABLED_CHATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        logging.exception(f"[BOT] Не удалось загрузить disabled_chats: {e}")
        return set()

def save_disabled_chats(chats: set):
    try:
        with open(DISABLED_CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(chats), f)
    except Exception as e:
        logging.exception(f"[BOT] Не удалось сохранить disabled_chats: {e}")

disabled_chats = load_disabled_chats()

# ---------------------- Persistent Unique Users и Groups ---------------------- #
UNIQUE_USERS_FILE = UNIQUE_USERS_FILE
UNIQUE_GROUPS_FILE = UNIQUE_GROUPS_FILE

def load_unique_users() -> set:
    if not os.path.exists(UNIQUE_USERS_FILE):
        return set()
    try:
        with open(UNIQUE_USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        logging.exception(f"Не удалось загрузить уникальных пользователей: {e}")
        return set()

def save_unique_users(users: set):
    try:
        with open(UNIQUE_USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(users), f)
    except Exception as e:
        logging.exception(f"Не удалось сохранить уникальных пользователей: {e}")

def load_unique_groups() -> set:
    if not os.path.exists(UNIQUE_GROUPS_FILE):
        return set()
    try:
        with open(UNIQUE_GROUPS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        logging.exception(f"Не удалось загрузить уникальные группы: {e}")
        return set()

def save_unique_groups(groups: set):
    try:
        with open(UNIQUE_GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(groups), f)
    except Exception as e:
        logging.exception(f"Не удалось сохранить уникальные группы: {e}")

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
            stats["unique_users"] = list(unique_users)
            save_stats()
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

EXCHANGE_PATTERN = re.compile(
    r"(?i)(\d+(?:[.,]\d+)?)[ \t]+([a-zа-яё$€₽¥]+)(?:\s+(?:в|to))?\s+([a-zа-яё$€₽¥]+)"
)

@dp.message(F.text.regexp(EXCHANGE_PATTERN))
async def handle_exchange_request(message: Message):
    match = EXCHANGE_PATTERN.search(message.text)
    if not match:
        await message.answer("Не удалось распознать запрос на обмен валют 😔")
        return

    amount, from_curr, to_curr = match.groups()

    # Исправляем запятую на точку для правильного преобразования в float
    amount = float(amount.replace(",", "."))

    result_text = await get_exchange_rate(amount, from_curr, to_curr)
    await message.answer(result_text)

async def get_floatrates_rate(from_curr: str, to_curr: str) -> float:
    from_curr = from_curr.lower()
    to_curr = to_curr.lower()

    # Сначала заменяем на стандартные коды валют
    from_curr = CURRENCY_SYNONYMS.get(from_curr, from_curr).lower()
    to_curr = CURRENCY_SYNONYMS.get(to_curr, to_curr).lower()

    url = f"https://www.floatrates.com/daily/{from_curr}.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logging.error(f"Floatrates вернул статус {resp.status} для {url}")
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
    # Пробуем получить курс
    rate = await get_floatrates_rate(from_curr, to_curr)
    if rate is None:
        # Возвращаем в случае ошибки более красивое сообщение
        return "Извините, не смог найти курс валют для такого запроса 😔"

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
                    logging.exception(f"Ошибка геокодинга для {name}: статус {resp.status}")
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
        logging.exception(f"[BOT] Ошибка перевода города '{city_name}': {e}")

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
                    logging.exception(f"Ошибка получения погоды: статус {resp.status}")
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

def split_text_for_tts(text: str, max_len: int = 4500) -> list[str]:
    import re
    paragraphs = re.split(r'(?<=[.!?])\s+', text)
    
    chunks = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 1 < max_len:
            current += para + " "
        else:
            chunks.append(current.strip())
            current = para + " "

    if current:
        chunks.append(current.strip())

    return chunks

# ---------------------- Функция для отправки голосового ответа ---------------------- #
async def send_voice_message(chat_id: int, text: str, lang: str = "en-US"):
    client = texttospeech.TextToSpeechClient()
    clean_text = clean_for_tts(text)

    chunks = split_text_for_tts(clean_text, max_len=4500)

    for i, chunk in enumerate(chunks):
        synthesis_input = texttospeech.SynthesisInput(text=chunk)

        if lang == "en-US":
            voice_name = "en-US-Wavenet-F"
        elif lang == "ru-RU":
            voice_name = "ru-RU-Wavenet-B"  
        else:
            voice_name = lang

        voice = texttospeech.VoiceSelectionParams(
            language_code=lang,
            name=voice_name

        )

        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.OGG_OPUS
        )

        try:
            response = client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )
        except Exception as e:
            logging.exception("[TTS] Ошибка при синтезе речи:")
            await bot.send_message(chat_id, "❌ Ошибка при озвучке части текста.")
            return

        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as out:
            out.write(response.audio_content)
            out_path = out.name

        await bot.send_voice(chat_id=chat_id, voice=FSInputFile(out_path, filename=f"voice_part_{i+1}.ogg"))
        await asyncio.sleep(1.2)  # немного подождём между отправками
        os.remove(out_path)

async def generate_voice_snippet(text: str, lang_code: str) -> str:
    client = texttospeech.TextToSpeechClient()

    if lang_code == "ru-RU":
        voice_name = "ru-RU-Wavenet-D"
    elif lang_code == "en-US":
        voice_name = "en-US-Wavenet-F"
    else:
        voice_name = lang_code  # fallback

    voice = texttospeech.VoiceSelectionParams(
        language_code=lang_code,
        name=voice_name
    )

    synthesis_input = texttospeech.SynthesisInput(text=text)
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.OGG_OPUS
    )
    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as out_file:
        out_file.write(response.audio_content)
        return out_file.name
        
async def send_bilingual_voice(chat_id: int, dialogue_text: str):
    audio_segments = []
    lines = [l.strip() for l in dialogue_text.strip().splitlines() if l.strip()]
    total = len(lines)

    progress_msg = await bot.send_message(chat_id, f"🔊 Озвучка [░░░░░░░░░░░░░░░░░░░░] 0/{total}")

    def progress_bar(current: int, total: int, size: int = 20) -> str:
        filled = int(size * current / total)
        return "█" * filled + "░" * (size - filled)

    for i, line in enumerate(lines, start=1):
        raw_line = strip_html(line)
        if not raw_line or re.match(r"^[#\-\*]+$", raw_line.strip()):
            continue

        cleaned = clean_for_tts(raw_line)
        # если запрос на русском — используем ru-RU
        if detect_lang(cleaned) == "ru":
            lang_code = "ru-RU"
        else:
            lang_code = "en-US"

        try:
            ogg_path = await generate_voice_snippet(cleaned, lang_code)
            segment = AudioSegment.from_file(ogg_path, format="ogg")
            audio_segments.append(segment)
            os.remove(ogg_path)
        except Exception as e:
            logging.exception(f"[voice] Ошибка при озвучке строки: {cleaned}\n{e}")
            continue

        # 📊 Обновляем прогресс
        bar = progress_bar(i, total)
        try:
            await progress_msg.edit_text(f"🎙️ Озвучка [{bar}] {i}/{total}")
        except Exception as e:
            logging.warning(f"⚠️ Не удалось обновить сообщение прогресса: {e}")

    if not audio_segments:
        await progress_msg.edit_text("❌ Ничего не удалось озвучить.")
        return

    final_audio = sum(audio_segments[1:], audio_segments[0])
    final_path = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg").name
    final_audio.export(final_path, format="ogg")

    await bot.send_voice(chat_id=chat_id, voice=FSInputFile(final_path, filename="dialogue.ogg"))
    os.remove(final_path)

    await progress_msg.edit_text("✅ Озвучка завершена!")

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

    greet = """Привет! Я <b>VAI</b> — твой заботливый помощник и верный компаньон 🤖💬

🎤 Хочешь — поговорим голосом или просто отвечу на любые вопросы 
📄 Присылай документы или код — всё прочту  
🧠 Помогу с программированием
📐 Больше не ломай голову над бесконечными интегралами, формулами и реакциями — просто пришли изображение, и я всё распознаю и объясню!
🖼 Мечтаешь о чём-то? Покажу картинку  
☀️ Спроси про погоду — подскажу, брать ли зонт  
💱 Подскажу курс валют — будь в курсе  
📝 Запомню важное в заметки  
⏰ Напомню о самом главном  
🇬🇧 Учим английский вместе — легко и интересно!

Я рядом, просто напиши 😊"""

    # 🌐 Если сообщение из группы
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if message.chat.id in disabled_chats:
            disabled_chats.remove(message.chat.id)
            save_disabled_chats(disabled_chats)
            logging.info(f"[BOT] Бот снова включён в группе {message.chat.id}")
        await message.answer("Бот включён ✅")
        await message.answer(greet, reply_markup=main_menu_keyboard)
        return

    # 📩 Если в ЛС
    await message.answer(greet, reply_markup=main_menu_keyboard)

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
            logging.exception(f"[BROADCAST] Ошибка при отправке в чат {recipient}: {e}")
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

@dp.message(Command("learn_en"))
async def cmd_learn_en(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Курс", callback_data="learn_course")],
        [InlineKeyboardButton(text="📙 Грамматика", callback_data="learn_grammar")],
        [InlineKeyboardButton(text="🎯 Квиз", callback_data="learn_quiz")],
        [InlineKeyboardButton(text="💬 Диалоги", callback_data="learn_dialogues")],
        [InlineKeyboardButton(text="🧠 Слово дня", callback_data="learn_word")],
        [InlineKeyboardButton(text="📓 Мой словарь", callback_data="learn_vocab")],
        [InlineKeyboardButton(text="➕ Добавить слово", callback_data="learn_add_word")],
        [InlineKeyboardButton(text="🔁 Повторить слова", callback_data="review_menu")],
        [InlineKeyboardButton(text="📈 Прогресс", callback_data="learn_progress")],
        [InlineKeyboardButton(text="🏆 Достижения", callback_data="learn_achievements")],
        [InlineKeyboardButton(text="🔔 Напоминания", callback_data="learn_reminders")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="learn_close")]
    ])
    await message.answer("🇬🇧 <b>Изучение английского</b>\nВыбери раздел:", reply_markup=keyboard)

@dp.callback_query(F.data == "learn_back")
async def handle_learn_back(callback: CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await cmd_learn_en(callback.message)

@dp.callback_query(F.data == "learn_toggle_reminders")
async def handle_toggle_reminders(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    current = vocab_reminders_enabled.get(str(uid), True)
    vocab_reminders_enabled[str(uid)] = not current
    save_vocab_reminder_settings()
    status = "включены ✅" if not current else "отключены ❌"
    await callback.answer(f"Напоминания теперь {status}", show_alert=True)
    await callback.message.delete()
    await cmd_learn_en(callback.message)

@dp.callback_query(F.data == "learn_reminders")
async def handle_learn_reminders(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    enabled = vocab_reminders_enabled.get(str(uid), True)

    text = (
        "🔔 <b>Напоминания о повторении слов</b>\n\n"
        "Бот может автоматически напоминать тебе повторить слова из словаря.\n"
        f"Сейчас: <b>{'включены ✅' if enabled else 'отключены ❌'}</b>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🚫 Выключить" if enabled else "✅ Включить",
                callback_data="learn_toggle_reminders"
            )
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(F.data == "learn_close")
async def handle_learn_close(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

@dp.callback_query(F.data == "learn_dialogues")
async def handle_learn_dialogues(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👋 Small Talk", callback_data="dialogue_topic:Small Talk")],
        [InlineKeyboardButton(text="🛫 Аэропорт", callback_data="dialogue_topic:Airport")],
        [InlineKeyboardButton(text="☕ Кафе", callback_data="dialogue_topic:Cafe")],
        [InlineKeyboardButton(text="🏨 Отель", callback_data="dialogue_topic:Hotel")],
        [InlineKeyboardButton(text="🧑‍⚕️ У врача", callback_data="dialogue_topic:Doctor")],
        [InlineKeyboardButton(text="🛍️ Покупки", callback_data="dialogue_topic:Shopping")],
        [InlineKeyboardButton(text="🚕 Такси", callback_data="dialogue_topic:Taxi")],
        [InlineKeyboardButton(text="📞 Телефонный звонок", callback_data="dialogue_topic:Phone Call")],
        [InlineKeyboardButton(text="👨‍🏫 На уроке", callback_data="dialogue_topic:In Class")],
        [InlineKeyboardButton(text="📅 Назначение встречи", callback_data="dialogue_topic:Making an Appointment")],
        [InlineKeyboardButton(text="🧭 Спросить дорогу", callback_data="dialogue_topic:Asking for Directions")],
        [InlineKeyboardButton(text="💼 Интервью", callback_data="dialogue_topic:Job Interview")],
        [InlineKeyboardButton(text="🏦 В банке", callback_data="dialogue_topic:Bank")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")]
    ])
    await callback.message.edit_text("Выбери тему диалога:", reply_markup=keyboard)
# ─── Обработчик выбора конкретной темы диалога ───#
@dp.callback_query(F.data.startswith("dialogue_topic:"))
async def handle_dialogue_topic(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    topic_raw = callback.data.split(":", 1)[1]
    topic_title = topic_raw.replace("_", " ").title()

    await callback.message.edit_text(
        f"📖 Генерирую 3–5 примеров диалогов на тему «{topic_title}»…",
        parse_mode="HTML"
    )

    # Новый промпт с более удобной для разбора структурой
    prompt = (
        f"Ты — опытный преподаватель английского. Составь 3–5 коротких диалогов на тему «{topic_title}».\n"
        "Ответь строго чистым JSON (без ```), в формате:\n"
        "[\n"
        "  {\n"
        "    \"title\": \"Ordering Coffee\",\n"
        "    \"dialogue\": [\n"
        "      {\"speaker\": \"You\", \"en\": \"Hi, can I get a cappuccino to go, please?\", \"ru\": \"Здравствуйте, можно мне капучино с собой, пожалуйста?\"},\n"
        "      {\"speaker\": \"VAI\", \"en\": \"Sure. What size would you like?\", \"ru\": \"Конечно. Какой размер вы бы хотели?\"}\n"
        "    ]\n"
        "  },\n"
        "  …\n"
        "]\n"
        "Никаких Markdown‑ограждений, только чистый JSON-массив."
    )

    resp = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
    raw = resp.text.strip()

    # Убираем возможные ```json … ```  
    raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()

    try:
        dialogs = json.loads(raw)
    except json.JSONDecodeError:
        await callback.message.edit_text(
            f"<b>💬 Тема: {topic_title}</b>\n\n"
            f"<code>Не удалось распознать JSON, вот что вернуло Gemini:</code>\n"
            f"{escape(raw)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")]
            ])
        )
        return

    # Очищаем любые звёздочки внутри строк
    for dlg in dialogs:
        for turn in dlg.get("dialogue", []):
            turn["en"] = re.sub(r"\*+", "", turn["en"]).strip()
            turn["ru"] = re.sub(r"\*+", "", turn["ru"]).strip()

    # Собираем HTML
    lines = [f"<b>💬 Тема: {topic_title}</b>\n"]
    for idx, block in enumerate(dialogs, 1):
        title = block.get("title", f"Диалог {idx}")
        lines.append(f"<u>{title}</u>")
        for turn in block["dialogue"]:
            sp = turn["speaker"]
            lines.append(f"• <b>{sp}:</b> {turn['en']}")
            lines.append(f"  <i>«{turn['ru']}»</i>")
        lines.append("")  # пустая строка между диалогами

    full_text = "\n".join(lines)

    await state.update_data(
        last_dialogue_json=dialogs,
        last_dialogue=full_text
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔊 Озвучить диалог", callback_data="dialogue_voice")],
        [InlineKeyboardButton(text="📘 Ключевые слова", callback_data="dialogue_add_words")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")],
    ])

    await callback.message.edit_text(
        full_text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "review_menu")
async def show_review_mode(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Только 'созревшие'", callback_data="learn_review")],
        [InlineKeyboardButton(text="📚 Повторить весь словарь", callback_data="vocab_repeat_all")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_vocab")]
    ])
    await callback.answer()
    await callback.message.edit_text("🧠 Выбери режим повторения:", reply_markup=keyboard)


@dp.callback_query(F.data == "learn_course")
async def handle_learn_course(callback: CallbackQuery):
    await callback.answer()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📘 A1", callback_data="learn_level:A1")],
        [InlineKeyboardButton(text="📗 A2", callback_data="learn_level:A2")],
        [InlineKeyboardButton(text="📙 B1", callback_data="learn_level:B1")],
        [InlineKeyboardButton(text="📕 B2", callback_data="learn_level:B2")],
        [InlineKeyboardButton(text="📓 C1", callback_data="learn_level:C1")],
        [InlineKeyboardButton(text="📔 C2", callback_data="learn_level:C2")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")]
    ])
    await callback.message.edit_text("Выбери уровень английского для изучения:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("learn_level:"))
async def handle_learn_level(callback: CallbackQuery, state: FSMContext):
    level = callback.data.split(":")[1]
    await callback.answer()
    await callback.message.edit_text(f"📚 Генерирую материалы для уровня {level}, подожди немного...")

    prompt = (
        f"Ты — профессиональный преподаватель английского языка. "
        f"Составь краткий учебный план для уровня {level}.\n"
        "Перечисли 3–5 тем. Для каждой:\n"
        "- Название (на русском и в скобках на английском)\n"
        "- Краткое описание\n"
        "- Задание\n\n"
        "Не используй HTML, верни обычный текст. Пример:\n\n"
        "Уровень: A2 (Pre-Intermediate)\n\n"
        "Тема 1: Название (Title)\n"
        "Описание: ...\n"
        "Задание: ...\n\n"
        "Тема 2: ..."
    )

    try:
        response = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
        raw_text = response.text.strip()
        text = format_gemini_response(raw_text)

        # 💡 Добавим HTML-разметку вручную
        formatted = f"<b>📘 Уровень {level}</b>\n\n"
        formatted += text.replace("Тема ", "<b>• Тема ").replace("\nОписание:", "</b>\n  <b>Описание:</b>").replace("\nЗадание:", "\n  <b>Задание:</b>")

        # Сохраняем для озвучки и квиза
        await state.update_data(last_course=formatted)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Ещё темы", callback_data=f"learn_more:{level}")],
            [
                InlineKeyboardButton(text="🧪 Тест по теме", callback_data=f"learn_quiz:{level}"),
                InlineKeyboardButton(text="🔊 Озвучить темы", callback_data=f"learn_voice:{level}")
            ],
            [InlineKeyboardButton(text="🔙 Назад к уровням", callback_data="learn_course")]
        ])

        await callback.message.edit_text(formatted, reply_markup=keyboard, parse_mode="HTML")

    except Exception as e:
        await callback.message.edit_text("❌ Ошибка при генерации курса.")
        logging.exception(f"[learn_level:{level}] Ошибка Gemini: {e}")

@dp.callback_query(F.data.startswith("learn_voice:"))
async def handle_learn_voice(callback: CallbackQuery, state: FSMContext):
    await callback.answer("🎧 Озвучиваю темы...")

    data = await state.get_data()
    course_text = data.get("last_course")

    if not course_text:
        await callback.message.answer("❌ Нет данных для озвучки.")
        return

    try:
        # ✨ Используем билингвальную озвучку (строка за строкой, auto-detect языка)
        await send_bilingual_voice(callback.message.chat.id, course_text)
    except Exception as e:
        logging.exception(f"[learn_voice] Ошибка при озвучке: {e}")
        await callback.message.answer("❌ Не удалось озвучить темы.")

@dp.callback_query(F.data.startswith("learn_quiz:"))
async def handle_learn_quiz(callback: CallbackQuery):
    level = callback.data.split(":")[1]
    user_id = callback.from_user.id
    await callback.answer(f"🧪 Генерирую тест для уровня {level}...")

    prompt = (
        f"Составь тест из 3 вопросов по английскому языку для уровня {level}.\n"
        "Каждый вопрос должен иметь 4 варианта ответа (A, B, C, D), с указанием правильного.\n"
        "Ответ строго в формате JSON-массива:\n\n"
        '[\n'
        '  {\n'
        '    "question": "What is the capital of France?",\n'
        '    "options": {\n'
        '      "A": "London",\n'
        '      "B": "Paris",\n'
        '      "C": "Berlin",\n'
        '      "D": "Madrid"\n'
        '    },\n'
        '    "answer": "B"\n'
        '  }\n'
        ']'
    )

    try:
        response = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
        raw_text = response.text.strip()

        if not raw_text:
            raise ValueError("Пустой ответ от Gemini")

        # 🔧 Удаляем Markdown-обёртку типа ```json ... ```
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]  # удаляем ```json\n
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]  # удаляем ```

        # 🧪 Пробуем распарсить JSON
        try:
            questions = json.loads(raw_text)
        except json.JSONDecodeError:
            logging.error(f"[learn_quiz:{level}] Невозможно распарсить JSON:\n{raw_text}")
            await callback.message.answer("❌ Ошибка разбора ответа. Gemini вернул некорректный формат.")
            return

        # Сохраняем правильные ответы
        quiz_storage[user_id] = {}
        for i, q in enumerate(questions):
            quiz_storage[user_id][i + 1] = q["answer"]

            buttons = [
                [InlineKeyboardButton(text=f"{k}) {v}", callback_data=f"quiz_answer:{level}:{i+1}:{k}")]
                for k, v in q["options"].items()
            ]

            await callback.message.answer(
                f"<b>Вопрос {i+1}:</b> {q['question']}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="HTML"
            )

    except Exception as e:
        logging.exception(f"[learn_quiz:{level}] Ошибка Gemini: {e}")
        await callback.message.answer("❌ Не удалось сгенерировать тест.")

async def send_quiz_question(message: Message, state: FSMContext):
    data = await state.get_data()
    questions = data["questions"]
    index = data["current_question"]

    if index >= len(questions):
        score = data["score"]
        level = data["level"]
        await message.answer(
            f"🏁 Тест завершён!\nТы ответил правильно на {score} из {len(questions)}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад к темам", callback_data=f"learn_level:{level}")]
            ])
        )
        await state.clear()
        return

    question = questions[index]
    text = f"<b>Вопрос {index + 1}:</b> {question['question']}"
    buttons = [
        [InlineKeyboardButton(text=f"{key}) {val}", callback_data=f"quiz_answer:{key}")]
        for key, val in question["options"].items()
    ]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@dp.callback_query(F.data == "dialogue_voice")
async def handle_dialogue_voice(callback: CallbackQuery, state: FSMContext):
    await callback.answer("🎧 Озвучиваю диалог...")

    data = await state.get_data()
    dialogue = data.get("last_dialogue")

    if not dialogue:
        await callback.message.answer("❌ Нет доступного диалога для озвучки.")
        return

    await send_bilingual_voice(callback.message.chat.id, dialogue)

@dp.callback_query(F.data == "learn_achievements")
async def show_achievements(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    await callback.answer()

    achievements = user_achievements.get(uid, [])

    if achievements:
        text = "<b>🏆 Твои достижения:</b>\n\n" + "\n".join(f"• {a}" for a in achievements)
    else:
        text = "😶 Пока нет достижений. Всё впереди!"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(F.data == "dialogue_add_words")
async def handle_dialogue_add_words(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    dialogue = data.get("last_dialogue")

    if not dialogue:
        await callback.message.answer("❌ Диалог не найден.")
        return

    prompt = (
        "Вот английский диалог. Найди 3–5 полезных или сложных слов, "
        "и для каждого дай перевод и короткий пример. Формат:\n\n"
        "Слово: ...\nЗначение: ...\nПример: ...\n---\n\n"
        + dialogue
    )

    await callback.message.edit_text("🔍 Анализирую диалог...")

    try:
        response = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
        raw = response.text.strip()
        # быстро конвертим **жирный** и *курсив* в HTML‑теги
        html = format_gemini_response(raw)
        await state.update_data(dialogue_words=html)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Добавить в словарь", callback_data="dialogue_add_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="dialogue_add_cancel")]
        ])
        await callback.message.answer(f"<b>📘 Найденные слова:</b>\n\n{html}",
                                      reply_markup=keyboard,
                                      parse_mode="HTML")
    except Exception as e:
        logging.exception(f"[dialogue_add_words] Ошибка: {e}")
        await callback.message.answer("❌ Не удалось обработать диалог.")

@dp.callback_query(F.data == "dialogue_add_confirm")
async def handle_dialogue_add_confirm(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    data = await state.get_data()
    text = data.get("dialogue_words", "")

    count = 0
    for block in text.split("---"):
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        word = lines[0].replace("Слово:", "").strip()
        meaning = lines[1].replace("Значение:", "").strip()
        example = lines[2].replace("Пример:", "").strip()
        entry = {
            "word": word,
            "meaning": meaning,
            "example": example,
            "last_reviewed": datetime.utcnow().isoformat(),
            "review_level": 0
        }
        user_vocab.setdefault(uid, []).append(entry)
        count += 1

    save_vocab(user_vocab)
    await check_achievements(uid, callback)
    await callback.message.edit_text(f"✅ Добавлено слов: <b>{count}</b>", parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data == "dialogue_add_cancel")
async def handle_dialogue_add_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer("❌ Отменено.")
    await callback.message.delete()
    await state.clear()


@dp.callback_query(F.data.startswith("learn_more:"))
async def handle_learn_more(callback: CallbackQuery):
    level = callback.data.split(":")[1]
    await callback.answer("Генерирую дополнительные темы...")

    prompt = (
        f"Сгенерируй ещё 3–5 новых учебных тем для уровня {level} по английскому языку.\n"
        "Формат:\n\n"
        "• Тема: Название\n"
        "Описание: ...\n"
        "Задание: ..."
    )

    try:
        response = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
        text = format_gemini_response(response.text.strip())
        chat_history[callback.from_user.id] = text

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Ещё темы", callback_data=f"learn_more:{level}"),
                InlineKeyboardButton(text="🔊 Озвучить", callback_data=f"voice_material:{level}")
            ],
            [
                InlineKeyboardButton(text="🧪 Тест по теме", callback_data=f"quiz_for:{level}")
            ],
            [InlineKeyboardButton(text="🔙 Назад к уровням", callback_data="learn_course")]
        ])

        await callback.message.answer(f"<b>📘 Дополнительные темы для уровня {level}</b>\n\n{text}", reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        await callback.message.answer("❌ Не удалось сгенерировать дополнительные темы.")
        logging.exception(f"[learn_more:{level}] Ошибка Gemini: {e}")

@dp.callback_query(F.data.startswith("voice_material:"))
async def handle_voice_material(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()

    text = chat_history.get(uid)
    if not text:
        await callback.message.answer("❌ Не удалось найти последний текст.")
        return

    await send_voice_message(callback.message.chat.id, clean_for_tts(text))

@dp.callback_query(F.data.startswith("quiz_for:"))
async def handle_quiz_for_topic(callback: CallbackQuery):
    level = callback.data.split(":")[1]
    await callback.answer("Генерирую тест по теме...")

    prompt = (
        f"Составь мини-квиз по английскому уровню {level}. "
        "Сделай 3 коротких вопроса с 4 вариантами ответов (A–D), "
        "указывая правильный вариант. Формат:\n\n"
        "1. Вопрос\nA) ...\nB) ...\nC) ...\nD) ...\nПравильный ответ: X\n\n"
        "2. ... и т.д."
    )

    try:
        response = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
        text = format_gemini_response(response.text.strip())

        # Парсим текст в структуру вопросов
        questions = parse_quiz_questions(text)

        if not questions:
            await callback.message.answer("❌ Не удалось распознать тест.")
            return

        # Сохраняем правильные ответы для этого пользователя
        quiz_storage[callback.from_user.id] = {i + 1: q["answer"] for i, q in enumerate(questions)}

        for idx, q in enumerate(questions, start=1):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="A", callback_data=f"quiz_answer:{level}:{idx}:A"),
                    InlineKeyboardButton(text="B", callback_data=f"quiz_answer:{level}:{idx}:B"),
                    InlineKeyboardButton(text="C", callback_data=f"quiz_answer:{level}:{idx}:C"),
                    InlineKeyboardButton(text="D", callback_data=f"quiz_answer:{level}:{idx}:D"),
                ]
            ])
            await callback.message.answer(f"<b>Вопрос {idx}:</b>\n{q['question']}", reply_markup=keyboard)

        await callback.message.answer("Выбирай ответы 👇 я скажу, правильно или нет.")

    except Exception as e:
        await callback.message.answer("❌ Ошибка при генерации теста.")
        logging.exception(f"[quiz_for:{level}] Ошибка Gemini: {e}")

@dp.callback_query(F.data == "learn_quiz")
async def handle_quiz_menu(callback: CallbackQuery):
    await callback.answer()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟦 A1", callback_data="quiz_level:A1")],
        [InlineKeyboardButton(text="🟩 A2", callback_data="quiz_level:A2")],
        [InlineKeyboardButton(text="🟨 B1", callback_data="quiz_level:B1")],
        [InlineKeyboardButton(text="🟥 B2", callback_data="quiz_level:B2")],
        [InlineKeyboardButton(text="⬛ C1", callback_data="quiz_level:C1")],
        [InlineKeyboardButton(text="🟫 C2", callback_data="quiz_level:C2")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")]
    ])
    await callback.message.edit_text("🎯 <b>Выбери уровень квиза по английскому:</b>", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("quiz_level:"))
async def handle_quiz_level(callback: CallbackQuery):
    level = callback.data.split(":")[1]
    await callback.answer()
    await callback.message.edit_text(f"📚 Генерирую квиз для уровня <b>{level}</b>...")

    prompt = (
        f"Составь квиз по английскому языку для уровня {level}. "
        "Сделай 5 коротких вопросов с 4 вариантами ответов (A–D), "
        "указывая правильный вариант. Используй формат:\n\n"
        "1. Вопрос\nA) ...\nB) ...\nC) ...\nD) ...\nПравильный ответ: X\n\n"
        "2. ... и т.д."
    )

    try:
        response = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
        text = format_gemini_response(response.text.strip())

        questions = parse_quiz_questions(text)

        if not questions:
            await callback.message.edit_text("❌ Не удалось сгенерировать квиз.")
            return

        for idx, q in enumerate(questions, start=1):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="A", callback_data=f"quiz_answer:{level}:{idx}:A"),
                    InlineKeyboardButton(text="B", callback_data=f"quiz_answer:{level}:{idx}:B"),
                    InlineKeyboardButton(text="C", callback_data=f"quiz_answer:{level}:{idx}:C"),
                    InlineKeyboardButton(text="D", callback_data=f"quiz_answer:{level}:{idx}:D")
                ]
            ])
            await callback.message.answer(f"<b>Вопрос {idx}:</b>\n{q['question']}", reply_markup=keyboard)

        next_button = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Новый квиз", callback_data=f"quiz_level:{level}")],
            [InlineKeyboardButton(text="📈 Прогресс", callback_data="learn_progress")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_quiz")]
        ])
        await callback.message.answer("✅ Выбирай варианты ответа, и я скажу правильно или нет 😉", reply_markup=next_button)

        # Сохраняем правильные ответы во временное хранилище
        quiz_storage[callback.from_user.id] = {i + 1: q["answer"] for i, q in enumerate(questions)}

    except Exception as e:
        await callback.message.edit_text("❌ Ошибка при генерации квиза.")
        logging.exception(f"[quiz_level:{level}] Ошибка Gemini: {e}")

@dp.callback_query(F.data.startswith("quiz_answer:"))
async def handle_quiz_answer(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Неверный формат ответа.")
        return

    _, level, q_number_str, user_choice = parts
    user_id = callback.from_user.id

    try:
        q_number = int(q_number_str)
    except:
        await callback.answer("Ошибка обработки вопроса.")
        return

    correct_answer = quiz_storage.get(user_id, {}).get(q_number)
    if not correct_answer:
        await callback.answer("Вопрос не найден или устарел.")
        return

    if user_choice == correct_answer:
        progress = user_progress.setdefault(callback.from_user.id, {})
        progress[level] = progress.get(level, 0) + 1
        save_progress(user_progress)
        correct = user_progress[callback.from_user.id][level]
        msg = f"📈 Прогресс: <b>{correct}</b> правильных ответов по уровню {level}"
        await callback.message.answer(msg)
        await callback.answer("✅ Верно!", show_alert=False)
        await callback.message.edit_text(callback.message.text + f"\n\n✅ Ответ: <b>{user_choice}</b>")
    else:
        await callback.answer("❌ Неверно!", show_alert=False)
        await callback.message.edit_text(callback.message.text + f"\n\n❌ Твой ответ: <b>{user_choice}</b>\n✔️ Правильный ответ: <b>{correct_answer}</b>")


@dp.callback_query(F.data == "learn_progress")
async def handle_learn_progress(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    progress_data = user_progress.get(uid, {})

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")]
    ])

    if not progress_data:
        await callback.message.edit_text(
            "📊 У тебя пока нет прогресса. Пройди пару квизов и возвращайся!",
            reply_markup=keyboard
        )
        return

    # Если прогресс есть — показать его + кнопку сброса
    text = "<b>📈 Твой прогресс по уровням:</b>\n"
    for level, correct_count in progress_data.items():
        text += f"• {level}: <b>{correct_count}</b> правильных ответов\n"

    keyboard.inline_keyboard.append([
        InlineKeyboardButton(text="🔄 Сбросить прогресс", callback_data="progress_reset")
    ])

    await callback.message.edit_text(text.strip(), reply_markup=keyboard)


@dp.callback_query(F.data == "learn_add_word")
async def handle_add_word_click(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("✍️ Введи слово на английском, которое хочешь добавить:")
    await state.set_state(VocabAdd.waiting_for_word)

@dp.message(VocabAdd.waiting_for_word)
async def handle_add_word_input(message: Message, state: FSMContext):
    uid = message.from_user.id
    word_raw = message.text.strip()

    prompt = (
        f"Дай краткое определение и пример для английского слова '{word_raw}'. "
        "Ответ в формате:\n"
        "Значение: ...\nПример: ..."
    )

    await message.answer("🔄 Генерирую перевод и пример...")
    try:
        response = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
        raw = response.text.strip().split("\n")
        meaning = raw[0].replace("Значение:", "").strip()
        example = raw[1].replace("Пример:", "").strip()

        await state.update_data(word=word_raw, meaning=meaning, example=example)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Добавить", callback_data="confirm_add_word"),
                InlineKeyboardButton(text="✏️ Редактировать", callback_data="edit_add_word"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_word")
            ]
        ])
        await message.answer(
            f"<b>Слово:</b> {word_raw}\n<b>Значение:</b> {meaning}\n<b>Пример:</b> {example}",
            reply_markup=keyboard
        )
    except Exception as e:
        logging.exception(f"[VOCAB_ADD_INTERFACE] Ошибка: {e}")
        await message.answer("❌ Не удалось добавить слово.")
        await state.clear()

@dp.callback_query(F.data == "progress_reset")
async def handle_progress_reset(callback: CallbackQuery):
    uid = callback.from_user.id
    if uid in user_progress:
        user_progress.pop(uid)
        save_progress(user_progress)
        await callback.answer("Прогресс сброшен ❌", show_alert=True)
    else:
        await callback.answer("У тебя и так нет прогресса 😄", show_alert=True)

@dp.callback_query(F.data == "learn_word")
async def handle_word_of_the_day(callback: CallbackQuery):
    await callback.answer("Генерирую слово дня...")

    uid = callback.from_user.id
    user_history = user_word_of_day_history.get(uid, [])

    attempts = 0
    max_attempts = 5
    word = ""
    meaning = ""
    example = ""

    while attempts < max_attempts:
        attempts += 1

        prompt = (
            "Придумай одно уникальное английское слово, желательно нужное и важное. "
            "Не повторяй слова, которые выдавал раньше, и не используй generic вроде 'hello'.\n\n"
            "Ответ строго в формате:\n"
            "Слово: cat\n"
            "Значение: Кошка\n"
            "Пример: I have a cat.\n\n"
            "Пожалуйста, не добавляй ничего лишнего, кроме этих трёх строк. "
            "На русском языке укажи «Слово:», «Значение:», «Пример:»."
        )

        try:
            response = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
            raw = response.text.strip().split("\n")

            for line in raw:
                if line.lower().startswith("слово:"):
                    word = line.split(":", 1)[1].strip()
                elif line.lower().startswith("значение:"):
                    meaning = line.split(":", 1)[1].strip()
                elif line.lower().startswith("пример:"):
                    example = line.split(":", 1)[1].strip()

            if word and word.lower() not in [w.lower() for w in user_history]:
                user_history.append(word)
                user_word_of_day_history[uid] = user_history[-100:]  # ограничим историю 100 словами
                save_word_of_day_history(user_word_of_day_history)
                break
        except Exception as e:
            logging.exception(f"[WORD_OF_DAY_ATTEMPT] Ошибка: {e}")
            continue

    if not word:
        await callback.message.edit_text("❌ Не удалось получить уникальное слово.")
        return

    text = (
        f"<b>📘 Слово дня:</b> <i>{word}</i>\n\n"
        f"<b>Значение:</b> {escape(meaning)}\n"
        f"<b>Пример:</b> {escape(example)}"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Новое слово", callback_data="learn_word")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(F.data == "learn_vocab")
async def handle_vocab(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()

    vocab = user_vocab.get(uid, [])
    if not vocab:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить слово", callback_data="vocab_add")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")]
        ])
        await callback.message.edit_text("📓 В твоём словаре пока нет слов.", reply_markup=keyboard)
        return

    for i, entry in enumerate(vocab):
        word = entry['word']
        meaning = entry['meaning']
        example = entry['example']
        last_reviewed = entry.get('last_reviewed', 'неизвестно')
        date_str = ""
        try:
            dt = datetime.fromisoformat(last_reviewed)
            date_str = dt.strftime('%d.%m.%Y')
        except:
            date_str = "неизвестно"

        review_level = entry.get("review_level", 0)
        progress = "🔹" * review_level + "⚪" * (5 - review_level)
        progress_percent = int((review_level / 5) * 100)
        text = (
            f"<b>{i+1}. {word}</b> — {meaning}\n"
            f"<i>{example}</i>\n"
            f"📅 Последнее повторение: <code>{date_str}</code>\n"
            f"📊 Уровень: {progress_percent}% {progress}"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"vocab_delete:{i}"),
                InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"vocab_edit:{i}")
            ]
        ])
        await callback.message.answer(text, reply_markup=keyboard)

    bottom = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить слово", callback_data="vocab_add")],
        [InlineKeyboardButton(text="📈 Статистика", callback_data="learn_vocab_stats")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back"),
         InlineKeyboardButton(text="❌ Закрыть", callback_data="vocab_close")]
    ])
    await callback.message.answer("📚 Вот все твои слова:", reply_markup=bottom)

@dp.callback_query(F.data == "vocab_add")
async def ask_add_vocab(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.message.delete()
    pending_note_or_reminder[uid] = {"type": "add_vocab"}
    await bot.send_message(uid, "✍️ Введи английское слово, которое хочешь добавить в словарь.")

@dp.callback_query(F.data == "learn_review")
async def handle_vocab_review(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id

    if not vocab_reminders_enabled.get(str(uid), True):
        await callback.message.edit_text("🔕 У тебя отключены напоминания для слов. Включи их, чтобы повторять слова.")
        return
    
    await callback.answer()
    await callback.message.delete()

    vocab = user_vocab.get(uid, [])
    if not vocab:
        await callback.message.edit_text("📓 В твоём словаре пока нет слов для повторения.")
        return

    now = datetime.utcnow()
    due_words = []
    for i, entry in enumerate(vocab):
        last = datetime.fromisoformat(entry.get("last_reviewed", now.isoformat()))
        level = entry.get("review_level", 0)
        interval_days = [0, 1, 2, 4, 7, 14, 30]
        interval = interval_days[min(level, len(interval_days) - 1)]
        if (now - last).days >= interval:
            due_words.append((i, entry))

    if not due_words:
        await callback.message.edit_text("✅ У тебя нет слов, которые нужно повторить прямо сейчас.")
        return

    await state.update_data(queue=due_words, index=0)
    await state.set_state(VocabReview.reviewing)
    await send_next_review_word(callback.message.chat.id, state)

async def send_next_review_word(uid: int, state: FSMContext):
    data = await state.get_data()
    queue = data.get("queue", [])
    index = data.get("index", 0)

    if index >= len(queue):
        await bot.send_message(uid, "✅ Все слова повторены!")
        await state.clear()
        return

    i, entry = queue[index]

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="✅ Помню", callback_data=f"review_remember:{i}"),
        InlineKeyboardButton(text="❌ Не помню", callback_data=f"review_forget:{i}")
    ],
    [
        InlineKeyboardButton(text="⏭ Пропустить", callback_data="review_skip"),
        InlineKeyboardButton(text="⏹ Стоп", callback_data="review_stop")
    ]
])

    await bot.send_message(
        uid,
        f"<b>{entry['word']}</b> — {entry['meaning']}\n\n<i>{entry['example']}</i>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("review_remember:"))
async def review_remember(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    i = int(callback.data.split(":")[1])
    user_vocab[uid][i]["review_level"] = min(user_vocab[uid][i].get("review_level", 0) + 1, 5)
    user_vocab[uid][i]["last_reviewed"] = datetime.utcnow().isoformat()
    save_vocab(user_vocab)

    uid_str = str(uid)
    user_stats = review_stats.get(uid_str, {"correct": 0, "wrong": 0})
    user_stats["correct"] += 1
    review_stats[uid_str] = user_stats
    save_review_stats()
    await check_achievements(uid, callback)


    data = await state.get_data()
    data["index"] += 1
    await state.update_data(data)
    await callback.answer("✅ Отлично!")
    await callback.message.delete()
    await send_next_review_word(uid, state)

@dp.callback_query(F.data.startswith("review_forget:"))
async def review_forget(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    i = int(callback.data.split(":")[1])
    user_vocab[uid][i]["review_level"] = max(user_vocab[uid][i].get("review_level", 0) - 1, 0)
    user_vocab[uid][i]["last_reviewed"] = datetime.utcnow().isoformat()
    save_vocab(user_vocab)

    uid_str = str(uid)
    user_stats = review_stats.get(uid_str, {"correct": 0, "wrong": 0})
    user_stats["wrong"] += 1
    review_stats[uid_str] = user_stats
    save_review_stats()

    data = await state.get_data()
    data["index"] += 1
    await state.update_data(data)
    await callback.answer("🔁 Запомнишь в следующий раз!")
    await callback.message.delete()
    await send_next_review_word(uid, state)

@dp.callback_query(F.data == "review_skip")
async def review_skip(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    data["index"] += 1
    await state.update_data(data)
    await callback.answer("⏭ Пропущено")
    await send_next_review_word(callback.from_user.id, state)

@dp.callback_query(F.data == "review_stop")
async def review_stop(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("⏹ Повторение остановлено")
    await callback.message.edit_text("🔕 Повторение остановлено.")

@dp.callback_query(F.data == "learn_vocab_stats")
async def handle_vocab_stats(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()

    vocab = user_vocab.get(uid, [])
    if not vocab:
        await callback.message.edit_text("📓 В твоём словаре пока нет слов.")
        return

    total = len(vocab)
    levels = {}
    now = datetime.utcnow()

    next_reviews = []

    for entry in vocab:
        level = entry.get("level", 1)
        levels[level] = levels.get(level, 0) + 1

        due = entry.get("next_review")
        if due:
            try:
                due_dt = datetime.fromisoformat(due)
                if due_dt > now:
                    next_reviews.append(due_dt)
            except:
                continue

    stats_text = f"<b>📊 Статистика словаря</b>\n\nВсего слов: <b>{total}</b>\n"

    for lvl in sorted(levels):
        stats_text += f"• Уровень {lvl}: <b>{levels[lvl]}</b>\n"

    if next_reviews:
        nearest = min(next_reviews)
        in_minutes = int((nearest - now).total_seconds() // 60)
        stats_text += f"\n⏰ Следующее повторение через <b>{in_minutes} мин</b>"
    else:
        stats_text += "\n✅ Все слова готовы к повторению!"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")]
    ])
    await callback.message.edit_text(stats_text.strip(), reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(F.data == "confirm_add_word")
async def confirm_add_word(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    data = await state.get_data()
    entry = {
        "word": data["word"],
        "meaning": data["meaning"],
        "example": data["example"],
        "last_reviewed": datetime.utcnow().isoformat(),
        "review_level": 0
    }
    user_vocab.setdefault(uid, []).append(entry)
    save_vocab(user_vocab)
    await callback.message.edit_text(f"✅ Слово <b>{data['word']}</b> добавлено в твой словарь.")
    await state.clear()

@dp.callback_query(F.data == "edit_add_word")
async def edit_add_word(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✍️ Введи новое слово на английском:")
    await state.set_state(VocabAdd.waiting_for_word)

@dp.callback_query(F.data == "cancel_add_word")
async def cancel_add_word(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🚫 Добавление слова отменено.")
    await state.clear()

 
@dp.callback_query(F.data == "vocab_close")
async def close_vocab(callback: CallbackQuery):
    await callback.message.delete()

@dp.callback_query(F.data.startswith("vocab_delete:"))
async def handle_vocab_delete(callback: CallbackQuery):
    uid = callback.from_user.id
    index = int(callback.data.split(":")[1])
    vocab = user_vocab.get(uid, [])

    if 0 <= index < len(vocab):
        deleted_word = vocab.pop(index)
        save_vocab(user_vocab)
        await callback.answer(f"Удалено: {deleted_word['word']}", show_alert=True)
    else:
        await callback.answer("❌ Не удалось найти слово для удаления.")

    # Возвращаемся к обновлённому списку
    await handle_vocab(callback)

@dp.callback_query(F.data.startswith("vocab_edit:"))
async def handle_vocab_edit(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    index = int(callback.data.split(":")[1])
    vocab = user_vocab.get(uid, [])

    if 0 <= index < len(vocab):
        await state.update_data(edit_index=index)
        await state.set_state(VocabEdit.waiting_for_field)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Слово", callback_data="edit_field:word")],
            [InlineKeyboardButton(text="✏️ Перевод", callback_data="edit_field:meaning")],
            [InlineKeyboardButton(text="✏️ Пример", callback_data="edit_field:example")]
        ])
        await callback.message.answer("Что ты хочешь изменить?", reply_markup=keyboard)
    else:
        await callback.message.answer("❌ Слово не найдено.")

@dp.callback_query(F.data.startswith("edit_field:"))
async def ask_new_value(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(":")[1]
    await state.update_data(field_to_edit=field)
    await state.set_state(VocabEdit.waiting_for_new_value)
    await callback.message.answer(f"✍️ Введи новое значение для <b>{field}</b>:")

@dp.message(VocabEdit.waiting_for_new_value)
async def save_new_value(message: Message, state: FSMContext):
    uid = message.from_user.id
    new_value = message.text.strip()
    data = await state.get_data()

    index = data["edit_index"]
    field = data["field_to_edit"]
    vocab = user_vocab.get(uid, [])

    if 0 <= index < len(vocab) and field in ["word", "meaning", "example"]:
        vocab[index][field] = new_value
        save_vocab(user_vocab)
        await message.answer(f"✅ Обновлено: <b>{field}</b> → {new_value}")
    else:
        await message.answer("❌ Ошибка при обновлении.")

    await state.clear()
    # Показываем обновлённый словарь
    fake_callback = type("Fake", (), {"from_user": message.from_user, "message": message})
    await handle_vocab(fake_callback)


@dp.callback_query(F.data.in_({"review_remember", "review_forget"}))
async def handle_review_response(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()

    vocab = user_vocab.get(uid, [])
    if not vocab:
        await callback.message.edit_text("Нет слов для повторения.")
        return

    current = vocab[0]  # первое слово — текущее
    if callback.data == "review_remember":
        current["review_level"] = current.get("review_level", 0) + 1
    else:
        current["review_level"] = 0  # сбрасываем

    current["last_reviewed"] = datetime.utcnow().isoformat()
    save_vocab(user_vocab)

    await handle_vocab_review(callback)  # повторяем следующий

@dp.callback_query(F.data == "learn_grammar")
async def handle_grammar(callback: CallbackQuery):
    await callback.answer()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📘 A1", callback_data="grammar_level:A1")],
        [InlineKeyboardButton(text="📗 A2", callback_data="grammar_level:A2")],
        [InlineKeyboardButton(text="📙 B1", callback_data="grammar_level:B1")],
        [InlineKeyboardButton(text="📕 B2", callback_data="grammar_level:B2")],
        [InlineKeyboardButton(text="📒 C1", callback_data="grammar_level:C1")],
        [InlineKeyboardButton(text="📓 C2", callback_data="grammar_level:C2")],
        [InlineKeyboardButton(text="❌ Назад", callback_data="learn_back")],
    ])
    await callback.message.edit_text("Выбери уровень грамматики:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("grammar_level:"))
async def handle_grammar_level(callback: CallbackQuery, state: FSMContext):
    level = callback.data.split(":", 1)[1]
    await callback.answer(f"Генерирую упражнение для уровня {level}…")

    prompt = (
        f"Составь одно небольшое грамматическое упражнение уровня {level}. "
        "Нужно ровно два поля (без лишнего текста):\n"
        "1) Вопрос (на английском): предложение с одним пропуском ____ и глагол в скобках.\n"
        "2) Ответ: только нужная форма глагола.\n\n"
        "Пример:\n"
        "Вопрос: By the time we arrived, the train ____ (leave).\n"
        "Ответ: had left"
    )
    resp = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
    raw = resp.text.strip()

    # ищем блок "Ответ"
    m = re.search(r"Ответ\s*[:\-]\s*(.+)", raw, flags=re.IGNORECASE)
    if not m:
        logging.error(f"[GRAMMAR:{level}] Bad response:\n{raw}")
        kb_retry = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Ещё раз", callback_data=f"grammar_level:{level}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")],
        ])
        await callback.message.edit_text("❌ Не удалось распознать ответ. Попробуй ещё раз.", reply_markup=kb_retry)
        return

    # отделяем вопрос до "Ответ"
    raw_q = raw.split("Ответ", 1)[0].strip()
    # убираем любые '*' и метку "Вопрос:"
    question = re.sub(r"\*+", "", raw_q)
    question = re.sub(r"(?i)^Вопрос[:\-\s]*", "", question).strip()

    correct = m.group(1).strip()

    await state.set_state(GrammarExercise.waiting_for_answer)
    await state.update_data(correct_answer=correct)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Новое", callback_data=f"grammar_level:{level}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")],
    ])

    await callback.message.edit_text(
        (
            f"<b>📘 Упражнение ({level})</b>\n\n"
            f"Вопрос: {escape(question)}\n\n"
            "✍️ Введите пропущенную форму глагола на английском "
            "(например: had left), без кавычек и лишних слов."
        ),
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )


@dp.message(GrammarExercise.waiting_for_answer)
async def check_grammar_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    correct = normalize_text(data["correct_answer"])
    user_input = normalize_text(message.text or "")

    if user_input == correct:
        await message.answer(
            "✅ Верно! Хотите ещё одно упражнение?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📘 Новое", callback_data="learn_grammar")]
                ]
            )
        )
    else:
        await message.answer(
            (
                f"❌ Неверно.\n"
                f"Правильный ответ: <b>{data['correct_answer']}</b>\n\n"
                "Пожалуйста, введите именно эту форму, без дополнительных слов."
            ),
            parse_mode="HTML"
        )

    await state.clear()


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


#  обработчик приходящей КАРТИНКИ с формулой
# ------------------------------------------------------------------
@dp.message(F.photo | F.document.mime_type.in_({"image/png", "image/jpeg"}))
async def handle_formula_image(message: Message):
    """
    1. скачиваем файл
    2. распознаём LaTeX
    3. кладём формулу в кэш + показываем превью
    (ответ от Gemini НЕ генерируем – ждём вопрос пользователя)
    """
    # 0️⃣ Сообщаем пользователю, что начали обработку
    notify_msg = await message.answer("🔄 Обрабатываю изображение, пожалуйста, подождите…")
    # 1️⃣  — получаем байты картинки
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    tg_file = await bot.get_file(file_id)
    url     = f"https://api.telegram.org/file/bot{TOKEN}/{tg_file.file_path}"

    async with aiohttp.ClientSession() as sess:
        async with sess.get(url) as r:
            img_bytes = await r.read()

    # 2️⃣  — распознаём формулу
    latex = await recognize_formula(img_bytes)
    if not latex:
        # 1️⃣ Обновляем статус: обработка завершилась с ошибкой
        await notify_msg.edit_text("❌ Не удалось обработать изображение.")
        await message.answer("❌ Не смог распознать формулу.")
        return

    # 3️⃣  — кладём в кэш → в следующем сообщении пользователь сможет
    # 1️⃣ Обновляем статус: распознавание прошло успешно
    await notify_msg.edit_text("✅ Изображение обработано")
    #      спросить «реши её», «упрости» и т.д.
    user_images_text[message.from_user.id] = latex

    #     делаем маленькое превью, чтобы человек видел, что именно распознано
    png_path = latex_to_png(latex)
    try:
        await bot.send_photo(
            chat_id = message.chat.id,
            photo   = FSInputFile(png_path, "formula.png"),
            caption = (f"Я вижу это 👆\n<code>{latex}</code>\n\n"
                       "Спроси что‑нибудь об этом!"),
            parse_mode = "HTML"
        )
    finally:
        os.remove(png_path)

    # 🔚  больше ничего не делаем – ждём дальнейший вопрос пользователя
    return


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
async def confirm_delete_all_notes(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="note_cancel_delete_all"),
            InlineKeyboardButton(text="✅ Удалить всё", callback_data="note_confirm_delete_all")
        ]
    ])
    await callback.message.answer("Ты точно хочешь удалить <b>все</b> заметки?", reply_markup=keyboard)

@dp.callback_query(F.data == "note_confirm_delete_all")
async def do_delete_all_notes(callback: CallbackQuery):
    uid = callback.from_user.id
    user_notes[uid] = []
    save_notes()
    await show_notes(uid, callback=callback)

@dp.callback_query(F.data == "note_cancel_delete_all")
async def cancel_delete_all_notes(callback: CallbackQuery):
    await callback.message.delete()

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
        logging.exception(f"[REMINDER_EDIT] Ошибка: {e}")
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
    await callback.message.delete()  # удаляем сообщение с кнопками
    await callback.message.answer("❌ Удаление отменено.")

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
        logging.exception(f"[FSM] Ошибка при преобразовании даты: {e}")
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
        logging.exception(f"[DELAYED_REMINDER] Ошибка: {e}")
        await message.answer("❌ Не удалось установить напоминание.")

@dp.message(F.text.lower().startswith("добавь слово:"))
async def handle_add_vocab(message: Message):
    uid = message.from_user.id
    try:
        word_raw = message.text.split(":", 1)[1].strip()
    except:
        await message.answer("Формат: <code>Добавь слово: example</code>")
        return

    prompt = (
        f"Дай краткое определение и пример для английского слова '{word_raw}'. "
        "Ответ в формате:\n"
        "Значение: ...\nПример: ..."
    )

    try:
        response = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
        raw = response.text.strip().split("\n")
        meaning = raw[0].replace("Значение:", "").strip()
        example = raw[1].replace("Пример:", "").strip()
        from datetime import datetime
        entry = {
            "word": word_raw,
            "meaning": meaning,
            "example": example,
            "last_reviewed": datetime.utcnow().isoformat(),
            "review_level": 0
        }

        user_vocab.setdefault(uid, []).append(entry)
        save_vocab(user_vocab)
        await message.answer(f"✅ Слово <b>{word_raw}</b> добавлено в твой словарь.")
    except Exception as e:
        logging.exception(f"[VOCAB_ADD] Ошибка: {e}")
        await message.answer("❌ Не удалось добавить слово.")

@dp.message(F.text == "📝 Мои заметки")
async def handle_notes_button(message: Message):
    await show_notes(message.chat.id, message=message)

@dp.message(F.text == "⏰ Мои напоминания")
async def handle_reminders_button(message: Message):
    await show_reminders(message.chat.id)

@dp.message(F.text == "🇬🇧 Изучение английского")
async def handle_learn_button(message: Message):
    await cmd_learn_en(message)

@dp.message(F.text == "🆘 Помощь")
async def handle_help_button(message: Message):
    await cmd_help(message)

@dp.message(VocabAdd.waiting_for_word)
async def handle_vocab_word_input(message: Message, state: FSMContext):
    uid = message.from_user.id

    # Проверяем, ожидается ли добавление слова
    if pending_note_or_reminder.get(uid, {}).get("type") != "add_vocab":
        return  # если нет — передаём в основной обработчик

    pending_note_or_reminder.pop(uid)

    word_raw = message.text.strip()
    if not word_raw or len(word_raw) < 2:
        await message.answer("❌ Слово слишком короткое. Попробуй снова.")
        return

    prompt = (
        f"Дай краткое определение и пример для английского слова '{word_raw}'. "
        "Ответ в формате:\n"
        "Значение: ...\nПример: ..."
    )

    try:
        response = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
        raw = response.text.strip().split("\n")
        meaning = raw[0].replace("Значение:", "").strip()
        example = raw[1].replace("Пример:", "").strip()

        entry = {
            "word": word_raw,
            "meaning": meaning,
            "example": example,
            "last_reviewed": datetime.utcnow().isoformat(),
            "review_level": 0
        }

        user_vocab.setdefault(uid, []).append(entry)
        save_vocab(user_vocab)

        await message.answer(
            f"✅ Слово <b>{word_raw}</b> добавлено в твой словарь.\n"
            f"<b>Значение:</b> {meaning}\n<i>{example}</i>"
        )
    except Exception as e:
        logging.exception(f"[VOCAB_ADD] Ошибка: {e}")
        await message.answer("❌ Не удалось добавить слово. Попробуй позже.")

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

async def show_dialogues(callback: CallbackQuery):
    if not dialogues:
        await callback.message.edit_text("Диалоги не найдены.")
        return

    text = "<b>💬 Пример диалога</b>\n\n"
    for exchange in dialogues[:10]:  # Показываем первые 10
        user_msg = exchange.get("user", "")
        bot_msg = exchange.get("bot", "")
        text += f"<b>Ты:</b> {user_msg}\n<b>VAI:</b> {bot_msg}\n\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="learn_back")]
    ])
    await callback.message.edit_text(text.strip(), reply_markup=keyboard)

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
                logging.exception(f"[BOT] Ошибка при отправке ответа админа пользователю: {e}")
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
                        logging.exception(f"[BOT] Не удалось отправить сообщение в поддержку ({support_id}): {e}")
            await message.answer("Сообщение отправлено в поддержку.")
        except Exception as e:
            logging.exception(f"[BOT] Ошибка при пересылке в поддержку: {e}")
            await message.answer("Произошла ошибка при отправке сообщения в поддержку.")
        return

    # Если бот отключён в группе
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if cid in disabled_chats:
            return
        # Новое условие: бот отвечает в группах только при упоминании его имени или при reply на его сообщение
        lower_text = user_input.lower()
        mentioned = any(keyword in lower_text for keyword in ["вай", "vai", "вэй"])
        reply_to_bot = (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == BOT_ID
        )
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

def parse_quiz_questions(text: str) -> list[dict]:
    """
    Парсит текст квиза в формате:
    1. Вопрос
    A) ...
    B) ...
    C) ...
    D) ...
    Правильный ответ: X

    Возвращает список словарей:
    [
        {"question": "...", "options": ["A", "B", "C", "D"], "answer": "B"},
        ...
    ]
    """
    questions = []
    blocks = re.split(r"\n\s*(?=\d+\.\s)", text)
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 6:
            continue
        question_line = lines[0].strip()
        options = []
        for i in range(1, 5):
            option_line = lines[i].strip()
            parts = option_line.split(")", 1)
            if len(parts) == 2:
                options.append(parts[1].strip())
            else:
                options.append(option_line)
        answer_line = lines[-1].strip()
        answer_match = re.search(r"правильный\s+ответ\s*[:\-]?\s*([A-D])", answer_line, re.IGNORECASE)
        if not answer_match:
            continue
        correct = answer_match.group(1).upper()
        question_text = "\n".join(lines[:5])
        questions.append({
            "question": question_text,
            "options": options,
            "answer": correct
        })
    return questions

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
                    logging.exception(f"Unsplash returned status {response.status} for prompt '{prompt}'")
                    return None
                data = await response.json()
                if "urls" not in data or "regular" not in data["urls"]:
                    logging.exception(f"No 'regular' URL in response for '{prompt}': {data}")
                    return None
                return data["urls"]["regular"]
    except Exception as e:
        logging.exception(f"Ошибка при получении изображения: {e}")
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
        logging.exception(f"Ошибка при переводе слова '{rus_word}': {e}")
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


# ──────────────────────────────────────────────────────────────────────
#  >>>  handle_msg – версия с фиксацией LaTeX и итоговой формулой  <<<
# ──────────────────────────────────────────────────────────────────────
async def handle_msg(
    message: Message,
    recognized_text: str | None = None,
    voice_response_requested: bool = False
):
    cid        = message.chat.id
    user_input = recognized_text or (message.text or "").strip()
    uid        = message.from_user.id

    # ───────────────────────────────────────────────
    # 1) В кэше есть формула → ждём вопрос
    # ───────────────────────────────────────────────
    if uid in user_images_text:
        latex = user_images_text.pop(uid)
        # уведомляем пользователя, что вопрос обрабатывается
        await message.answer("🔄 Обрабатываю ваш запрос… 😊")

        # нет вопроса → просим сформулировать
        if not user_input:
            await message.answer("✍️ Сформулируй вопрос к этой формуле, и я отвечу!")
            return

        prompt = (
            "Ты — опытный преподаватель математики. Объясняй всё максимально подробно и при этом простым, понятным языком. "
            "Избегай громоздких формулировок, разжёвывай каждый шаг и давай маленькие примеры там, где это уместно.\n\n"
            # 0) Исходный LaTeX (любой области) между $$ … $$
            "Перед тобой выражение в формате LaTeX между двойными долларами:\n"
            f"$$ {latex} $$\n\n"
            # 1) Определи область и тип задачи
            "1) Скажи, к какой области относится эта запись (Математика / Физика / Химия / Другое) "
            "и какой это тип задачи:\n"
            "   – В математике: интеграл, уравнение, производная, упрощение и т.п.\n"
            "   – В физике: формула для расчёта величины (напр. сила, скорость, энергия), выведи нужное значение или преобразуй уравнение.\n"
            "   – В химии: уравнение реакции, баланса, расчёт стехиометрии и т.п.\n\n"
            # 2) Что делать
            "2) В зависимости от типа задачи:\n"
            "   • Если это интеграл – вычисли его.\n"
            "   • Если уравнение – реши его для указанной переменной.\n"
            "   • Если производная – найди её.\n"
            "   • Если упрощение – упрости выражение.\n"
            "   • Если физика – выполни расчёт (подставь при необходимости физические константы) или преобразуй формулу как требуется.\n"
            "   • Если химия – сбалансируй реакцию или рассчитай количества веществ.\n\n"
            # 3) Пошаговый разбор
            "3) Дай пошаговое решение. Для каждого шага указывай:\n"
            "   Шаг N:\n"
            "   • Формула в LaTeX между $$…$$\n"
            "   • Пояснение на русском.\n\n"
            # 4) Итог
            "4) В конце приведи итоговый ответ (численное значение, общий вид решения или сбалансированное уравнение) тоже в формате $$…$$."
        )

        # запрашиваем модель
        try:
            resp = await model.generate_content_async([{"role": "user", "parts": [prompt]}])
            raw_answer = resp.text.strip()
        except Exception as e:
            logging.exception(f"[FORMULA‑QA] Gemini error: {e}")
            await message.answer("❌ Не смог получить ответ. Попробуй ещё раз.")
            return

        # разбиваем ответ на шаги
        steps = split_steps(raw_answer)  # [(latex, header, explain), …]

        # ───────────────────────────────────────────
        # A. формат корректный → отрисовываем шаги
        # ───────────────────────────────────────────
        if steps:
            from PIL import Image, ImageOps

            step_imgs    = []  # список PNG шагов
            voice_chunks = []  # реплики для TTS

            # ---------- отправляем каждый шаг ----------#
            for idx, (latex_step, _h, explain_raw) in enumerate(steps, 1):
                # 1) Рендерим формулу
                img_path = latex_to_png(_sanitize_for_png(latex_step))
                step_imgs.append(img_path)
                # 2) Отбрасываем строку «4) Итоговый ответ: …», если она есть
                cleaned_lines = []
                for line in explain_raw.splitlines():
                    # удаляем строки, начинающиеся с цифры+')' (возможно с обрамлением **)
                    if re.match(r'^\s*\**\s*\d+\)\s*', line):
                        continue
                    cleaned_lines.append(line)
                cleaned = "\n".join(cleaned_lines)

                # 3) Превращаем формулу в читабельный текст
                explain = _clean_explain(cleaned)

                # 4) Удаляем звёздочки и маркеры «•» по краям
                explain = re.sub(r'^[\*\s]+|[\*\s]+$', '', explain)
                explain = re.sub(r'^[\u2022]\s*', '', explain)
                # дополнительно убираем все остаточные звёздочки, которые могли остаться
                explain = explain.replace('*', '')

                # 5) Делаем «Пояснение:» жирным
                if explain.startswith('Пояснение:'):
                    explain = explain.replace('Пояснение:', '<b>Пояснение:</b>', 1)
                    explain = re.sub(r'^<b>Пояснение:</b>[\*\s]*', '<b>Пояснение:</b> ', explain)
                else:
                    explain = escape(explain)

                caption = f"<b>Шаг {idx}.</b>\n{explain}"

                # 6) Отправляем
                if len(caption) > 1024:
                    await bot.send_photo(
                        cid,
                        FSInputFile(img_path, "step.png"),
                        caption=f"<b>Шаг {idx}</b>",
                        parse_mode="HTML",
                        reply_to_message_id=message.message_id
                    )
                    await safe_send(cid, explain, reply_to=message.message_id)
                else:
                    await bot.send_photo(
                        cid,
                        FSInputFile(img_path, "step.png"),
                        caption=caption,
                        parse_mode="HTML",
                        reply_to_message_id=message.message_id
                    )


            # ---------- итоговая формула ----------
            try:
                all_latex = re.findall(r"\$\$(.+?)\$\$", raw_answer, flags=re.S)
                if all_latex:
                    final_latex = all_latex[-1].strip()
                    if final_latex not in {l for l, _, _ in steps}:
                        final_img = latex_to_png(_sanitize_for_png(final_latex))
                        await bot.send_photo(
                            cid,
                            FSInputFile(final_img, "result.png"),
                            caption="🏁 <b>Итог</b>",
                            parse_mode="HTML",
                            reply_to_message_id=message.message_id
                        )
            finally:
                if 'final_img' in locals():
                    os.remove(final_img)
            # ---------- конец итоговой формулы ----------

            # ─────────── «общая доска» ───────────
            if step_imgs:
                try:
                    imgs    = [Image.open(p) for p in step_imgs]
                    max_w   = max(im.width for im in imgs)
                    total_h = sum(im.height for im in imgs) + 20 * (len(imgs) - 1)

                    board = Image.new("RGB", (max_w, total_h), "white")
                    y = 0
                    for im in imgs:
                        board.paste(ImageOps.expand(im, border=10, fill="white"), (0, y))
                        y += im.height + 20

                    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                        board.save(tmp.name)
                        await bot.send_photo(
                            cid,
                            FSInputFile(tmp.name, "board.png"),
                            caption="🟢 Общий вид решения",
                            parse_mode="HTML"
                        )
                finally:
                    for p in step_imgs:
                        os.remove(p)
                    if 'tmp' in locals():
                        os.remove(tmp.name)

            # озвучка (если просили «голосом»)
            if voice_response_requested:
                await send_voice_message(cid, " ".join(voice_chunks))
            return  # 🎉 готово!
        
        # ───────────────────────────────────────────
        # B. формат не распознан → плоский текст
        # ───────────────────────────────────────────
        text, imgs = replace_latex_with_png(format_gemini_response(raw_answer))
        if voice_response_requested:
            await send_voice_message(cid, text)
        else:
            await safe_send(cid, text, reply_to=message.message_id)
            for p in imgs:
                try:
                    await bot.send_photo(cid, FSInputFile(p, "latex_part.png"))
                finally:
                    os.remove(p)
        return                                   # дальше не идём

    # ───────────────────────────────────────────────
    # 2) Остальная логика (имя, Unsplash, и т.д.)
    # ───────────────────────────────────────────────
    lower_inp = user_input.lower()

    # --- имя бота -------------------------------------------------------
    if any(nc in lower_inp for nc in NAME_COMMANDS):
        answer = "Меня зовут <b>VAI</b>! 🤖"
        return await (
            send_voice_message(cid, answer) if voice_response_requested
            else message.answer(answer)
        )

    # --- информация о создателе ----------------------------------------
    if any(ic in lower_inp for ic in INFO_COMMANDS):
        reply_text = random.choice(OWNER_REPLIES)
        return await (
            send_voice_message(cid, reply_text) if voice_response_requested
            else message.answer(reply_text)
        )

    # --- «покажи …» (Unsplash) -----------------------------------------
    show_image, rus_word, image_en, leftover = parse_russian_show_request(user_input)
    if show_image and rus_word:
        leftover = re.sub(r"\b(вай|vai)\b", "", leftover, flags=re.IGNORECASE).strip()

    leftover    = leftover.strip()
    full_prompt = f"{rus_word} {leftover}".strip() if rus_word else leftover

    image_url = await get_unsplash_image_url(
        image_en, UNSPLASH_ACCESS_KEY
    ) if show_image else None

    # ответ Gemini
    gemini_text = await generate_and_send_gemini_response(
        cid, full_prompt, show_image, rus_word, leftover
    )

    # --- если нужен voice‑ответ ----------------------------------------
    if voice_response_requested:
        await send_voice_message(cid, gemini_text or "Нет ответа.")
        return

    # --- отправляем результат ------------------------------------------
    if image_url:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(image_url) as r:
                if r.status == 200:
                    tmp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg").name
                    with open(tmp_path, "wb") as f:
                        f.write(await r.read())
                    try:
                        await bot.send_chat_action(cid, "upload_photo")
                        caption, rest = split_caption_and_text(gemini_text or "…")
                        await bot.send_photo(
                            cid,
                            FSInputFile(tmp_path, "image.jpg"),
                            caption=caption or "…",
                            **thread(message)
                        )
                        for c in rest:
                            await bot.send_message(cid, c, **thread(message))
                    finally:
                        os.remove(tmp_path)
    elif gemini_text:
        for chunk in split_smart(gemini_text, TELEGRAM_MSG_LIMIT):
            await message.answer(chunk)
    else:
        await message.answer("❌ Я не смог сгенерировать ответ.")
# ──────────────────────────────────────────────────────────────────────


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
    # если в запросе встречаются «аналитические» ключевые слова — чуть расширяем prompt
    needs_expansion = any(k in full_prompt.lower() for k in analysis_keywords)
    if needs_expansion:
        smart_prompt = (
            "Ответь чётко и по делу. Если в вопросе несколько частей — ответь на каждую. "
            "Приводи имена и конкретные примеры, если они есть. Не повторяй вопрос, просто ответь:\n\n"
        )
        full_prompt = smart_prompt + full_prompt

    # короткий путь для генерации подписей к картинкам
    if show_image and rus_word and not leftover:
        return await generate_short_caption(rus_word)

    # строим историю диалога (context window)
    conversation = chat_history.setdefault(cid, [])
    conversation.append({"role": "user", "parts": [full_prompt]})
    if len(conversation) > 8:
        conversation.pop(0)

    try:
        # показываем «печатаю…»
        await bot.send_chat_action(chat_id=cid, action="typing")

        # первый прогон Gemini
        resp = await model.generate_content_async(conversation)
        raw_model_text = resp.text.strip()

        # fallback только когда Gemini прямо говорит о нехватке знаний
        if (
            "обрезаны по состоянию на" in raw_model_text.lower()
            or "не обладаю информацией" in raw_model_text.lower()
        ):
            # делаем веб‑поиск по полному запросу
            snippets = web_search(full_prompt)
            # формируем новый prompt с фактами из поиска
            fallback_prompt = (
                "У меня есть результаты веб-поиска по запросу:\n"
                f"{snippets}\n\n"
                f"На их основе дай полный развёрнутый ответ на вопрос:\n{full_prompt}"
            )
            # повторный прогон Gemini
            resp2 = await model.generate_content_async([
                {"role": "user", "parts": [fallback_prompt]}
            ])
            raw_model_text = resp2.text.strip()

        # если Gemini не вернул кандидатов — значит запрос заблокировали
        if not resp.candidates:
            reason = getattr(resp.prompt_feedback, "block_reason", "неизвестна")
            logging.exception(f"[BOT] Запрос заблокирован Gemini: причина — {reason}")
            gemini_text = (
                f"⚠️ Запрос отклонён. Возможная причина: <b>{reason}</b>.\n"
                "Попробуйте переформулировать запрос."
            )
        else:
            # форматируем ответ
            gemini_text = format_gemini_response(raw_model_text)
            # сохраняем в историю для контекста
            conversation.append({"role": "model", "parts": [raw_model_text]})
            if len(conversation) > 8:
                conversation.pop(0)

    except Exception as e:
        logging.error(f"[BOT] Ошибка при обращении к Gemini: {e}")
        gemini_text = "⚠️ Произошла ошибка при генерации ответа. Попробуйте ещё раз позже."

    return gemini_text


async def vocab_reminder_loop():
    while True:
        now = datetime.utcnow()
        for uid, vocab in user_vocab.items():
            if not vocab_reminders_enabled.get(str(uid), True):
                continue
            for entry in vocab:
                last = datetime.fromisoformat(entry.get("last_reviewed", now.isoformat()))
                level = entry.get("review_level", 0)
                interval_days = [0, 1, 2, 4, 7, 14, 30]
                interval = interval_days[min(level, len(interval_days) - 1)]
                if (now - last).days >= interval:
                    try:
                        keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [
                                InlineKeyboardButton(text="✅ Помню", callback_data="review_remember"),
                                InlineKeyboardButton(text="❌ Не помню", callback_data="review_forget")
                            ]
                        ])
                        await bot.send_message(uid,
                            f"🔁 Пора повторить слово: <b>{entry['word']}</b>\n"
                            f"{entry['meaning']}\n<i>{entry['example']}</i>",
                            reply_markup=keyboard
                        )
                        break  # только одно напоминание за цикл
                    except Exception as e:
                        logging.exception(f"[VOCAB_REMINDER] Ошибка при отправке: {e}")
                    break
        await asyncio.sleep(3600)  # проверяем раз в час

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
                logging.exception(f"[REMINDER] Не удалось отправить напоминание: {e}")

        
        await asyncio.sleep(30)  # каждые 30 секунд проверяем


@dp.message()
async def handle_all_messages(message: Message):
    user_input = (message.text or "").strip()

    # --- Проверка на запрос с озвучкой: ---
    if re.search(r"(прочитай это|озвучь голосом|ответь голосом|ответь войсом)", user_input, re.IGNORECASE):
        # Если это ответ на сообщение — озвучим его напрямую
        if message.reply_to_message and message.reply_to_message.text:
            target = message.reply_to_message.text
            voice_lang = "ru-RU" if detect_lang(target) == "ru" else "en-US"
            await message.reply("🎧 Озвучиваю...")
            await send_voice_message(message.chat.id, target, voice_lang)
            return

        # Иначе генерируем ответ и озвучиваем
        cleaned = re.sub(r"(прочитай это|озвучь голосом|ответь голосом|ответь войсом)", "", user_input, flags=re.IGNORECASE).strip()
        if not cleaned:
            await message.reply("❌ Напиши, что озвучить.")
            return

        await message.reply("🎤 Генерирую ответ и озвучиваю...")

        try:
            response = await model.generate_content_async([{"role": "user", "parts": [cleaned]}])
            reply_text = response.text.strip()

            # --------------- (ИЗМЕНЕНО) Очистим лишние символы. ---------------
            # Хочешь, можешь поместить это внутрь send_voice_message, 
            # но проще отфильтровать прямо здесь:
            reply_text = re.sub(r"[*_`]+", "", reply_text)

            lang = detect_lang(reply_text)
            voice_lang = "ru-RU" if lang == "ru" else "en-US"
            await send_voice_message(message.chat.id, reply_text, voice_lang)
        except Exception as e:
            logging.exception("[BOT] Ошибка при генерации и озвучке:")
            await message.reply("❌ Не удалось сгенерировать или озвучить.")
        return

    # --- Обычная обработка всех остальных сообщений ---
    await handle_all_messages_impl(message, user_input)


# ---------------------- Запуск бота ---------------------- #
async def main():
    global BOT_ID
    me = await bot.get_me()
    BOT_ID = me.id
    
    asyncio.create_task(reminder_loop())
    asyncio.create_task(vocab_reminder_loop())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
