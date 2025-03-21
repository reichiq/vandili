import asyncio
import google.generativeai as genai
import logging
import os
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

# API-ключи из Railway Variables
TELEGRAM_BOT_TOKEN = os.getenv("7561074770:AAFCd1Gemz_g-mB-0FU0VXJa53BM3Lq41wA")
GEMINI_API_KEY = os.getenv("AIzaSyAYEQ4CYf9w98CViYyFsnNKu6WK1Eqtfp4")

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro")

# Бот
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer(f"Привет, {message.from_user.full_name}! 🤖 Я AI от Vandili. Спрашивай!")

@dp.message()
async def chat_with_gemini(message: Message):
    try:
        response = model.generate_content([message.text])
        await message.answer(response.text)
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await message.answer("Что-то не так, попробуй позже!")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())