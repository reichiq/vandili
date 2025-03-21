import asyncio
import google.generativeai as genai
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

# 🔑 Вставь API-ключ от Google Gemini
GEMINI_API_KEY = "AIzaSyAYEQ4CYf9w98CViYyFsnNKu6WK1Eqtfp4"

# Настроим Google Gemini API
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro")  # Можно поменять на "gemini-1.5-pro"

# Настроим Telegram-бота
TELEGRAM_BOT_TOKEN = "7561074770:AAFCd1Gemz_g-mB-0FU0VXJa53BM3Lq41wA"
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# Логирование
logging.basicConfig(level=logging.INFO)

# 📌 Команда /start
@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer(f"Привет, {message.from_user.full_name}! 🤖 Я работаю на Google Gemini AI. Спрашивай что угодно!")

# 📌 AI отвечает на все сообщения
@dp.message()
async def chat_with_gemini(message: Message):
    try:
        response = model.generate_content([message.text])
        await message.answer(response.text)
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await message.answer("Что-то пошло не так, попробуй позже!")

# 📌 Запуск бота
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())