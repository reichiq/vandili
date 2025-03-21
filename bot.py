import logging
import os
import re
import random
import aiohttp
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message
from html import escape

# Получаем токены из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Проверка переменных среды
if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Не установлены TELEGRAM_BOT_TOKEN или GEMINI_API_KEY")

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Инициализация модели
model = genai.GenerativeModel(model_name="models/gemini-1.5-pro-latest")

# Инициализация бота с HTML-разметкой
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO, filename="/home/khan_770977/vandili/bot.log", filemode="a", format="%(asctime)s - %(levelname)s - %(message)s")

# История сообщений
chat_history = {}
user_names = {}

# Проверка интернета
async def check_internet():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://google.com", timeout=5):
                return True
    except:
        return False

# Форматирование текста для Telegram HTML
def format_gemini_response(text: str) -> str:
    code_blocks = {}

    # Вырезаем блоки кода
    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        return placeholder

    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)

    # Экранируем остальной текст
    text = escape(text)

    # Возвращаем кодовые блоки
    for placeholder, block in code_blocks.items():
        text = text.replace(escape(placeholder), block)

    # Применяем HTML-теги форматирования
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)

    return text

# Проверка, вызван ли бот
async def is_bot_called(message: Message) -> bool:
    if message.chat.type == "private":
        return True
    if message.reply_to_message and message.reply_to_message.from_user.id == (await bot.get_me()).id:
        return True
    bot_usernames = [(await bot.get_me()).username.lower(), "вай", "vai", "вай бот", "вайбот", "vai bot", "vaibot"]
    return any(name in message.text.lower() for name in bot_usernames)

# Распознавание вопроса о владельце
def is_owner_question(text: str) -> bool:
    keywords = [
        "чей это бот", "кто владелец", "кто сделал", "кто создал", "разработчик", "кем ты создан",
        "кто твой создатель", "кто твой разработчик", "кто хозяин", "кто тебя сделал", "кем был создан"
    ]
    return any(k in text.lower() for k in keywords)

# Обработка входящих сообщений
@dp.message()
async def handle_message(message: Message):
    if not await is_bot_called(message):
        return

    user_id = message.from_user.id
    user_text = message.text
    username = message.from_user.username or message.from_user.full_name

    if is_owner_question(user_text):
        responses = [
            "🤖 Этот бот был создан лично Vandili!",
            "👨‍💻 Разработан Vandili и работает исключительно для него!",
            "🧐 Я служу только Vandili — он мой создатель и хозяин!",
            "💡 Моим автором является Vandili, и я создан помогать именно ему!",
            "🛠️ Меня написал Vandili. Все вопросы — к нему!",
            "📡 Создан и запрограммирован Vandili."
        ]
        await message.answer(format_gemini_response(random.choice(responses)), parse_mode=ParseMode.HTML)
        return

    chat_history.setdefault(user_id, []).append({"role": "user", "parts": [user_text]})
    if len(chat_history[user_id]) > 5:
        chat_history[user_id].pop(0)

    try:
        if not await check_internet():
            raise ConnectionError("Нет подключения к интернету")

        response = model.generate_content(chat_history[user_id])
        result = format_gemini_response(response.text)

        if random.random() < 0.3 and username:
            result = f"@{username}, {result}"

        await message.answer(result, parse_mode=ParseMode.HTML)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Не удаётся подключиться к облакам Vandili.", parse_mode=ParseMode.HTML)
    except ConnectionError:
        await message.answer("⚠️ Нет подключения к интернету. Попробуйте позже.", parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        error_text = format_gemini_response(str(e))
        await message.answer(f"❌ Ошибка запроса: {error_text}", parse_mode=ParseMode.HTML)

# Запуск
if __name__ == '__main__':
    import asyncio
    asyncio.run(dp.start_polling(bot))
