# ---------------------- Импорты ---------------------- #
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
    CallbackQuery, InputFile, BufferedInputFile
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

# Дополнительные импорты для голосовой обработки
from pydub import AudioSegment
import speech_recognition as sr
from gtts import gTTS

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

ADMIN_ID = 1936733487

SUPPORT_PROMPT_TEXT = (
    "Отправьте любое сообщение (текст, фото, видео, файлы, аудио, голосовые) — всё дойдёт до поддержки.\n\n"
    "Также вы можете отправлять голосовые сообщения – я отвечу голосом!"
)

def thread_kwargs(message: Message) -> dict:
    """
    Если это супергруппа/группа с топиками, возвращаем словарь {"message_thread_id": ...}.
    """
    if (message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]
            and message.message_thread_id is not None):
        return {"message_thread_id": message.message_thread_id}
    return {}

# ---------------------- Статистика (опционально) ---------------------- #
stats = {
    "messages_total": 0,
    "unique_users": set(),
    "files_received": 0,
    "commands_used": {}
}

def _register_message_stats(message: Message):
    stats["messages_total"] += 1
    stats["unique_users"].add(message.from_user.id)
    if message.text and message.text.startswith('/'):
        cmd = message.text.split()[0]
        stats["commands_used"][cmd] = stats["commands_used"].get(cmd, 0) + 1

# ---------------------- Функция отправки ответа админа одним сообщением ---------------------- #
async def send_admin_reply_as_single_message(admin_message: Message, user_id: int):
    """
    Отправляет пользователю user_id одно сообщение, содержащее:
    <b>Ответ от поддержки:</b> и контент ответа админа.
    Для медиа-сообщений префикс добавляется в caption.
    """
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

# ---------------------- Обработчики команд ---------------------- #
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """
    В ЛС — обычный старт (с информацией о голосовой поддержке).
    В группе/супергруппе — снимаем отключение (удаляем chat.id из disabled_chats).
    """
    _register_message_stats(message)
    if message.chat.type == ChatType.PRIVATE and message.text.startswith("/start support"):
        support_mode_users.add(message.from_user.id)
        await message.answer(SUPPORT_PROMPT_TEXT)
        return
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if message.chat.id in disabled_chats:
            disabled_chats.remove(message.chat.id)
            save_disabled_chats(disabled_chats)
            await message.answer("Бот снова включён в этом чате.", **thread_kwargs(message))
            logging.info(f"[BOT] Бот снова включён в группе {message.chat.id}")
        else:
            await message.answer("Бот уже активен в этом чате.", **thread_kwargs(message))
        return
    greet = """Привет! Я <b>VAI</b> — интеллектуальный помощник 😊

Вот что я умею:
• Читаю PDF, DOCX, TXT и .py-файлы — просто отправь мне файл.
• Отвечаю на вопросы по содержимому файла.
• Помогаю с кодом — напиши #рефактор и вставь код.
• Показываю изображения по ключевым словам.
• Поддерживаю команды /help и режим поддержки.
• Теперь поддерживаю голосовые сообщения – отправляйте голос, а я отвечу голосом!

Всегда на связи!"""
    await bot.send_message(chat_id=message.chat.id, text=greet, **thread_kwargs(message))

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    """
    В группе/супергруппе — добавляем чат в disabled_chats, отключая бота.
    """
    _register_message_stats(message)
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        disabled_chats.add(message.chat.id)
        save_disabled_chats(disabled_chats)
        await bot.send_message(chat_id=message.chat.id, text="Бот отключён в этом чате.", **thread_kwargs(message))
        logging.info(f"[BOT] Бот отключён в группе {message.chat.id}")
    else:
        await message.answer("Команда /stop работает только в группе.")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    _register_message_stats(message)
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

# ---------------------- Режим поддержки (callback) ---------------------- #
@dp.callback_query(F.data == "support_request")
async def handle_support_click(callback: CallbackQuery):
    await callback.answer()
    support_mode_users.add(callback.from_user.id)
    await callback.message.answer(SUPPORT_PROMPT_TEXT)

