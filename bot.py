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


# Функция для форматирования ответа от Gemini
def format_gemini_response(text: str) -> str:
    """
    Форматирует текст от Gemini для корректного отображения в Telegram (MarkdownV2).
    """
    # Экранируем только критические символы, но не Markdown-разметку
    special_chars = r"_[]()~`>#+-=|{}.!"
    for ch in special_chars:
        text = text.replace(ch, f"\\{ch}")

    # Обрабатываем заголовки (### -> ***) и (## -> **)
    text = re.sub(r'### (.*?)\n', r'***\1***\n', text)
    text = re.sub(r'## (.*?)\n', r'**\1**\n', text)

    # Жирный текст (делаем так, чтобы **работало правильно**)
    text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)

    # Исправляем кодовые блоки (```python ... ```)
    text = re.sub(r'```(\w+)?\n(.*?)\n```', r'```\1\n\2\n```', text, flags=re.DOTALL)

    return text


# Проверка, упомянули ли бота
def is_bot_mentioned(message: types.Message):
    triggers = ["vai", "вай", "VAI", "Vai", "Вай"]
    text = message.text.lower()
    return (
        any(trigger in text for trigger in triggers) or 
        (message.reply_to_message and message.reply_to_message.from_user.id == bot.id)
    )


# Команда /start
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    text = f"Привет, {message.from_user.full_name}! 🤖 Я AI от Vandili. Спрашивай что угодно!"
    await message.answer(format_gemini_response(text), parse_mode="MarkdownV2")


# Обработка текстовых сообщений и запрос в Gemini
@dp.message()
async def chat_with_gemini(message: types.Message):
    if message.chat.type != 'private' and not is_bot_mentioned(message):
        return

    # Убираем триггеры из текста перед отправкой запроса
    user_text = message.text
    for trigger in ["vai", "вай", "VAI", "Vai", "Вай"]:
        user_text = user_text.replace(trigger, "").strip()

    try:
        response = model.generate_content(user_text).text

        # Форматируем ответ от Gemini
        formatted_response = format_gemini_response(response)

        # Отправляем отформатированный ответ
        await message.answer(formatted_response, parse_mode="MarkdownV2")
    
    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        await message.answer(f"Ошибка запроса: `{format_gemini_response(str(e))}`", parse_mode="MarkdownV2")


# Запуск
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.info("Бот запущен")
    asyncio.run(main())