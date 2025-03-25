import logging
import os
import re
import random
import aiohttp
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

from google.cloud import translate
from google.oauth2 import service_account

import json
import requests

# ---------------------- Инициализация ---------------------- #
key_path = '/root/vandili/gcloud-key.json'
credentials = service_account.Credentials.from_service_account_file(key_path)
translate_client = translate.TranslationServiceClient(credentials=credentials)

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")  # Ключ для DeepSeek через OpenRouter
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
morph = MorphAnalyzer()

chat_history = {}

ENABLED_CHATS_FILE = "enabled_chats.json"

def load_enabled_chats() -> set:
    if not os.path.exists(ENABLED_CHATS_FILE):
        return set()
    try:
        with open(ENABLED_CHATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        logging.warning(f"[BOT] Не удалось загрузить enabled_chats: {e}")
        return set()

def save_enabled_chats(chats: set):
    try:
        with open(ENABLED_CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(chats), f)
    except Exception as e:
        logging.warning(f"[BOT] Не удалось сохранить enabled_chats: {e}")

enabled_chats = load_enabled_chats()
support_mode_users = set()
ADMIN_ID = 1936733487

# ---------------------- Системный промпт ---------------------- #
SYSTEM_PROMPT = (
    "Ты — VAI, Telegram-бот, созданный Vandili. Отвечай вежливо. "
    "Если пользователь здоровается — можешь поздороваться, но не повторяй приветствие в каждом ответе. "
    "Если просят факты, выводи их построчно (каждый пункт с новой строки, например, с символом •). "
    "Если пользователь оскорбляет, отвечай кратко и вежливо."
)

# ---------------------- DeepSeek Chat API через OpenRouter ---------------------- #
def call_deepseek_chat_api(chat_messages: list[dict], api_key: str) -> str:
    """
    Отправляем запрос к DeepSeek Chat API через OpenRouter.
    Запрос отправляется на:
      POST https://openrouter.ai/api/v1/chat/completions
    Тело запроса содержит:
      {
         "model": "deepseek/deepseek-chat-v3-0324:free",
         "messages": [ { "role": "system", "content": "..." }, { "role": "user", "content": "..." } ]
      }
    """
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = {
        "model": "deepseek/deepseek-chat-v3-0324:free",
        "messages": chat_messages
    }
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=30)
        if resp.status_code == 200:
            js = resp.json()
            choices = js.get("choices", [])
            if choices:
                return choices[0]["message"]["content"]
            else:
                return "Пустой ответ от DeepSeek (нет choices)."
        else:
            logging.error(f"[DeepSeek] Ошибка {resp.status_code}: {resp.text}")
            return "Произошла ошибка при запросе к DeepSeek."
    except Exception as e:
        logging.error(f"[DeepSeek] Исключение при запросе: {e}")
        return "Ошибка при обращении к DeepSeek."

def deepseek_generate_content(messages: list[dict]) -> str:
    """
    Преобразуем внутреннюю структуру сообщений в формат DeepSeek Chat API и вызываем его.
    """
    chat_messages = []
    for msg in messages:
        role = msg["role"]
        content = msg["parts"][0]
        chat_messages.append({"role": role, "content": content})
    return call_deepseek_chat_api(chat_messages, DEEPSEEK_API_KEY)

# ---------------------- Функция для парсинга запроса "вай покажи ..." ---------------------- #
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
    if rus_word:
        en_word = fallback_translate_to_english(rus_word)
    else:
        en_word = ""
    return (True, rus_word, en_word, leftover) if rus_word else (False, "", "", user_text)

# ---------------------- Команды ---------------------- #
@dp.message(Command("start"))
async def cmd_start(message: Message):
    greet = (
        "Привет! Я <b>VAI</b> — интеллектуальный помощник 😊\n\n"
        "Просто напиши мне, и я постараюсь ответить или помочь.\n"
        "Всегда на связи!"
    )
    await bot.send_message(
        chat_id=message.chat.id,
        text=greet,
        message_thread_id=message.message_thread_id
    )
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        enabled_chats.add(message.chat.id)
        save_enabled_chats(enabled_chats)
        logging.info(f"[BOT] Бот включён в группе {message.chat.id}")

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        enabled_chats.discard(message.chat.id)
        save_enabled_chats(enabled_chats)
        await bot.send_message(
            chat_id=message.chat.id,
            text="Бот отключён в этом чате.",
            message_thread_id=message.message_thread_id
        )
        logging.info(f"[BOT] Бот отключён в группе {message.chat.id}")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✉️ Написать в поддержку", callback_data="support_request")]
        ]
    )
    await bot.send_message(
        chat_id=message.chat.id,
        text="Если возник вопрос или хочешь сообщить об ошибке — напиши нам:",
        reply_markup=keyboard,
        message_thread_id=message.message_thread_id
    )

# ---------------------- Поддержка ---------------------- #
@dp.callback_query(F.data == "support_request")
async def handle_support_click(callback: CallbackQuery):
    await bot.send_message(
        chat_id=callback.message.chat.id,
        text="Напиши своё сообщение (можно с фото или видео). Я передам его в поддержку.",
        message_thread_id=callback.message.message_thread_id
    )
    support_mode_users.add(callback.from_user.id)
    await callback.answer()

