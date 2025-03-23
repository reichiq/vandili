import logging
import os
import re
import random
import aiohttp
from io import BytesIO
from aiogram.client.default import DefaultBotProperties
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, Message
from html import escape
from dotenv import load_dotenv
from pathlib import Path
import asyncio
import tempfile
from aiogram.filters import Command

import google.generativeai as genai
from google.generativeai.types import Content

# Попытка подключить переводчик
try:
    from googletrans import Translator
    translator = Translator()
    USE_TRANSLATOR = True
except ImportError:
    translator = None
    USE_TRANSLATOR = False

# Load environment variables
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Настройка Gemini с system-инструкцией
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    model_name="models/gemini-1.5-pro-latest",
    system_instruction=(
        "Никогда не говори, что ты текстовая модель, языковая модель, ИИ или искусственный интеллект. "
        "Никогда не упоминай, что ты не можешь что-то сделать, особенно показать изображения. "
        "Никогда не предлагай искать в Google, Yandex или других поисковиках. "
        "Никогда не вставляй ссылки. Если тебя просят показать изображение, просто опиши его или продолжай как будто оно есть."
    )
)

chat_history = {}

# Команды
NAME_COMMANDS = ["как тебя зовут", "твое имя", "твоё имя", "what is your name", "who are you"]
INFO_COMMANDS = [
    "кто тебя создал", "кто ты", "кто разработчик", "кто твой автор",
    "кто твой создатель", "чей ты бот", "кем ты был создан", "кто хозяин", "кто твой владелец"
]
OWNER_REPLIES = [
    "Я — <b>VAI</b>, Telegram-бот, созданный <i>Vandili</i>.",
    "Мой создатель — <b>Vandili</b>. Я работаю для него.",
    "Я принадлежу <i>Vandili</i>, он мой автор.",
    "Создан <b>Vandili</b> — именно он дал мне жизнь.",
    "Я бот <b>Vandili</b>. Всё просто.",
    "Я продукт <i>Vandili</i>. Он мой единственный владелец."
]
IMAGE_TRIGGERS = [
    "покажи", "покажи мне", "фото", "изображение", "отправь фото", "пришли картинку", "прикрепи фото",
    "покажи картинку", "дай фото", "дай изображение", "картинка"
]
PROMPT_FIX = {
    "пудель": "poodle", "пудели": "poodle", "кошка": "cat", "кошки": "cats",
    "кот": "cat", "коты": "cats", "собака": "dog", "собаки": "dogs", "орхидея": "orchid",
    "орхидею": "orchid", "орхидеи": "orchids", "персики": "peaches", "обезьяна": "monkey",
    "обезьяну": "monkey"
}

UNWANTED_REGEX = [
    r"(?is)(\bя\s+(?:текстов\w*|language\s+model|большая\s+языковая\s+модель)[^.]{0,50})",
    r"(?is)(\bне\s+могу\s+показать\s+(изображени|графику|что-либо).{0,50})",
    r"(?is)(https?:\/\/[^\s)]+)",  # URL
    r"(?is)(google|yandex|bing|поиск|поисковик|search\s+engine)",
    r"(?is)(рекомендую\s+поискать|советую\s+посмотреть)",
    r"(?is)(вы\s+можете\s+найти.*?(фото|картинки))",
]

def remove_unwanted_phrases(text: str) -> str:
    for pattern in UNWANTED_REGEX:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text

def maybe_shorten(text: str, user_input: str) -> str:
    if "покажи" in user_input.lower() and not re.search(r"(расскажи|факт|опиши|объясни)", user_input.lower()):
        sents = re.split(r'(?<=[.!?])\s+', text)
        return " ".join(sents[:2]).strip()
    return text.strip()

def format_gemini_response(text: str, user_input: str) -> str:
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", "", text)
    text = re.sub(r"\[.*?(вставить|insert|фото|image|picture).*?\]", "", text, flags=re.IGNORECASE)
    text = escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)
    text = remove_unwanted_phrases(text)
    return maybe_shorten(text, user_input)

