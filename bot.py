import logging
import os
import re
import random
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.utils.markdown import hbold
from google.generativeai import GenerativeModel
from google.generativeai.types import HarmCategory, HarmBlockThreshold

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Проверка переменных среды
if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Не установлены TELEGRAM_BOT_TOKEN или GEMINI_API_KEY")

bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

model = GenerativeModel(
    model_name="gemini-pro",
    safety_settings={HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE},
    api_key=GEMINI_API_KEY
)

# Словарь для хранения истории сообщений и имен пользователей
chat_history = {}
user_names = {}

# Проверка подключения к интернету
async def check_internet():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://google.com", timeout=5):
                return True
    except:
        return False

# Функция форматирования ответа от Gemini
def format_gemini_response(text: str) -> str:
    special_chars = r"_[]()~>#+-=|{}.!"
    for ch in special_chars:
        text = text.replace(ch, f"\\{ch}")
    text = text.replace("**", "")
    text = re.sub(r'```\\w*\\n', '```\n', text)
    text = re.sub(r'\n```', '\n```', text)
    text = re.sub(r'```(\\w+)?\n(.*?)\n```', lambda m: f"```\n{m.group(2)}\n```", text, flags=re.DOTALL)
    text = re.sub(r'(\d+\.) ', r'\n\1 ', text)
    return text

# Проверка, упомянут ли бот или ответ ли это
async def is_bot_called(message: Message) -> bool:
    if message.reply_to_message and message.reply_to_message.from_user.id == (await bot.get_me()).id:
        return True
    if (await bot.get_me()).username.lower() in message.text.lower():
        return True
    return False

# Проверка вопроса о владельце/разработчике
def is_owner_question(text: str) -> bool:
    keywords = [
        "чей это бот", "кто владелец", "кто сделал", "кто создал", "разработчик", "кем ты создан"
    ]
    return any(k in text.lower() for k in keywords)

@dp.message()
async def handle_message(message: Message):
    if not await is_bot_called(message):
        return

    user_id = message.from_user.id
    user_text = message.text
    username = message.from_user.username or message.from_user.full_name

    if is_owner_question(user_text):
        responses = [
            "🤖 Этот бот создан специально для Vandili!",
            "👨‍💻 Разработан Vandili и работает только для него!",
            "🧠 Я служу только Vandili — он мой создатель и хозяин!"
        ]
        await message.answer(random.choice(responses), parse_mode=ParseMode.MARKDOWN_V2)
        return

    if user_id not in chat_history:
        chat_history[user_id] = []
    if user_id not in user_names:
        user_names[user_id] = username

    chat_history[user_id].append({"role": "user", "parts": [user_text]})
    if len(chat_history[user_id]) > 5:
        chat_history[user_id].pop(0)

    try:
        if not await check_internet():
            raise ConnectionError("Нет подключения к интернету")

        response = model.generate_content(chat_history[user_id])
        result = format_gemini_response(response.text)

        # Добавим случайное имя, если оно есть в user_names
        if random.random() < 0.5:
            result = f"{user_names[user_id]}, {result}"

        await message.answer(result, parse_mode=ParseMode.MARKDOWN_V2)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Не удаётся подключиться к облакам Vandili.", parse_mode=ParseMode.MARKDOWN_V2)
    except ConnectionError:
        await message.answer("⚠️ Нет подключения к интернету. Попробуйте позже.", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        await message.answer(f"❌ Ошибка запроса: {format_gemini_response(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)

if __name__ == '__main__':
    import asyncio
    asyncio.run(dp.start_polling(bot))
