import logging
import os
import re
import random
import aiohttp
from html import escape
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties

# 🔐 Токены из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY or not UNSPLASH_ACCESS_KEY:
    raise ValueError("Не установлены необходимые переменные окружения")

# 🔧 Логгирование и конфигурация Gemini
logging.basicConfig(level=logging.INFO)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-1.5-pro-latest")

# 🤖 Бот
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
chat_history = {}

# ✅ Проверка подключения
async def check_internet():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://www.google.com", timeout=5):
                return True
    except:
        return False

# ✨ HTML-форматирование
def format_gemini_response(text: str) -> str:
    code_blocks = {}

    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        return placeholder

    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)
    text = escape(text)
    for placeholder, block in code_blocks.items():
        text = text.replace(escape(placeholder), block)

    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    return text

# 🔍 Поиск фото на Unsplash
async def search_unsplash_image(query: str) -> str | None:
    url = "https://api.unsplash.com/search/photos"
    params = {
        "query": query,
        "per_page": 1,
        "client_id": UNSPLASH_ACCESS_KEY
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            results = data.get("results")
            if results:
                return results[0]["urls"]["regular"]
            return None

# 🧠 Вызван ли бот?
async def is_bot_called(message: Message) -> bool:
    if message.chat.type == "private":
        return True
    if message.reply_to_message and message.reply_to_message.from_user.id == (await bot.get_me()).id:
        return True
    names = [(await bot.get_me()).username.lower(), "вай", "vai", "вай бот", "vai bot", "vaibot"]
    return any(name in message.text.lower() for name in names)

# 👤 Узнают ли про автора?
def is_owner_question(text: str) -> bool:
    return any(k in text.lower() for k in [
        "чей это бот", "кто владелец", "кто сделал", "кто создал", "разработчик",
        "кем ты создан", "кто твой создатель", "кто твой разработчик", "кто хозяин"
    ])

# 📩 Обработка сообщений
@dp.message()
async def handle_message(message: Message):
    if not await is_bot_called(message):
        return

    if message.message_id in getattr(bot, "_handled_messages", set()):
        return
    bot._handled_messages = getattr(bot, "_handled_messages", set())
    bot._handled_messages.add(message.message_id)

    user_id = message.from_user.id
    user_text = message.text.strip()
    username = message.from_user.username or message.from_user.full_name

    # 🔧 Ответ на вопрос о владельце
    if is_owner_question(user_text):
        answer = random.choice([
            "🤖 Этот бот был создан лично Vandili!",
            "👨‍💻 Разработан Vandili и работает исключительно для него!",
            "💡 Моим автором является Vandili, и я создан помогать именно ему!"
        ])
        await message.answer(format_gemini_response(answer))
        return

    # 🖼️ Обработка запросов на изображение + рассказ
    if any(k in user_text.lower() for k in ["арт", "фото", "картинку", "изображение"]) or (
        "покажи" in user_text.lower() and any(x in user_text.lower() for x in ["история", "расскажи", "про", "немного"])
    ):
        try:
            prompt = (
                f"Пользователь попросил: «{user_text}». "
                "Ответь как умный Telegram-бот: дай нормальный текст без описания изображения в скобках. Просто расскажи суть."
            )
            gemini_response = model.generate_content(prompt)
            description = format_gemini_response(gemini_response.text.strip())

            keywords = re.sub(r"(покажи|арт|фото|картинку|изображение|пожалуйста|нарисуй|расскажи|немного|про|историю)", "", user_text, flags=re.IGNORECASE).strip()
            if not keywords:
                keywords = "art"

            image_url = await search_unsplash_image(keywords)
            if image_url:
                await message.answer_photo(image_url, caption=description)
            else:
                await message.answer("😔 Не удалось найти подходящее изображение.")
        except Exception as e:
            await message.answer(f"⚠️ Ошибка при поиске изображения: <code>{escape(str(e))}</code>")
        return

    # 💬 Обычный ответ Gemini
    chat_history.setdefault(user_id, []).append({"role": "user", "parts": [user_text]})
    if len(chat_history[user_id]) > 5:
        chat_history[user_id].pop(0)

    try:
        if not await check_internet():
            raise ConnectionError("Нет подключения к интернету")

        response = model.generate_content(chat_history[user_id])
        reply = format_gemini_response(response.text)
        if random.random() < 0.3 and username:
            reply = f"@{username}, {reply}"
        await message.answer(reply)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Не удаётся подключиться к облакам Vandili.", parse_mode=ParseMode.HTML)

    except ConnectionError:
        await message.answer("⚠️ Нет подключения к интернету. Попробуйте позже.", parse_mode=ParseMode.HTML)

    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        error_text = format_gemini_response(str(e))
        await message.answer(f"❌ Ошибка запроса: {error_text}", parse_mode=ParseMode.HTML)

# 🚀 Запуск
if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))
