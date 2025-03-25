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
from aiogram.filters import Command, CommandStart
from pymorphy3 import MorphAnalyzer
from string import punctuation

from google.cloud import translate
from google.oauth2 import service_account

import json

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

# Используем модель Gemini 2.0-flash
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-2.0-flash")

chat_history = {}

ENABLED_CHATS_FILE = "enabled_chats.json"
ADMIN_ID = 1936733487
support_mode_users = set()

# Текст, который бот присылает в ЛС, когда переходит в «режим поддержки»
SUPPORT_PROMPT_TEXT = (
    "✉️ <b>Режим поддержки активирован.</b>\n\n"
    "Напиши сюда своё сообщение: текст, фото, видео, документы или голос — всё дойдёт до команды поддержки."
)

# ---------------------- Вспомогательная функция для топиков ---------------------- #
def thread_kwargs(message: Message) -> dict:
    if (
        message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]
        and message.message_thread_id is not None
    ):
        return {"message_thread_id": message.message_thread_id}
    return {}

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

# ---------------------- Команды /start и /stop ---------------------- #
@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandStart = None):
    if message.chat.type == ChatType.PRIVATE and command.args == "support":
        support_mode_users.add(message.from_user.id)
        await message.answer(SUPPORT_PROMPT_TEXT)
        return

    greet = (
        "Привет! Я <b>VAI</b> — интеллектуальный помощник 😊\n\n"
        "Просто напиши мне, и я постараюсь ответить или помочь.\n"
        "Всегда на связи!"
    )
    await message.answer(greet, **thread_kwargs(message))

    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        enabled_chats.add(message.chat.id)
        save_enabled_chats(enabled_chats)
        logging.info(f"[BOT] Бот включён в группе {message.chat.id}")

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        enabled_chats.discard(message.chat.id)
        save_enabled_chats(enabled_chats)
        await message.answer(
            "Бот отключён в этом чате.",
            **thread_kwargs(message)
        )
        logging.info(f"[BOT] Бот отключён в группе {message.chat.id}")

# ---------------------- Команда /help ---------------------- #
@dp.message(Command("help"))
async def cmd_help(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "Если возник вопрос или хочешь сообщить об ошибке — напиши нам:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="✉️ Написать в поддержку",
                        callback_data="support_request"
                    )
                ]]
            )
        )
    else:
        private_url = f"https://t.me/{BOT_USERNAME}?start=support"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="✉️ Написать в поддержку",
                    url=private_url
                )
            ]]
        )
        await message.answer(
            "Если возник вопрос или хочешь сообщить об ошибке — напиши нам:",
            reply_markup=keyboard,
            **thread_kwargs(message)
        )
# ---------------------- Режим поддержки ---------------------- #
@dp.callback_query(F.data == "support_request")
async def handle_support_click(callback: CallbackQuery):
    user_id = callback.from_user.id
    private_url = f"https://t.me/{BOT_USERNAME}?start=support"

    if callback.message.chat.type == ChatType.PRIVATE:
        support_mode_users.add(user_id)
        await callback.message.answer(
            "Отправьте любое сообщение (текст, фото, видео, файлы, аудио, голосовые) — всё дойдёт до поддержки."
        )
    else:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть чат с VAI",
                        url=private_url
                    )
                ]
            ]
        )
        await callback.message.answer(
            "Чтобы написать в поддержку, перейди в личные сообщения с VAI по этой ссылке:",
            reply_markup=keyboard,
            message_thread_id=callback.message.message_thread_id
        )
    await callback.answer()


@dp.message()
async def handle_all_messages(message: Message):
    # Обрабатываем start=support
    if message.chat.type == ChatType.PRIVATE and message.text and message.text.strip().lower() == "/start support":
        support_mode_users.add(message.from_user.id)
        await message.answer(
            "Вы перешли в режим поддержки. Напишите сообщение (текст, фото, видео, файл, аудио или войс)."
        )

    # Пересылка в поддержку
    uid = message.from_user.id
    if uid in support_mode_users:
        try:
            caption = message.caption or message.text or "[Без текста]"
            username_part = f" (@{message.from_user.username})" if message.from_user.username else ""
            content = (
                f"✨ <b>Новое сообщение в поддержку</b> от <b>{message.from_user.full_name}</b>{username_part} "
                f"(id: <code>{uid}</code>):\n\n{caption}"
            )

            async def fetch_and_forward(file_id, method, filename):
                file = await bot.get_file(file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        data = await resp.read()
                await getattr(bot, method)(
                    ADMIN_ID,
                    **{method.split("_", 1)[1]: BufferedInputFile(data, filename=filename)},
                    caption=content
                )

            if message.photo:
                await fetch_and_forward(message.photo[-1].file_id, "send_photo", "image.jpg")
            elif message.video:
                await fetch_and_forward(message.video.file_id, "send_video", "video.mp4")
            elif message.document:
                await fetch_and_forward(message.document.file_id, "send_document", message.document.file_name or "document")
            elif message.audio:
                await fetch_and_forward(message.audio.file_id, "send_audio", message.audio.file_name or "audio.mp3")
            elif message.voice:
                await fetch_and_forward(message.voice.file_id, "send_voice", "voice.ogg")
            else:
                await bot.send_message(ADMIN_ID, content)

            await message.answer("<i>Сообщение отправлено в поддержку.</i>")

        except Exception as e:
            logging.error(f"[BOT] Ошибка при пересылке: {e}")
            await message.answer("<b>Ошибка:</b> Не удалось отправить сообщение.")

        return

    # Если не в режиме поддержки — вызываем основную логику
    await handle_msg(message)


# ---------------------- Простейшая проверка "вай покажи ..." ---------------------- #
@dp.message(F.text.lower().startswith("вай покажи"))
async def group_show_request(message: Message):
    await handle_msg(message)


# ---------------------- Запуск ---------------------- #
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
