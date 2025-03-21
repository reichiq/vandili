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

# Инициализация бота и диспетчера
from aiogram.client.default import DefaultBotProperties
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="MarkdownV2"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# Хранилище имён пользователей
user_names = {}

# Функция для форматирования ответа
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

# Функция проверки упоминания бота
def is_bot_mentioned(message: types.Message):
    triggers = ["vai", "вай", "VAI", "Vai", "Вай"]
    text = message.text.lower()
    return any(trigger in text for trigger in triggers) or (message.reply_to_message and message.reply_to_message.from_user.id == bot.id)

# Функция для проверки соединения с интернетом
async def check_internet():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://www.google.com", timeout=5) as resp:
                return resp.status == 200
    except Exception:
        return False

# Функция для определения вопросов о пользователе
def is_user_name_question(text: str) -> bool:
    name_keywords = ["как меня зовут", "ты знаешь мое имя", "ты помнишь кто я"]
    return any(re.search(rf"\\b{re.escape(keyword)}\\b", text.lower()) for keyword in name_keywords)

# Функция запоминания имени пользователя
def remember_user_name(user_id: int, user_name: str):
    user_names[user_id] = user_name

# Команда /start
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    logging.info(f"Команда /start от {message.from_user.id}")
    text = f"Привет, {message.from_user.full_name}! 🤖 Я AI от Vandili. Спрашивай что угодно!"
    remember_user_name(message.from_user.id, message.from_user.full_name)
    await message.answer(format_gemini_response(text), parse_mode="MarkdownV2")

# Обработчик вопросов о пользователе
@dp.message()
async def handle_user_name_question(message: types.Message):
    user_id = message.from_user.id
    if is_user_name_question(message.text):
        if user_id in user_names:
            await message.answer(f"Ты {user_names[user_id]}, как же я мог забыть? 😊", parse_mode="MarkdownV2")
        else:
            await message.answer("Я пока не знаю, как тебя зовут. Можешь сказать мне своё имя?", parse_mode="MarkdownV2")
        return
    await chat_with_gemini(message)

# Обработчик сообщений в чат с Gemini
@dp.message()
async def chat_with_gemini(message: types.Message):
    logging.info(f"Получено сообщение: {message.text} от {message.from_user.id}")
    if message.chat.type != 'private' and not is_bot_mentioned(message):
        return
    user_text = message.text
    for trigger in ["vai", "вай", "VAI", "Vai", "Вай"]:
        user_text = user_text.replace(trigger, "").strip()
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        if not await check_internet():
            raise ConnectionError("Нет подключения к интернету")
        response = model.generate_content(user_text).text
        formatted_response = format_gemini_response(response)
        await message.answer(formatted_response, parse_mode="MarkdownV2")
    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Не удаётся подключиться к облакам Vandili.", parse_mode="MarkdownV2")
    except ConnectionError:
        await message.answer("⚠️ Ошибка: Нет подключения к интернету. Проверьте соединение и попробуйте снова.", parse_mode="MarkdownV2")
    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        await message.answer(f"❌ Ошибка запроса: {format_gemini_response(str(e))}", parse_mode="MarkdownV2")

# Запуск
async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
