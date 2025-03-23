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

# Загрузка .env
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

def format_gemini_response(text: str) -> str:
    code_blocks = {}
    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        return placeholder

    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)
    text = re.sub(r"\[.*?(фото|изображени|вставьте).*?\]", "", text, flags=re.IGNORECASE)
    text = escape(text)

    for placeholder, block in code_blocks.items():
        text = text.replace(escape(placeholder), block)

    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)

    return text.strip()

def get_safe_prompt(text: str) -> str:
    text = re.sub(r'[.,!?\-\n]', ' ', text.lower())
    text = re.sub(r"\b(расскажи|покажи|мне|про|факт|фото|изображение|прикрепи|дай|и|о|об|отправь|что|такое|интересное)\b", "", text)
    words = text.strip().split()
    return " ".join(words[:4]) if words else "random"

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

def split_text(text: str, max_length: int = 950):
    parts = []
    while len(text) > max_length:
        split_index = text[:max_length].rfind('. ')
        if split_index == -1:
            split_index = max_length
        parts.append(text[:split_index+1].strip())
        text = text[split_index+1:].strip()
    if text:
        parts.append(text)
    return parts

@dp.message()
async def handle_message(message: Message):
    user_input = message.text.strip()
    user_id = message.from_user.id

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
                            chunks = split_text(gemini_text)
                            await bot.send_photo(chat_id=message.chat.id, photo=file, caption=chunks[0], parse_mode=ParseMode.HTML)
                            for chunk in chunks[1:]:
                                await message.answer(chunk, parse_mode=ParseMode.HTML)
                            return
            except Exception as e:
                logging.warning(f"Ошибка при отправке изображения: {e}")

        await message.answer(gemini_text, parse_mode=ParseMode.HTML)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Не удаётся подключиться к облакам Vandili.", parse_mode=ParseMode.HTML)
    except ConnectionError:
        await message.answer("⚠️ Нет подключения к интернету. Попробуйте позже.", parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        error_text = format_gemini_response(str(e))
        await message.answer(f"❌ Ошибка запроса: {error_text}", parse_mode=ParseMode.HTML)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
