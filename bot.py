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
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
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

# ---------------------- Deepseek Chat API ---------------------- #
def call_deepseek_chat_api(chat_messages: list[dict], api_key: str) -> str:
    url = "https://platform.deepseek.ai/api/chat"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = {
        "model": "deepseek-chat",  # название модели, актуальное на 25.03.2025
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
                return "Пустой ответ от Deepseek (нет choices)."
        else:
            logging.error(f"[Deepseek] Ошибка {resp.status_code}: {resp.text}")
            return "Произошла ошибка при запросе к Deepseek."
    except Exception as e:
        logging.error(f"[Deepseek] Исключение при запросе: {e}")
        return "Ошибка при обращении к Deepseek."

def deepseek_generate_content(messages: list[dict]) -> str:
    chat_messages = []
    for msg in messages:
        role = msg["role"]
        part = msg["parts"][0]
        chat_messages.append({"role": role, "content": part})
    return call_deepseek_chat_api(chat_messages, DEEPSEEK_API_KEY)
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
            [
                InlineKeyboardButton(
                    text="✉️ Написать в поддержку",
                    callback_data="support_request"
                )
            ]
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
            content = (
                f"\u2728 <b>Новое сообщение в поддержку</b> от <b>{message.from_user.full_name}</b>{username_part} "
                f"(id: <code>{uid}</code>):\n\n{caption}"
            )

            file_id = None
            if message.photo:
                file_id = message.photo[-1].file_id
                filetype = "photo"
            elif message.video:
                file_id = message.video.file_id
                filetype = "video"
            elif message.document:
                file_id = message.document.file_id
                filetype = "document"
            elif message.audio:
                file_id = message.audio.file_id
                filetype = "audio"
            elif message.voice:
                file_id = message.voice.file_id
                filetype = "voice"
            else:
                await bot.send_message(ADMIN_ID, content)
                await bot.send_message(
                    chat_id=message.chat.id,
                    text="Спасибо! Ваше сообщение отправлено в поддержку.",
                    message_thread_id=message.message_thread_id
                )
                support_mode_users.discard(uid)
                return

            file = await bot.get_file(file_id)
            url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    file_bytes = await resp.read()

            buffered_file = BufferedInputFile(file_bytes, filename=f"{filetype}.dat")

            send_func = {
                "photo": bot.send_photo,
                "video": bot.send_video,
                "document": bot.send_document,
                "audio": bot.send_audio,
                "voice": bot.send_voice,
            }.get(filetype)

            if send_func:
                await send_func(ADMIN_ID, **{filetype: buffered_file}, caption=content)

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
async def generate_and_send_deepseek_response(cid, full_prompt, show_image, rus_word, leftover, thread_id):
    """
    Генерация ответа через DeepSeek. Обнуляем историю, вставляем системный промпт и текущее сообщение.
    """
    chat_history[cid] = []

    # Добавляем системный промпт как отдельное system-сообщение
    chat_history[cid].append({"role": "system", "parts": [SYSTEM_PROMPT]})

    # Если запрос только на подпись к картинке (без leftover)
    if show_image and rus_word and not leftover:
        return generate_short_caption(rus_word)

    # Иначе: обычный запрос
    if full_prompt:
        chat_history[cid].append({"role": "user", "parts": [full_prompt]})
        try:
            await bot.send_chat_action(cid, "typing", message_thread_id=thread_id)
            text = deepseek_generate_content(chat_history[cid])
            return format_deepseek_response(text)
        except Exception as e:
            logging.error(f"[BOT] Ошибка при генерации DeepSeek: {e}")
            return "⚠️ Произошла ошибка при генерации ответа. Попробуйте ещё раз позже."
    return ""

def format_deepseek_response(text: str) -> str:
    """
    Очищаем и форматируем ответ от DeepSeek:
    - Экранируем HTML
    - Заменяем "* " на "• "
    - Добавляем переносы между ". •"
    """
    text = escape(text)

    lines = text.split('\n')
    new_lines = []
    for line in lines:
        stripped = line.lstrip()
        prefix_len = len(line) - len(stripped)
        if stripped.startswith('* ') and not stripped.startswith('**'):
            replaced = (' ' * prefix_len) + '• ' + stripped[2:]
            new_lines.append(replaced)
        else:
            new_lines.append(line)
    text = '\n'.join(new_lines)

    # Добавляем перенос строки после точки перед "•"
    text = re.sub(r"(\.\s*)•", r".\n•", text)

    return text.strip()

CAPTION_LIMIT = 950
TELEGRAM_MSG_LIMIT = 4096

def split_smart(text: str, limit: int) -> list[str]:
    results = []
    start = 0
    while start < len(text):
        remaining = text[start:]
        if len(remaining) <= limit:
            results.append(remaining.strip())
            break
        cut_pos = remaining.rfind('. ', 0, limit)
        if cut_pos == -1:
            cut_pos = remaining.rfind(' ', 0, limit)
        if cut_pos == -1:
            cut_pos = limit
        results.append(remaining[:cut_pos + 1].strip())
        start += cut_pos + 1
    return results

def split_caption_and_text(text: str) -> tuple[str, list[str]]:
    if len(text) <= CAPTION_LIMIT:
        return text, []
    chunks = split_smart(text, CAPTION_LIMIT)
    caption = chunks[0]
    leftover = " ".join(chunks[1:]).strip()
    return caption, split_smart(leftover, TELEGRAM_MSG_LIMIT)

async def handle_msg(message: Message, prompt_mode: bool = False):
    cid = message.chat.id
    thread_id = message.message_thread_id
    user_input = (message.text or "").strip()

    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if cid not in enabled_chats:
            return

        text_lower = user_input.lower()
        mention_bot = BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in text_lower
        is_reply_to_bot = (
            message.reply_to_message and message.reply_to_message.from_user and
            message.reply_to_message.from_user.id == bot.id
        )
        mention_keywords = ["вай", "вэй", "vai"]
        if not mention_bot and not is_reply_to_bot and not any(k in text_lower for k in mention_keywords):
            return

    logging.info(f"[BOT] cid={cid}, text='{user_input}'")

    lower_inp = user_input.lower()
    if any(nc in lower_inp for nc in NAME_COMMANDS):
        await bot.send_message(cid, "Меня зовут <b>VAI</b>!", message_thread_id=thread_id)
        return
    if any(ic in lower_inp for ic in INFO_COMMANDS):
        await bot.send_message(cid, random.choice(OWNER_REPLIES), message_thread_id=thread_id)
        return

    show_image, rus_word, image_en, leftover = parse_russian_show_request(user_input)
    if show_image and rus_word:
        leftover = replace_pronouns_morph(leftover, rus_word)

    leftover = leftover.strip()
    full_prompt = f"{rus_word} {leftover}".strip() if rus_word else leftover

    image_url = await get_unsplash_image_url(image_en, UNSPLASH_ACCESS_KEY) if show_image else None
    has_image = bool(image_url)

    logging.info(f"[BOT] show_image={show_image}, rus_word='{rus_word}', image_en='{image_en}', leftover='{leftover}', image_url='{image_url}'")

    response_text = await generate_and_send_deepseek_response(cid, full_prompt, show_image, rus_word, leftover, thread_id)

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
                        caption, rest = split_caption_and_text(response_text)
                        await bot.send_photo(cid, file, caption=caption or "...", message_thread_id=thread_id)
                        for part in rest:
                            await bot.send_message(cid, part, message_thread_id=thread_id)
                    finally:
                        os.remove(tmp_path)
    else:
        if response_text:
            for chunk in split_smart(response_text, TELEGRAM_MSG_LIMIT):
                await bot.send_message(cid, chunk, message_thread_id=thread_id)

def generate_short_caption(rus_word: str) -> str:
    prompt = (
        "ИНСТРУКЦИЯ: Ты — VAI, бот от Vandili. Если есть факты, перечисляй их построчно. "
        f"Напиши одну короткую, дружелюбную подпись к изображению с «{rus_word}» (до 15 слов)."
    )
    messages = [
        {"role": "system", "parts": [SYSTEM_PROMPT]},
        {"role": "user", "parts": [prompt]}
    ]
    result = deepseek_generate_content(messages)
    result = re.sub(r"(\.\s*)•", r".\n•", result)
    return result.strip()

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
        logging.warning(f"Ошибка при переводе '{rus_word}': {e}")
        return rus_word

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
