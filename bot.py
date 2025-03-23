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
import google.generativeai as genai
import tempfile
from aiogram.filters import Command

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-1.5-pro-latest")

chat_history = {}

NAME_COMMANDS = ["как тебя зовут", "твое имя", "твоё имя", "what is your name", "who are you"]
INFO_COMMANDS = [
    "кто тебя создал", "кто ты", "кто разработчик", "кто твой автор",
    "кто твой создатель", "чей ты бот", "кем ты был создан",
    "кто хозяин", "кто твой владелец", "в смысле кто твой создатель"
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
    "покажи", "покажи мне", "фото", "изображение", "отправь фото",
    "пришли картинку", "прикрепи фото", "покажи картинку",
    "дай фото", "дай изображение", "картинка"
]

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

    for placeholder, block in code_blocks.items():
        text = text.replace(escape(placeholder), block)

    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)

    return text.strip()

def get_safe_prompt(text: str) -> str:
    text = re.sub(r'[.,!?\-\n]', ' ', text.lower())
    match = re.search(r'покажи(?:\s+мне)?\s+(\w+)', text)
    return match.group(1) if match else re.sub(r"[^a-zA-Zа-яА-Я0-9\s]", "", text).strip().split(" ")[0]

async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    url = f"https://api.unsplash.com/photos/random?query={prompt}&client_id={access_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data['urls']['regular']
    except Exception as e:
        logging.warning(f"Ошибка при получении изображения: {e}")
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
    greet = (
        "Привет! Я <b>VAI</b> — интеллектуальный помощник.\n\n"
        "Я могу отвечать на самые разные вопросы, делиться фактами, рассказывать интересное и даже показывать изображения по твоему запросу.\n\n"
        "Попробуй, например:\n"
        "• «покажи тигра»\n"
        "• «расскажи про Луну»\n\n"
        "Всегда рад пообщаться! 🧠✨"
    )
    await message.answer(greet)

@dp.message()
async def handle_msg(message: Message):
    user_input = message.text.strip()
    cid = message.chat.id
    logging.info(f"[BOT] cid={cid}, text='{user_input}'")

    if any(name_trig in user_input.lower() for name_trig in NAME_COMMANDS):
        await message.answer("Меня зовут <b>VAI</b>!")
        return

    if any(info_trig in user_input.lower() for info_trig in INFO_COMMANDS):
        r = random.choice(OWNER_REPLIES)
        await message.answer(r)
        return

    chat_history.setdefault(cid, []).append({"role": "user", "parts": [user_input]})
    if len(chat_history[cid]) > 5:
        chat_history[cid].pop(0)

    try:
        await bot.send_chat_action(cid, "typing")
        resp = model.generate_content(chat_history[cid])
        gemini_text = format_gemini_response(resp.text)
        logging.info(f"[GEMINI] => {gemini_text[:200]}")

        prompt = get_safe_prompt(user_input)
        image_url = await get_unsplash_image_url(prompt, UNSPLASH_ACCESS_KEY)
        triggered = any(t in user_input.lower() for t in IMAGE_TRIGGERS)
        logging.info(f"[BOT] triggered={triggered}, image={image_url}")

        if image_url and triggered:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(image_url) as r:
                    if r.status == 200:
                        photo_bytes = await r.read()
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmpf:
                            tmpf.write(photo_bytes)
                            tmp_path = tmpf.name

                        parts = split_text(gemini_text)
                        try:
                            await bot.send_chat_action(cid, "upload_photo")
                            file = FSInputFile(tmp_path, filename="image.jpg")
                            cpt = parts[0] if parts else "..."
                            await bot.send_photo(cid, file, caption=cpt)
                            for chunk in parts[1:]:
                                await message.answer(chunk)
                        finally:
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)
                        return

        for chunk in split_text(gemini_text):
            await message.answer(chunk)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Нет связи с облаками.")
    except ConnectionError:
        await message.answer("⚠️ Нет подключения к интернету.")
    except Exception as e:
        logging.error(f"[BOT] Error: {e}")
        await message.answer(f"❌ Ошибка: {escape(str(e))}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
