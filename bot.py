import logging
import os
import re
import random
import aiohttp
from io import BytesIO
from aiogram.client.default import DefaultBotProperties
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, Message
from html import escape
from dotenv import load_dotenv
from pathlib import Path
import asyncio

# Загрузка .env
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

import google.generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-1.5-pro-latest")

chat_history = {}

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

IMAGE_TRIGGERS = [
    "покажи", "покажи мне", "фото", "изображение", "отправь фото",
    "пришли картинку", "прикрепи фото", "покажи картинку", "дай фото", "дай изображение", "картинка"
]

STOPWORDS = {
    "покажи", "расскажи", "мне", "про", "факт", "фото", "изображение", "и", "что", "ты", "о", "про", "интересное"
}

# Форматирование ответа Gemini
def format_gemini_response(text: str) -> str:
    code_blocks = {}

    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        return placeholder

    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)
    text = re.sub(r"\[.*?(вставьте|image|insert).*?\]", "", text, flags=re.IGNORECASE)
    text = escape(text)

    for placeholder, block in code_blocks.items():
        text = text.replace(escape(placeholder), block)

    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)

    return text.strip()

# Деление текста по смыслу
def split_message(text, limit=950):
    if len(text) <= limit:
        return [text]
    parts = []
    while len(text) > limit:
        idx = text[:limit].rfind("\n")
        if idx == -1:
            idx = text[:limit].rfind(". ")
            if idx == -1:
                idx = limit
        parts.append(text[:idx].strip())
        text = text[idx:].strip()
    if text:
        parts.append(text)
    return parts

# Получение релевантного поискового запроса
def get_safe_prompt(text: str) -> str:
    text = text.lower()
    words = re.findall(r"\b[\wёЁ]+\b", text)
    keywords = [w for w in words if w not in STOPWORDS]
    return " ".join(keywords) if keywords else "city"

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

@dp.message()
async def handle_message(message: Message):
    user_input = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name

    if any(trigger in user_input.lower() for trigger in INFO_COMMANDS):
        reply = random.choice(OWNER_REPLIES)
        await bot.send_chat_action(message.chat.id, action="typing")
        await asyncio.sleep(1.2)
        await message.answer(reply, parse_mode=ParseMode.HTML)
        return

    chat_history.setdefault(user_id, []).append({"role": "user", "parts": [user_input]})
    if len(chat_history[user_id]) > 5:
        chat_history[user_id].pop(0)

    try:
        await bot.send_chat_action(message.chat.id, action="typing")
        response = model.generate_content(chat_history[user_id])
        gemini_text = format_gemini_response(response.text)

        image_prompt = get_safe_prompt(user_input)
        image_url = await get_unsplash_image_url(image_prompt, UNSPLASH_ACCESS_KEY)

        if image_url and any(trigger in user_input.lower() for trigger in IMAGE_TRIGGERS):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(image_url) as resp:
                        if resp.status == 200:
                            photo = await resp.read()
                            file = FSInputFile(BytesIO(photo), filename="image.jpg")
                            caption = gemini_text[:950] if gemini_text else ""
                            await bot.send_photo(chat_id=message.chat.id, photo=file, caption=caption, parse_mode=ParseMode.HTML)

                            remaining_text = gemini_text[950:].strip()
                            if remaining_text:
                                for chunk in split_message(remaining_text):
                                    await asyncio.sleep(0.5)
                                    await message.answer(chunk, parse_mode=ParseMode.HTML)
                            return
            except Exception as e:
                logging.warning(f"Ошибка при отправке изображения: {e}")

        for chunk in split_message(gemini_text):
            await message.answer(chunk, parse_mode=ParseMode.HTML)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Не удаётся подключиться к облакам Vandili.", parse_mode=ParseMode.HTML)
    except ConnectionError:
        await message.answer("⚠️ Нет подключения к интернету. Попробуйте позже.", parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        error_text = format_gemini_response(str(e))
        await message.answer(f"❌ Ошибка запроса: {error_text}", parse_mode=ParseMode.HTML)

# Запуск
async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