@dp.message()
async def handle_all_messages(message: Message):
    uid = message.from_user.id
    if uid in support_mode_users:
        try:
            caption = message.caption or message.text or "[Без текста]"
            username_part = f" (@{message.from_user.username})" if message.from_user.username else ""
            content = (f"\u2728 <b>Новое сообщение в поддержку</b> от <b>{message.from_user.full_name}</b>{username_part} "
                       f"(id: <code>{uid}</code>):\n\n{caption}")
            if message.photo:
                file = await bot.get_file(message.photo[-1].file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        photo_bytes = await resp.read()
                await bot.send_photo(ADMIN_ID, photo=BufferedInputFile(photo_bytes, filename="image.jpg"), caption=content)
            elif message.video:
                file = await bot.get_file(message.video.file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        video_bytes = await resp.read()
                await bot.send_video(ADMIN_ID, video=BufferedInputFile(video_bytes, filename="video.mp4"), caption=content)
            elif message.document:
                file = await bot.get_file(message.document.file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        doc_bytes = await resp.read()
                await bot.send_document(ADMIN_ID, document=BufferedInputFile(doc_bytes, filename=message.document.file_name or "document"), caption=content)
            elif message.audio:
                file = await bot.get_file(message.audio.file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        audio_bytes = await resp.read()
                await bot.send_audio(ADMIN_ID, audio=BufferedInputFile(audio_bytes, filename=message.audio.file_name or "audio.mp3"), caption=content)
            elif message.voice:
                file = await bot.get_file(message.voice.file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        voice_bytes = await resp.read()
                await bot.send_voice(ADMIN_ID, voice=BufferedInputFile(voice_bytes, filename="voice.ogg"), caption=content)
            else:
                await bot.send_message(ADMIN_ID, content)
            await bot.send_message(
                chat_id=message.chat.id,
                text="Спасибо! Ваше сообщение отправлено в поддержку.",
                message_thread_id=message.message_thread_id
            )
        except Exception as e:
            logging.error(f"[BOT] Ошибка при пересылке в поддержку: {e}")
            await bot.send_message(
                chat_id=message.chat.id,
                text="Произошла ошибка при отправке сообщения. Попробуйте позже.",
                message_thread_id=message.message_thread_id
            )
        finally:
            support_mode_users.discard(uid)
        return
    await handle_msg(message)

@dp.message(F.text.lower().startswith("вай покажи"))
async def group_show_request(message: Message):
    await handle_msg(message)

# ---------------------- Основная логика и генерация ответа через DeepSeek ---------------------- #
async def generate_and_send_deepseek_response(cid, full_prompt, show_image, rus_word, leftover, thread_id):
    """
    Генерация ответа через DeepSeek. Обнуляем историю, добавляем system и user сообщения,
    затем вызываем deepseek_generate_content и форматируем ответ.
    """
    chat_history[cid] = []
    chat_history[cid].append({"role": "system", "parts": [SYSTEM_PROMPT]})
    if show_image and rus_word and not leftover:
        text = generate_short_caption(rus_word)
        return text
    else:
        text = ""
        if full_prompt:
            chat_history[cid].append({"role": "user", "parts": [full_prompt]})
            try:
                await bot.send_chat_action(cid, "typing", message_thread_id=thread_id)
                text = deepseek_generate_content(chat_history[cid])
                text = format_deepseek_response(text)
            except Exception as e:
                logging.error(f"[BOT] Ошибка при обращении к DeepSeek: {e}")
                text = "⚠️ Произошла ошибка при генерации ответа. Попробуйте ещё раз позже."
        return text

def format_deepseek_response(text: str) -> str:
    """
    Форматируем ответ DeepSeek к HTML, заменяем "* " на "• " и добавляем переносы строк между ". •".
    """
    text = escape(text)
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
    text = '\n'.join(new_lines)
    text = re.sub(r"(\.\s*)•", r".\n•", text)
    return text.strip()

CAPTION_LIMIT = 950
TELEGRAM_MSG_LIMIT = 4096

IMAGE_TRIGGERS_RU = ["покажи", "покажи мне", "хочу увидеть", "пришли фото", "фото"]

NAME_COMMANDS = [
    "как тебя зовут", "твое имя", "твоё имя", "what is your name", "who are you", "я кто"
]
INFO_COMMANDS = [
    "кто тебя создал", "кто ты", "кто разработчик", "кто твой автор",
    "кто твой создатель", "чей ты бот", "кем ты был создан", "кто хозяин",
    "кто твой владелец", "в смысле кто твой создатель"
]
OWNER_REPLIES = [
    "Я — <b>VAI</b>, Telegram-бот, созданный <i>Vandili</i>.",
    "Мой создатель — <b>Vandili</b>. Я работаю для него.",
    "Я принадлежу <i>Vandili</i>, он мой автор.",
    "Создан <b>Vandili</b> — именно он дал мне жизнь.",
    "Я бот <b>Vandili</b>. Всё просто.",
    "Я продукт <i>Vandili</i>. Он мой единственный владелец."
]

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
        r"\bо\s+нем\b": f"о {word_prep}",
        r"\bо\s+нём\b": f"о {word_prep}",
        r"\bо\s+ней\b": f"о {word_prep}",
    }
    for pattern, repl in pronoun_map.items():
        leftover = re.sub(pattern, repl, leftover, flags=re.IGNORECASE)
    return leftover

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

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