def get_safe_prompt(user_input: str) -> str:
    clean = re.sub(r'[.,!?\-\n]', ' ', user_input.lower())
    clean = re.sub(r"\b(расскажи|покажи|фото|изображение|картинка|про|о|мне|опиши|факт|интересное|дай|что)\b", "", clean)
    words = clean.strip().split()
    for i, w in enumerate(words):
        if w in PROMPT_FIX:
            words[i] = PROMPT_FIX[w]
    prompt = " ".join(words) or "random"
    if USE_TRANSLATOR:
        try:
            translated = translator.translate(prompt, src="ru", dest="en").text
            return translated.strip() or prompt
        except:
            return prompt
    return prompt

async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    url = f"https://api.unsplash.com/photos/random?query={prompt}&client_id={access_key}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                if r.status == 200:
                    data = await r.json()
                    return data["urls"]["regular"]
    except Exception as e:
        logging.warning(f"[UNSPLASH] Error: {e}")
    return None

def split_text(text: str, max_len=950):
    parts = []
    while len(text) > max_len:
        idx = text[:max_len].rfind('. ')
        if idx == -1:
            idx = max_len
        parts.append(text[:idx+1].strip())
        text = text[idx+1:].strip()
    if text:
        parts.append(text)
    return parts

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я <b>VAI</b> — бот, созданный <i>Vandili</i>.\n\n"
        "<i>Я умею:</i>\n"
        "• Отвечать на твои вопросы (Gemini)\n"
        "• Присылать картинки (Unsplash)\n\n"
        "Просто напиши «покажи кота» или «расскажи про Париж»!\n"
        "Если хочешь узнать обо мне — спроси «кто тебя создал».\n\n"
        "Приятного общения! 🦾"
    )

@dp.message()
async def handle_msg(message: Message):
    cid = message.chat.id
    user_input = message.text.strip()

    if any(trig in user_input.lower() for trig in NAME_COMMANDS):
        await message.answer("Меня зовут <b>VAI</b>!", parse_mode=ParseMode.HTML)
        return

    if any(trig in user_input.lower() for trig in INFO_COMMANDS):
        await message.answer(random.choice(OWNER_REPLIES), parse_mode=ParseMode.HTML)
        return

    chat_history.setdefault(cid, []).append({"role": "user", "parts": [user_input]})
    if len(chat_history[cid]) > 5:
        chat_history[cid].pop(0)

    try:
        await bot.send_chat_action(cid, "typing")
        response = model.generate_content(chat_history[cid])
        gemini_text = format_gemini_response(response.text, user_input)
        prompt = get_safe_prompt(user_input)
        image_url = await get_unsplash_image_url(prompt, UNSPLASH_ACCESS_KEY)
        triggered = any(t in user_input.lower() for t in IMAGE_TRIGGERS)

        if image_url and triggered:
            async with aiohttp.ClientSession() as s:
                async with s.get(image_url) as r:
                    if r.status == 200:
                        photo = await r.read()
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                            tmp.write(photo)
                            tmp_path = tmp.name

                        try:
                            await bot.send_chat_action(cid, "upload_photo")
                            file = FSInputFile(tmp_path)
                            parts = split_text(gemini_text)
                            await bot.send_photo(cid, file, caption=parts[0] if parts else "", parse_mode=ParseMode.HTML)
                            for chunk in parts[1:]:
                                await message.answer(chunk, parse_mode=ParseMode.HTML)
                        finally:
                            os.remove(tmp_path)
                        return

        for chunk in split_text(gemini_text):
            await message.answer(chunk, parse_mode=ParseMode.HTML)

    except Exception as e:
        logging.error(f"[BOT] Error: {e}")
        await message.answer(f"❌ Ошибка: {escape(str(e))}", parse_mode=ParseMode.HTML)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
