import logging
import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import google.generativeai as genai
import re

# Получаем токены из окружения
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Настройка Gemini API
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-latest")

# Настройка бота
from aiogram.client.default import DefaultBotProperties
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="MarkdownV2"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# Команда /start
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(f"Привет, {message.from_user.full_name}! 🤖 Я AI от Vandili. Спрашивай что угодно!")

# Проверка обращения к боту
def is_bot_mentioned(message: types.Message):
    triggers = ["vai", "вай", "VAI", "Vai", "Вай"]
    text = message.text.lower()
    return (
        any(trigger in text for trigger in triggers)
        or (message.reply_to_message and message.reply_to_message.from_user.id == bot.id)
    )

# Функция для экранирования символов MarkdownV2
def escape_markdown(text: str) -> str:
    special_chars = "_*[]()~`>#+-=|{}.!"
    for ch in special_chars:
        text = text.replace(ch, f"\\{ch}")
    return text

# Функция для обработки ответа от Gemini
def format_response(text: str) -> str:
    code_blocks = re.findall(r"```([\s\S]+?)```", text)  # Ищем блоки кода
    if code_blocks:
        for code in code_blocks:
            formatted_code = f"```{code}```"
            text = text.replace(f"```{code}```", formatted_code)
    else:
        text = escape_markdown(text)
    
    return text

# Основная обработка сообщений
@dp.message()
async def chat_with_gemini(message: types.Message):
    if message.chat.type != 'private' and not is_bot_mentioned(message):
        return

    user_text = message.text
    for trigger in ["vai", "вай", "VAI", "Vai", "Вай"]:
        user_text = user_text.replace(trigger, "").strip()

    try:
        response = model.generate_content(user_text)
        formatted_text = format_response(response.text)
        await message.answer(formatted_text, parse_mode="MarkdownV2")
    except Exception as e:
        await message.answer(f"Ошибка запроса: {escape_markdown(str(e))}", parse_mode="MarkdownV2")

# Запуск бота
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())