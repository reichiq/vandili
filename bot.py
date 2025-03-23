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
    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK__"
        return placeholder

    # Упростим, чтобы не путаться
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)
    text = re.sub(r"\[.*?(фото|изображени|вставьте).*?\]", "", text, flags=re.IGNORECASE)
    text = escape(text)
    # Markdown → HTML
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)
    return text.strip()

def get_safe_prompt(text: str) -> str:
    text = re.sub(r'[.,!?\-\n]', ' ', text.lower())
    text = re.sub(r"\b(расскажи|покажи|мне|про|факт|фото|изображение|прикрепи|дай|и|о|об|отправь|что|такое|интересное)\b", "", text)
    words = text.strip().split()
    if not words:
        return "random"
    # Возьмем до 3 слов
    return " ".join(words[:3])

async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    logging.info(f"[UNSPLASH] Запрос по prompt: '{prompt}'")
    url = f"https://api.unsplash.com/photos/random?query={prompt}&client_id={access_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                logging.info(f"[UNSPLASH] Статус ответа: {response.status}")
                if response.status == 200:
                    data = await response.json()
                    logging.info(f"[UNSPLASH] Ответ JSON: {data}")
                    if 'urls' in data and 'regular' in data['urls']:
                        return data['urls']['regular']
                    else:
                        logging.warning("[UNSPLASH] Нет поля 'urls' или 'regular'")
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

    logging.info(f"[BOT] Получено сообщение: '{user_input}'")
    logging.info(f"[BOT] Пользователь ID: {user_id}")

    # Проверка — инфо-команды
    if any(trigger in user_input.lower() for trigger in INFO_COMMANDS):
        reply = random.choice(OWNER_REPLIES)
        logging.info("[BOT] Сработала инфо-команда => ответ про создателя Vandili")
        await message.answer(reply, parse_mode=ParseMode.HTML)
        return

    # Добавляем сообщение в чат-историю
    chat_history.setdefault(user_id, []).append({"role": "user", "parts": [user_input]})
    if len(chat_history[user_id]) > 5:
        chat_history[user_id].pop(0)

    # Печатает...
    await bot.send_chat_action(message.chat.id, action="typing")

    # Генерация от Gemini
    response = model.generate_content(chat_history[user_id])
    gemini_text = format_gemini_response(response.text)

    logging.info(f"[GEMINI] Итоговый текст (после format): '{gemini_text[:200]}...'")

    # Prompt для Unsplash
    image_prompt = get_safe_prompt(user_input)
    logging.info(f"[BOT] Итоговый 'image_prompt': '{image_prompt}'")

    # Получение URL
    image_url = await get_unsplash_image_url(image_prompt, UNSPLASH_ACCESS_KEY)
    logging.info(f"[BOT] image_url: {image_url}")

    # Есть ли триггер для фото?
    triggered = any(trigger in user_input.lower() for trigger in IMAGE_TRIGGERS)
    logging.info(f"[BOT] triggered (фото)?: {triggered}")

    # Если и URL, и триггер => отправляем фото
    if image_url and triggered:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if resp.status == 200:
                        logging.info("[BOT] Успешно скачал фото, отправляю в чат...")
                        photo = await resp.read()
                        file = FSInputFile(BytesIO(photo), filename="image.jpg")
                        chunks = split_text(gemini_text)
                        # Отправляем первую часть как caption
                        await bot.send_photo(
                            chat_id=message.chat.id,
                            photo=file,
                            caption=chunks[0] if chunks else "...",
                            parse_mode=ParseMode.HTML
                        )
                        # Остальные части — отдельными сообщениями
                        for chunk in chunks[1:]:
                            await message.answer(chunk, parse_mode=ParseMode.HTML)
                        return
                    else:
                        logging.warning(f"[BOT] resp.status != 200 ({resp.status}) => не отправляю фото")
        except Exception as e:
            logging.warning(f"Ошибка при отправке изображения: {e}")

    # Иначе — просто текст
    logging.info("[BOT] Отправляю текст без фото...")
    for chunk in split_text(gemini_text):
        await message.answer(chunk, parse_mode=ParseMode.HTML)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
