import logging
import re
import random
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from google.generativeai import GenerativeModel
from config import TELEGRAM_BOT_TOKEN, GEMINI_API_KEY
import google.generativeai as genai
import asyncio

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = GenerativeModel("gemini-pro")

# Инициализация бота
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="MarkdownV2"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# Словарь для хранения имен пользователей
user_names = {}
user_histories = {}
MAX_HISTORY = 5

# Проверка соединения
async def check_internet():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://www.google.com", timeout=5):
                return True
    except:
        return False

# Форматирование MarkdownV2
def format_gemini_response(text: str) -> str:
    special_chars = r"_[]()~>#+-=|{}.!"
    for ch in special_chars:
        text = text.replace(ch, f"\\{ch}")

    text = text.replace("**", "")
    text = re.sub(r'```(\w+)?\n', '```\n', text)
    text = re.sub(r'\n```', '\n```', text)
    text = re.sub(r'```(\w+)?\n(.*?)\n```', lambda m: f"```\n{m.group(2)}\n```", text, flags=re.DOTALL)
    text = re.sub(r'(\d+\.) ', r'\n\1 ', text)

    return text

# Проверка вопроса о владельце
owner_keywords = [
    "чей это бот", "кто владелец бота", "чей ии", "кому принадлежит бот",
    "кто сделал этот бот", "кто его создал", "для кого этот бот", "кому он служит",
    "кем был разработан этот бот", "кто его разрабатывал", "кто тебя создал", "кто твой создатель",
    "кем ты был создан", "кем ты разработан", "разработчик этого бота", "кто разрабатывал этот бот"
]
def is_owner_question(text: str) -> bool:
    return any(re.search(rf"\\b{re.escape(keyword)}\\b", text.lower()) for keyword in owner_keywords)

# Основной обработчик
@dp.message()
async def chat_with_gemini(message: types.Message):
    # Игнорировать, если бот не упомянут и не reply
    if not (message.text and (message.reply_to_message and message.reply_to_message.from_user.id == (await bot.me()).id) or f"@{(await bot.me()).username}" in message.text):
        return

    user_id = message.from_user.id
    user_text = message.text.replace(f"@{(await bot.me()).username}", "").strip()

    # Обработка вопроса о владельце
    if is_owner_question(user_text):
        replies = [
            "Этот бот был создан для Vandili 🧠",
            "Разработан исключительно для Vandili 🤖",
            "Создатель — Vandili. Все вопросы к нему 👨‍💻",
            "Vandili — мой разработчик и владелец 💡",
            "Я служу только Vandili и никому больше ✨"
        ]
        await message.answer(format_gemini_response(random.choice(replies)), parse_mode="MarkdownV2")
        return

    # Обработка имени
    if re.search(r'меня зовут|зовут меня|мо[ея] имя', user_text.lower()):
        match = re.search(r'(?:меня зовут|зовут меня|мо[ея] имя)\s+([\wА-Яа-яёЁ]+)', user_text, re.IGNORECASE)
        if match:
            user_names[user_id] = match.group(1)
            await message.answer(f"Приятно познакомиться, {match.group(1)}! 😊", parse_mode="MarkdownV2")
            return

    # Подготовка истории
    if user_id not in user_histories:
        user_histories[user_id] = []
    user_histories[user_id].append({"role": "user", "parts": [user_text]})
    user_histories[user_id] = user_histories[user_id][-MAX_HISTORY:]

    try:
        if not await check_internet():
            raise ConnectionError("Нет подключения к интернету")

        response = await model.generate_content_async(user_histories[user_id])
        reply_text = response.text

        # Добавляем имя пользователя, если оно есть
        if user_id in user_names and random.random() < 0.5:
            reply_text = f"{user_names[user_id]}, {reply_text}"

        user_histories[user_id].append({"role": "model", "parts": [reply_text]})
        user_histories[user_id] = user_histories[user_id][-MAX_HISTORY:]

        await message.answer(format_gemini_response(reply_text), parse_mode="MarkdownV2")

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Не удаётся подключиться к облакам Vandili.", parse_mode="MarkdownV2")
    except ConnectionError:
        await message.answer("⚠️ Ошибка: Нет подключения к интернету. Проверьте соединение и попробуйте снова.", parse_mode="MarkdownV2")
    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        await message.answer(f"❌ Ошибка запроса: {format_gemini_response(str(e))}", parse_mode="MarkdownV2")


if __name__ == '__main__':
    import asyncio
    asyncio.run(dp.start_polling(bot))
