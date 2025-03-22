import logging
import os
import asyncio
import re
import aiohttp
import random
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
import google.generativeai as genai

# Токены из окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-latest")

# Настройка бота
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="MarkdownV2"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# --- Форматирование Gemini-текста ---
def format_gemini_response(text: str) -> str:
    special_chars = r"_[]()~>#+-=|{}.!"
    for ch in special_chars:
        text = text.replace(ch, f"\\{ch}")
    text = text.replace("**", "")
    text = re.sub(r'\s*```\w*\n', '```\n', text)
    text = re.sub(r'\n```\s*', '\n```', text)
    text = re.sub(r'```(\w+)?\n(.*?)\n```', lambda m: f"```\n{m.group(2)}\n```", text, flags=re.DOTALL)
    text = re.sub(r'(\d+\.) ', r'\n\1 ', text)
    return text

# --- Триггеры ---
TRIGGERS = ["vai", "вай", "VAI", "Vai", "Вай"]

def is_bot_mentioned(message: types.Message) -> bool:
    text = message.text.lower()
    return (
        any(trigger in text for trigger in TRIGGERS)
        or (message.reply_to_message and message.reply_to_message.from_user.id == bot.id)
    )

# --- Проверка интернета ---
async def check_internet():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://www.google.com", timeout=5) as resp:
                return resp.status == 200
    except Exception:
        return False

# --- Команда /start ---
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    text = f"Привет, {message.from_user.full_name}! 🤖 Я AI от Vandili. Спрашивай что угодно!"
    await message.answer(format_gemini_response(text), parse_mode="MarkdownV2")

# --- Вопросы о создателе ---
def is_owner_question(text: str) -> bool:
    owner_keywords = [
        "чей это бот", "кто владелец бота", "чей ии", "кому принадлежит бот",
        "кто сделал этот бот", "кто его создал", "для кого этот бот", "кому он служит",
        "кем был разработан этот бот", "кто его разрабатывал", "кто тебя создал", "кто твой создатель",
        "кем ты был создан", "кем ты разработан", "разработчик этого бота", "кто разрабатывал этот бот"
    ]
    return any(kw in text.lower() for kw in owner_keywords)

@dp.message()
async def message_router(message: types.Message):
    if message.chat.type != 'private' and not is_bot_mentioned(message):
        return

    user_text = message.text
    for trigger in TRIGGERS:
        user_text = user_text.replace(trigger, "").strip()

    if is_owner_question(user_text):
        responses = [
            "🤖 Этот бот был создан для *Vandili*.",
            "🧠 ИИ бота разработан специально для *Vandili*.",
            "💼 Владелец и создатель бота — *Vandili*.",
            "🔧 Разработан исключительно для нужд *Vandili*.",
            "👨‍💻 Всё, что я делаю — для *Vandili*!"
        ]
        await message.answer(format_gemini_response(random.choice(responses)), parse_mode="MarkdownV2")
        return

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        if not await check_internet():
            raise ConnectionError("Нет подключения к интернету")

        response = model.generate_content(user_text).text
        formatted = format_gemini_response(response)
        await message.answer(formatted, parse_mode="MarkdownV2")

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Не удаётся подключиться к облакам Vandili.", parse_mode="MarkdownV2")

    except ConnectionError:
        await message.answer("⚠️ Ошибка: Нет подключения к интернету. Проверьте соединение и попробуйте снова.", parse_mode="MarkdownV2")

    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        await message.answer(f"❌ Ошибка запроса: {format_gemini_response(str(e))}", parse_mode="MarkdownV2")

# --- Запуск ---
async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
