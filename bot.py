import logging
import os
import asyncio
import re
import aiohttp
import random
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import google.generativeai as genai

# Получаем токены из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Настройка Gemini API
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-latest")

# Инициализация бота
from aiogram.client.default import DefaultBotProperties
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="MarkdownV2"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# Словарь для хранения имён пользователей
user_names = {}

# Функция для проверки, содержит ли сообщение вопрос о владельце
def is_owner_question(text: str) -> bool:
    owner_keywords = [
        "чей это бот", "кто владелец бота", "чей ии", "кому принадлежит бот",
        "кто сделал этот бот", "кто его создал", "для кого этот бот",
        "кем был разработан этот бот", "кто его разрабатывал",
        "разработчик этого бота", "кто твой создатель", "кем ты был создан",
        "кто твой разраб", "кто тобой управляет", "кто твой хозяин"
    ]
    return any(re.search(rf"\b{re.escape(keyword)}\b", text.lower()) for keyword in owner_keywords)

# Функция для запоминания имени
def extract_name(text: str):
    match = re.search(r"(зови меня|меня зовут|можешь называть меня) (\w+)", text.lower())
    return match.group(2).capitalize() if match else None

# Функция форматирования текста
def format_gemini_response(text: str) -> str:
    special_chars = r"_[]()~>#+-=|{}.!?"
    for ch in special_chars:
        text = text.replace(ch, f"\\{ch}")
    return text

# Команда /start
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    text = f"Привет, {message.from_user.full_name}! 🤖 Я AI от Vandili. Спрашивай что угодно!"
    await message.answer(format_gemini_response(text), parse_mode="MarkdownV2")

# Обработка сообщений
@dp.message()
async def chat_with_gemini(message: types.Message):
    text = message.text.strip().lower()
    user_id = message.from_user.id

    # Проверяем, назвали ли пользователи своё имя
    name = extract_name(text)
    if name:
        user_names[user_id] = name
        await message.answer(f"Рад познакомиться, {name}! 😊", parse_mode="MarkdownV2")
        return

    # Если бот не знает имя пользователя, пытаемся его запомнить
    user_name = user_names.get(user_id, None)

    # Проверка на вопросы про владельца
    if is_owner_question(text):
        responses = [
            "Этот бот был создан для Vandili. 😎",
            "Искусственный интеллект этого бота принадлежит Vandili! 🔥",
            "Vandili — мой разработчик и хозяин. 🤖",
            "Меня создал Vandili, я работаю только для него! 🚀",
            "Я бот Vandili, и это всё, что вам нужно знать! 😉"
        ]
        await message.answer(format_gemini_response(random.choice(responses)), parse_mode="MarkdownV2")
        return

    # Ответ от Gemini
    await bot.send_chat_action(message.chat.id, "typing")
    
    try:
        response = model.generate_content(text).text

        # Если бот знает имя пользователя, может обращаться по имени
        if user_name:
            response = response.replace("Привет!", f"Привет, {user_name}!")

        formatted_response = format_gemini_response(response)
        await message.answer(formatted_response, parse_mode="MarkdownV2")

    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        await message.answer(f"❌ Ошибка: `{format_gemini_response(str(e))}`", parse_mode="MarkdownV2")

# Запуск
async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
