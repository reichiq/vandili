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

# Память для запоминания диалогов и имён
user_memory = {}

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

# Проверка упоминания бота
def is_bot_mentioned(message: types.Message):
    triggers = ["vai", "вай", "VAI", "Vai", "Вай"]
    text = message.text.lower()
    return (
        any(trigger in text for trigger in triggers) or 
        (message.reply_to_message and message.reply_to_message.from_user.id == bot.id)
    )

# Проверка соединения с интернетом
async def check_internet():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://www.google.com", timeout=5) as resp:
                return resp.status == 200
    except Exception:
        return False

# Команда /start
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    logging.info(f"Команда /start от {message.from_user.id}")
    text = f"Привет, {message.from_user.full_name}! 🤖 Я AI от Vandili. Спрашивай что угодно!"
    await message.answer(format_gemini_response(text), parse_mode="MarkdownV2")

# Проверка на вопрос о владельце или разработчике
def is_owner_question(text: str) -> bool:
    owner_keywords = [
        "чей это бот", "кто владелец бота", "чей ии", "кому принадлежит бот", 
        "кто сделал этот бот", "кто его создал", "для кого этот бот", "кому он служит",
        "кем был разработан этот бот", "кто его разрабатывал", "кто тебя создал", "кто твой создатель",
        "кем ты был создан", "кем ты разработан", "разработчик этого бота", "кто разрабатывал этот бот"
    ]
    return any(re.search(rf"\\b{re.escape(keyword)}\\b", text.lower()) for keyword in owner_keywords)

# Обработка всех сообщений
@dp.message()
async def chat_with_gemini(message: types.Message):
    logging.info(f"Получено сообщение: {message.text} от {message.from_user.id}")

    if message.chat.type != 'private':
        if not is_bot_mentioned(message) and not message.is_topic_message:
            return

    # Обработка имени
    user_id = message.from_user.id
    user_name = message.from_user.first_name or message.from_user.username

    if user_id not in user_memory:
        user_memory[user_id] = {
            "name": user_name,
            "history": []
        }

    user_memory[user_id]["history"].append(message.text)
    if len(user_memory[user_id]["history"]) > 5:
        user_memory[user_id]["history"] = user_memory[user_id]["history"][1:]

    # Ответы на вопросы о владельце
    if is_owner_question(message.text):
        responses = [
            "🤖 Этот бот был создан для *Vandili*.",
            "🛠️ Искусственный интеллект этого бота предназначен для Vandili.",
            "👑 *Vandili* — единственный владелец и создатель этого бота.",
            "🔒 Этот бот обслуживает только *Vandili*.",
            "🧠 Я создан для *Vandili*. Все вопросы к нему!",
            "💡 Разработан специально для *Vandili*, больше ни для кого!",
            "🧰 Меня разрабатывал *Vandili*, так что только он знает все мои секреты!",
            "🧬 Я создан *Vandili* и работаю исключительно для него.",
            "📢 Хочешь узнать больше о моем создателе? Обратись к *Vandili*!",
            "🛡️ *Vandili* — мой разработчик и владелец. Он знает всё обо мне!"
        ]
        await message.answer(format_gemini_response(random.choice(responses)), parse_mode="MarkdownV2")
        return

    user_text = message.text
    for trigger in ["vai", "вай", "VAI", "Vai", "Вай"]:
        user_text = user_text.replace(trigger, "").strip()

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        if not await check_internet():
            raise ConnectionError("Нет подключения к интернету")

        response = model.generate_content(user_text).text

        name = user_memory[user_id]["name"]
        if name and random.random() < 0.25:
            response = f"{name}, {response}"

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
