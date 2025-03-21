import logging
import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import google.generativeai as genai

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-latest")

from aiogram.client.default import DefaultBotProperties
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="MarkdownV2"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(f"Привет, {message.from_user.full_name}! 🤖 Я AI от Vandili. Спрашивай что угодно!")

def is_bot_mentioned(message: types.Message):
    triggers = ["vai", "вай", "VAI", "Vai", "Вай"]
    text = message.text.lower()
    return (
        any(trigger in text for trigger in triggers)
        or (message.reply_to_message and message.reply_to_message.from_user.id == bot.id)
    )

# Проверяем содержит ли ответ код
def format_response(text: str) -> str:
    if "```" in text:
        return text
    else:
        # экранируем специальные символы для MarkdownV2
        for ch in "_*[]()~`>#+-=|{}.!" :
            text = text.replace(ch, "\\"+ch)
        return text

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
        error_msg = f"Ошибка запроса: {str(e)}"
        await message.answer(format_response(error_msg), parse_mode="MarkdownV2")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())