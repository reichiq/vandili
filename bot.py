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
import requests  # Для обращения к Deepseek

# ---------------------- Инициализация ---------------------- #
key_path = '/root/vandili/gcloud-key.json'
credentials = service_account.Credentials.from_service_account_file(key_path)
translate_client = translate.TranslationServiceClient(credentials=credentials)

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")  # Новый ключ для Deepseek
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
morph = MorphAnalyzer()

# Не используем Google Generative AI, поэтому закомментированы следующие строки:
# genai.configure(api_key=GEMINI_API_KEY)
# model = genai.GenerativeModel(model_name="models/gemini-1.5-pro-latest")

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
    "Не упоминай, что ты обучен Google или являешься большой языковой моделью. "
    "Если пользователь оскорбляет, отвечай кратко и вежливо."
)

# ---------------------- Deepseek API ---------------------- #
def call_deepseek_api(prompt: str, api_key: str) -> str:
    """
    Обращаемся к Deepseek API, передаём prompt, получаем text-ответ.
    Замените URL и логику под реальную спецификацию Deepseek.
    """
    url = "https://api.deepseek.ai/v1/generate"  # Пример, выдуманный эндпоинт
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = {
        "prompt": prompt,
        "max_tokens": 400,  # можно изменить
        "temperature": 0.7  # пример
    }
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=30)
        if resp.status_code == 200:
            js = resp.json()
            if "text" in js:
                return js["text"]
            elif "choices" in js and len(js["choices"]) > 0:
                return js["choices"][0].get("text", "")
            else:
                return "Пустой ответ от Deepseek"
        else:
            logging.error(f"[Deepseek] Ошибка {resp.status_code}: {resp.text}")
            return "Произошла ошибка при запросе к Deepseek."
    except Exception as e:
        logging.error(f"[Deepseek] Исключение при запросе: {e}")
        return "Ошибка при обращении к Deepseek."

def deepseek_generate_content(messages: list[dict]) -> str:
    """
    Формируем общий prompt из messages и вызываем Deepseek.
    """
    prompt_text = ""
    for msg in messages:
        if msg["role"] == "user":
            prompt_text += f"Пользователь: {msg['parts'][0]}\n"
        elif msg["role"] == "assistant":
            prompt_text += f"Помощник: {msg['parts'][0]}\n"
    prompt_text += "Помощник:"
    result = call_deepseek_api(prompt_text, DEEPSEEK_API_KEY)
    return result

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
            await bot.send_message(
                chat_id=message.chat.id,
                text="Произошла ошибка при отправке сообщения. Попробуйте позже.",
                message_thread_id=message.message_thread_id
            )
            logging.error(f"[BOT] Ошибка при пересылке в поддержку: {e}")
        finally:
            support_mode_users.discard(uid)
        return
    await handle_msg(message)

@dp.message(F.text.lower().startswith("вай покажи"))
async def group_show_request(message: Message):
    await handle_msg(message)

# ---------------------- Основная логика ---------------------- #
async def generate_and_send_deepseek_response(cid, full_prompt, show_image, rus_word, leftover, thread_id):
    """
    Генерация ответа через Deepseek. Обнуляем историю, вставляем системный промпт и текущее сообщение.
    """
    chat_history[cid] = []
    chat_history[cid].append({"role": "user", "parts": [SYSTEM_PROMPT]})
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
                logging.error(f"[BOT] Ошибка при обращении к Deepseek: {e}")
                text = "⚠️ Произошла ошибка при генерации ответа. Попробуйте ещё раз позже."
        return text

def format_deepseek_response(text: str) -> str:
    """
    Приводим ответ Deepseek к HTML-формату, удаляем лишние упоминания, заменяем "* " на "• " и добавляем переносы строк между пунктами.
    """
    text = escape(text)
    text = remove_google_lmm_mentions(text)
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

