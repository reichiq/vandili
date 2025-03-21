import logging
import os
import asyncio
import re
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import google.generativeai as genai

# Получаем токены из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Настройка Gemini API
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-latest")

# Инициализация бота и диспетчера
from aiogram.client.default import DefaultBotProperties
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="MarkdownV2"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# Список триггерных слов для упоминания бота
TRIGGERS = ["vai", "вай", "VAI", "Vai", "Вай"]

# Функция проверки упоминания бота
def is_bot_mentioned(message: types.Message):
    text = message.text.lower()
    return (
        any(trigger in text for trigger in TRIGGERS)
        or (message.reply_to_message and message.reply_to_message.from_user.id == bot.id)
    )

# Функция экранирования спецсимволов для MarkdownV2 (исправлено для формул)
def escape_markdown(text: str) -> str:
    escape_chars = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

# Команда /start
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    text = f"Привет, {message.from_user.full_name}\! 🤖\nЯ AI от Vandili\. Спрашивай что угодно\!"
    await message.answer(escape_markdown(text))

# Обработка сообщений с проверкой обращения
@dp.message()
async def chat_with_gemini(message: types.Message):
    if message.chat.type != 'private' and not is_bot_mentioned(message):
        return

    # Убираем триггерное слово из запроса
    user_text = message.text
    for trigger in TRIGGERS:
        user_text = user_text.replace(trigger, "").strip()

    try:
        response = model.generate_content(user_text)
        formatted_response = escape_markdown(response.text)

        # Если ответ содержит код, корректно форматируем его
        if "```" in formatted_response:
            formatted_response = "```\n" + formatted_response.replace("```", "") + "\n```"
        else:
            # Если не код, но есть математические выражения — экранируем цифры и знаки
            formatted_response = re.sub(r"(\d+)", r"\1", formatted_response)  # Цифры без изменений
            formatted_response = re.sub(r"([\+\-\*/=])", r"\\\1", formatted_response)  # Экранируем +, -, *, /

        await message.answer(formatted_response)
    except Exception as e:
        error_message = f"Ошибка запроса: {escape_markdown(str(e))}"
        await message.answer(error_message)

# Запуск бота
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())