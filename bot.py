import logging
import os
import asyncio
import re
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import google.generativeai as genai

# Токены
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-latest")

# Настройка бота с MarkdownV2
from aiogram.client.default import DefaultBotProperties
bot = Bot(
    token=TELEGRAM_BOT_TOKEN, 
    default=DefaultBotProperties(parse_mode="MarkdownV2")
)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# Экранирование MarkdownV2
def escape_markdown_v2(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# /start
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    text = escape_markdown_v2(f"Привет, {message.from_user.full_name}! 🤖 Я AI от Vandili. Спрашивай что угодно!")
    await message.answer(text)

# Проверка обращения
def is_bot_mentioned(message: types.Message):
    triggers = ["vai", "вай", "VAI", "Vai", "Вай"]
    text = message.text.lower()
    return (
        any(trigger in text for trigger in triggers)
        or (message.reply_to_message and message.reply_to_message.from_user.id == bot.id)
    )

# Основной обработчик сообщений
@dp.message()
async def chat_with_gemini(message: types.Message):
    if message.chat.type != 'private' and not is_bot_mentioned(message):
        return

    user_text = message.text
    for trigger in ["vai", "вай", "VAI", "Vai", "Вай"]:
        user_text = user_text.replace(trigger, "").strip()

    try:
        response = model.generate_content(user_text)

        if "```" in response.text:
            parts = response.text.split("```")
            response_text = f"```{parts[1]}```"
        else:
            response_text = response.text

        response_text = escape_markdown_v2(response_text)
        await message.answer(response_text)

    except Exception as e:
        error_text = escape_markdown_v2(f"Ошибка запроса: {e}")
        await message.answer(error_text)

# Запуск бота
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())