def remove_google_lmm_mentions(txt: str) -> str:
    txt = re.sub(r"(я\s+большая\s+языковая\s+модель.*google\.?)", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"(i\s+am\s+a\s+large\s+language\s+model.*google\.?)", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"большая\s+языковая\s+модель", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"large\s+language\s+model", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"обученная(\s+\S+){0,2}\s+google", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt

def generate_short_caption(rus_word: str) -> str:
    """
    Генерация короткой подписи через Deepseek.
    """
    short_prompt = (
        "ИНСТРУКЦИЯ: Ты — VAI, бот от Vandili. Если есть факты, перечисляй их построчно. "
        f"Напиши одну короткую, дружелюбную подпись к изображению с «{rus_word}» (до 15 слов)."
    )
    messages = [
        {"role": "user", "parts": [SYSTEM_PROMPT]},
        {"role": "user", "parts": [short_prompt]}
    ]
    result = deepseek_generate_content(messages)
    result = remove_google_lmm_mentions(result)
    result = re.sub(r"(\.\s*)•", r".\n•", result)
    return result.strip()

async def handle_msg(message: Message, prompt_mode: bool = False):
    cid = message.chat.id
    thread_id = message.message_thread_id
    user_input = (message.text or "").strip()
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if cid not in enabled_chats:
            return
        text_lower = user_input.lower()
        mention_bot = BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in text_lower
        is_reply_to_bot = (message.reply_to_message and message.reply_to_message.from_user and (message.reply_to_message.from_user.id == bot.id))
        mention_keywords = ["вай", "вэй", "vai"]
        if not mention_bot and not is_reply_to_bot and not any(k in text_lower for k in mention_keywords):
            return
    logging.info(f"[BOT] cid={cid}, text='{user_input}'")
    lower_inp = user_input.lower()
    if any(nc in lower_inp for nc in NAME_COMMANDS):
        await bot.send_message(chat_id=cid, text="Меня зовут <b>VAI</b>!", message_thread_id=thread_id)
        return
    if any(ic in lower_inp for ic in INFO_COMMANDS):
        await bot.send_message(chat_id=cid, text=random.choice(OWNER_REPLIES), message_thread_id=thread_id)
        return
    show_image, rus_word, image_en, leftover = parse_russian_show_request(user_input)
    if show_image and rus_word:
        leftover = replace_pronouns_morph(leftover, rus_word)
    leftover = leftover.strip()
    full_prompt = f"{rus_word} {leftover}".strip() if rus_word else leftover
    image_url = None
    if show_image:
        image_url = await get_unsplash_image_url(image_en, UNSPLASH_ACCESS_KEY)
    has_image = bool(image_url)
    logging.info(f"[BOT] show_image={show_image}, rus_word='{rus_word}', image_en='{image_en}', leftover='{leftover}', image_url='{image_url}'")
    deepseek_text = await generate_and_send_deepseek_response(cid, full_prompt, show_image, rus_word, leftover, thread_id)
    if has_image:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(image_url) as r:
                if r.status == 200:
                    photo_bytes = await r.read()
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmpf:
                        tmpf.write(photo_bytes)
                        tmp_path = tmpf.name
                    try:
                        await bot.send_chat_action(cid, "upload_photo", message_thread_id=thread_id)
                        file = FSInputFile(tmp_path, filename="image.jpg")
                        caption, rest = split_caption_and_text(deepseek_text)
                        await bot.send_photo(chat_id=cid, photo=file, caption=caption if caption else "...", message_thread_id=thread_id)
                        for c in rest:
                            await bot.send_message(chat_id=cid, text=c, message_thread_id=thread_id)
                    finally:
                        os.remove(tmp_path)
    else:
        if deepseek_text:
            chunks = split_smart(deepseek_text, TELEGRAM_MSG_LIMIT)
            for c in chunks:
                await bot.send_message(chat_id=cid, text=c, message_thread_id=thread_id)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