# ---------------------- Обработка голосовых сообщений от пользователей ---------------------- #
# Обработка голосовых сообщений выполняется только в обычном чате (не в поддержке)
@dp.message()
async def handle_all_messages(message: Message):
    # 1. Если админ отвечает реплаем в своём чате
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

    # 2. Если сообщение – голосовое от пользователя (и не от админа)
    if message.voice and cid != ADMIN_ID:
        try:
            file_info = await bot.get_file(message.voice.file_id)
            voice_file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
            async with aiohttp.ClientSession() as session:
                async with session.get(voice_file_url) as resp:
                    voice_bytes = await resp.read()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_voice:
                tmp_voice.write(voice_bytes)
                ogg_path = tmp_voice.name
            # Конвертируем OGG в WAV
            audio = AudioSegment.from_file(ogg_path, format="ogg")
            wav_path = ogg_path.replace(".ogg", ".wav")
            audio.export(wav_path, format="wav")
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio_data = recognizer.record(source)
            try:
                text_from_voice = recognizer.recognize_sphinx(audio_data, language="ru-RU")
            except Exception as e:
                logging.warning(f"[BOT] Ошибка распознавания речи: {e}")
                text_from_voice = ""
            os.remove(ogg_path)
            os.remove(wav_path)
            if text_from_voice:
                # Генерируем ответ через Gemini
                response_text = await generate_and_send_gemini_response(cid, text_from_voice, False, "", "")
                # Синтезируем голосовой ответ через gTTS
                tts = gTTS(response_text, lang="ru")
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_tts:
                    tts.save(tmp_tts.name)
                    mp3_path = tmp_tts.name
                audio_tts = AudioSegment.from_mp3(mp3_path)
                ogg_tts_path = mp3_path.replace(".mp3", ".ogg")
                audio_tts.export(ogg_tts_path, format="ogg")
                os.remove(mp3_path)
                caption = "<b>Ответ от поддержки:</b>"
                await bot.send_voice(chat_id=cid, voice=InputFile(ogg_tts_path), caption=caption)
                os.remove(ogg_tts_path)
            else:
                await bot.send_message(chat_id=cid, text="Не удалось распознать голосовое сообщение.")
        except Exception as e:
            logging.error(f"[BOT] Ошибка обработки голосового сообщения: {e}")
            await bot.send_message(chat_id=cid, text="Ошибка обработки голосового сообщения.")
        return

    # 3. Если сообщение из группы/супергруппы и чат отключён — игнорируем
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if cid in disabled_chats:
            return

    # 4. Если сообщение содержит документ (файл)
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

    logging.info(f"[DEBUG] Message from {uid}: content_type={message.content_type}, has_document={bool(message.document)}, text={message.text!r}")
    await handle_msg(message)

# ---------------------- "Вай покажи ..." ---------------------- #
@dp.message(F.text.lower().startswith("вай покажи"))
async def group_show_request(message: Message):
    await handle_msg(message)

# ---------------------- Логика генерации ответа Gemini ---------------------- #
async def generate_and_send_gemini_response(cid, full_prompt, show_image, rus_word, leftover):
    gemini_text = ""
    analysis_keywords = [
        "почему", "зачем", "на кого", "кто", "что такое", "влияние",
        "философ", "отрицал", "повлиял", "смысл", "экзистенциализм", "опроверг"
    ]
    needs_expansion = any(k in full_prompt.lower() for k in analysis_keywords)
    if needs_expansion:
        smart_prompt = (
            "Ответь чётко и по делу. Если в вопросе несколько частей — ответь на каждую. "
            "Приводи имена и конкретные примеры, если они есть. Не повторяй вопрос, просто ответь:\n\n"
        )
        full_prompt = smart_prompt + full_prompt
    if show_image and rus_word and not leftover:
        gemini_text = generate_short_caption(rus_word)
        return gemini_text
    conversation = chat_history.setdefault(cid, [])
    # Исправлено: используем "parts" как и ожидалось
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

NAME_COMMANDS = [
    "как тебя зовут", "твое имя", "твоё имя", "what is your name", "who are you"
]
INFO_COMMANDS = [
    "кто тебя создал", "кто ты", "кто разработчик", "кто твой автор",
    "кто твой создатель", "чей ты бот", "кем ты был создан",
    "кто хозяин", "кто твой владелец", "в смысле кто твой создатель"
]

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

# ---------------------- Запуск бота ---------------------- #
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
