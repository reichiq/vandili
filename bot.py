import logging
import os
import asyncio
import re
import random
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
import google.generativeai as genai

# === Конфигурация токенов ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# === Настройка Gemini ===
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-latest")

# === Инициализация бота ===
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="MarkdownV2"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# === Проверка упоминания бота или ответа ===
def is_bot_mentioned(message: types.Message) -> bool:
    triggers = ["vai", "вай", "VAI", "Vai", "Вай"]
    text = message.text.lower() if message.text else ""
    return (
        any(trigger in text for trigger in triggers) or
        (message.reply_to_message and message.reply_to_message.from_user.id == bot.id)
    )

# === Проверка интернета ===
async def check_internet():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://www.google.com", timeout=5) as resp:
                return resp.status == 200
    except Exception:
        return False

# === Форматирование Markdown ===
def format_gemini_response(text: str) -> str:
    # Экранирование спецсимволов в MarkdownV2
    def escape_markdown(text):
        special_chars = r'_[]()~>#+-=|{}.!'
        for ch in special_chars:
            text = text.replace(ch, f"\\{ch}")
        return text

    # Удаляем **жирные** обёртки
    text = text.replace("**", "")

    # Исправляем кодовые блоки
    text = re.sub(r"```(\w+)?\n", "```
", text)
    text = re.sub(r"\n```", "
```", text)
    text = re.sub(r"```(\w+)?\n(.*?)\n```", lambda m: f"```
{m.group(2)}
```", text, flags=re.DOTALL)

    # Добавляем переносы перед списками
    text = re.sub(r'(\d+\.) ', r'\n\1 ', text)

    return escape_markdown(text)

# === Ответы на вопросы о владельце/разработчике ===
def is_owner_question(text: str) -> bool:
    keywords = [
        "чей это бот", "кому принадлежит бот", "кто владелец",
        "кто тебя создал", "разработчик бота", "кем ты был создан",
        "кто тебя разрабатывал", "для кого ты создан", "чей ии"
    ]
    return any(keyword in text.lower() for keyword in keywords)

@dp.message()
async def handle_message(message: types.Message):
    # Только если в ЛС или по упоминанию / ответу
    if message.chat.type != 'private' and not is_bot_mentioned(message):
        return

    user_text = message.text or ""

    # Убираем триггеры
    for trigger in ["vai", "вай", "VAI", "Vai", "Вай"]:
        user_text = user_text.replace(trigger, "").strip()

    # Ответы на вопросы о владельце
    if is_owner_question(user_text):
        replies = [
            "Этот бот был создан для Vandili 🤖",
            "Мой разработчик — Vandili 👨‍💻",
            "Я принадлежу Vandili и служу только ему ✨",
            "Vandili — мой создатель и вдохновитель 🔥",
            "Разработан исключительно для Vandili 🚀"
        ]
        await message.answer(format_gemini_response(random.choice(replies)), parse_mode="MarkdownV2")
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
        await message.answer("⚠️ Ошибка: Нет подключения к интернету.", parse_mode="MarkdownV2")
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await message.answer(f"❌ Ошибка запроса: {format_gemini_response(str(e))}", parse_mode="MarkdownV2")

# === Стартовая команда ===
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    text = f"Привет, {message.from_user.full_name}! 🤖 Я бот Vandili. Спрашивай что угодно."
    await message.answer(format_gemini_response(text), parse_mode="MarkdownV2")

# === Запуск ===
async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
