import logging
import aiohttp
import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from dotenv import load_dotenv
from openai import OpenAI
from urllib.parse import quote

# Загружаем ключи из .env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")

# Проверка переменных
if not TELEGRAM_TOKEN or not GEMINI_API_KEY or not UNSPLASH_ACCESS_KEY:
    raise ValueError("Один или несколько API ключей отсутствуют в .env")

# Настройка логов
logging.basicConfig(level=logging.INFO)

# Инициализация бота
bot = Bot(token=TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# ===== Gemini client =====
client = OpenAI(api_key=GEMINI_API_KEY, base_url="https://generativelanguage.googleapis.com/v1beta/models")

async def ask_gemini(prompt: str) -> str:
    try:
        response = await client.text.generate(
            prompt=prompt,
            temperature=0.7,
            max_tokens=500
        )
        return response.text.strip()
    except Exception as e:
        logging.error(f"Gemini error: {e}")
        return "Произошла ошибка при обработке запроса."

# ===== Unsplash search =====
async def fetch_image(query: str) -> str | None:
    url = f"https://api.unsplash.com/photos/random?query={quote(query)}&client_id={UNSPLASH_ACCESS_KEY}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["urls"]["regular"]
    except Exception as e:
        logging.error(f"Ошибка при получении изображения: {e}")
    return None

# ===== Команда start =====
@dp.message(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я VAI — бот с интеллектом Gemini и изображениями из Unsplash. Просто напиши что угодно!")

# ===== Обработка всех сообщений =====
@dp.message()
async def handle_message(message: types.Message):
    user_input = message.text.strip().lower()
    await message.chat.do("upload_photo")

    # Если запрос содержит "покажи", "изображение", "фото", ищем картинку
    keywords = ["покажи", "изображение", "фото", "картинку", "арт"]
    needs_image = any(word in user_input for word in keywords)

    img_url = await fetch_image(user_input) if needs_image else None
    gemini_reply = await ask_gemini(user_input)

    # Сжимаем подпись, чтобы Telegram не выдал "caption too long"
    caption_text = gemini_reply[:1020] + "..." if len(gemini_reply) > 1020 else gemini_reply

    if img_url:
        try:
            await bot.send_photo(
                chat_id=message.chat.id,
                photo=img_url,
                caption=caption_text,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.warning(f"❗ Ошибка отправки фото: {e}")
            # если что-то не так — просто отправим текст
            await message.answer(caption_text)
    else:
        await message.answer(gemini_reply)

# ===== Запуск бота =====
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
