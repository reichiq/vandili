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

# ⚡ Память диалогов (словарь)
user_memory = {}

# 🛠 Функция для форматирования текста
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

# ⚡ Проверка, спрашивает ли пользователь про создателя
def is_owner_question(text: str) -> bool:
    owner_keywords = [
        "чей это бот", "кто владелец бота", "чей ии", "кому принадлежит бот",
        "кто сделал этот бот", "кто его создал", "для кого этот бот", "кому он служит",
        "кем был разработан этот бот", "кто его разрабатывал", "кто тебя создал", "кто твой создатель",
        "кем ты был создан", "кем ты разработан", "разработчик этого бота", "кто разрабатывал этот бот"
    ]
    return any(re.search(rf"\b{re.escape(keyword)}\b", text.lower()) for keyword in owner_keywords)

# 🏆 Ответы на вопросы про создателя
@dp.message()
async def handle_owner_question(message: types.Message):
    if is_owner_question(message.text):
        responses = [
            "🤖 Этот бот был создан специально для Vandili.",
            "🔧 Vandili — мой создатель и разработчик!",
            "⚙️ Я создан для Vandili и только для него!",
            "📌 Vandili — мой разработчик, я служу только ему!",
            "🛠️ Vandili мой единственный создатель!",
            "🤖 Vandili знает всё обо мне, он мой хозяин!",
            "💡 Моё существование — заслуга Vandili!",
            "🧠 Vandili меня тренировал, я служу только ему!"
        ]
        await message.answer(format_gemini_response(random.choice(responses)), parse_mode="MarkdownV2")
        return

    # Если вопрос не про создателя, передаём в чат с Gemini
    await chat_with_gemini(message)

# 📡 Проверка соединения с интернетом
async def check_internet():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://www.google.com", timeout=5) as resp:
                return resp.status == 200
    except Exception:
        return False

# 🎯 Команда /start
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    logging.info(f"Команда /start от {message.from_user.id}")
    text = f"Привет, {message.from_user.full_name}! 🤖 Я AI от Vandili. Спрашивай что угодно!"
    await message.answer(format_gemini_response(text), parse_mode="MarkdownV2")

# 💬 Запоминание сообщений и ответ в контексте
@dp.message()
async def chat_with_gemini(message: types.Message):
    logging.info(f"Получено сообщение: {message.text} от {message.from_user.id}")

    # Если это не личные сообщения и бот не упомянут — игнорируем
    if message.chat.type != 'private' and not is_bot_mentioned(message):
        return

    user_text = message.text.strip()
    user_id = message.from_user.id
    user_name = message.from_user.full_name or message.from_user.username

    # Убираем триггеры упоминания бота из текста
    for trigger in ["vai", "вай", "VAI", "Vai", "Вай"]:
        user_text = user_text.replace(trigger, "").strip()

    # Если сообщение — просто "Привет", не запоминаем контекст
    if user_text.lower() in ["привет", "хай", "hello", "здарова", "алло"]:
        greeting_responses = [
            f"Привет, {user_name}! 😊 Как дела?",
            f"Здравствуй, {user_name}! 🚀",
            f"Хэй, {user_name}! Как твои дела? 🔥",
        ]
        await message.answer(format_gemini_response(random.choice(greeting_responses)), parse_mode="MarkdownV2")
        return

    # Включаем "печатает..."
    await bot.send_chat_action(message.chat.id, "typing")

    try:
        # Проверяем подключение перед запросом
        if not await check_internet():
            raise ConnectionError("Нет подключения к интернету")

        # Проверяем, есть ли предыдущие сообщения от пользователя
        if user_id in user_memory:
            past_messages = user_memory[user_id]
        else:
            past_messages = []

        # Добавляем новое сообщение в историю (ограничиваем 5 сообщениями)
        past_messages.append(user_text)
        past_messages = past_messages[-5:]  # Храним не более 5 последних сообщений

        # Формируем запрос к Gemini
        full_conversation = "\n".join(past_messages)
        response = model.generate_content(full_conversation).text

        # Сохраняем обновлённую историю диалога
        user_memory[user_id] = past_messages

        # Форматируем ответ
        formatted_response = format_gemini_response(response)

        # Добавляем имя юзера только в первом ответе, а не каждый раз
        if len(past_messages) == 1:
            formatted_response = f"{user_name}, {formatted_response}"

        await message.answer(formatted_response, parse_mode="MarkdownV2")

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Не удаётся подключиться к облакам Vandili.", parse_mode="MarkdownV2")

    except ConnectionError:
        await message.answer("⚠️ Ошибка: Нет подключения к интернету. Проверьте соединение и попробуйте снова.", parse_mode="MarkdownV2")

    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        await message.answer(f"❌ Ошибка запроса: {format_gemini_response(str(e))}", parse_mode="MarkdownV2")

# 🚀 Запуск бота
async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
