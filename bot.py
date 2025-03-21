import logging
import os
import asyncio
import re
import aiohttp
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

# Список возможных ответов
OWNER_RESPONSES = [
    "Этот бот был создан специально для Vandili. 🔥",
    "Разработчиком этого ИИ является Vandili. 😊",
    "Этот бот — творение Vandili. 😎",
    "Vandili — мой создатель и вдохновитель! 🤖",
    "Моя разработка велась специально для Vandili! 🚀",
    "Я был создан для Vandili, он мой единственный владелец. 🔥",
    "Если кто-то и может мной распоряжаться, то это Vandili! 😉",
    "Благодаря Vandili я существую и отвечаю вам. 😊",
    "Разработал и владеет мной Vandili. 😎",
]

# Ключевые слова для вопросов о владельце, разработчике, предназначении
OWNER_KEYWORDS = [
    "чей ты бот", "кто тебя сделал", "кем ты разработан", "для кого ты создан", 
    "кому принадлежишь", "кто владелец", "кто твой хозяин", "кто тебя написал", 
    "кто разработчик", "кем ты создан", "кто тебя создал", "чей ты"
]


def format_gemini_response(text: str) -> str:
    """
    Форматирует текст от Gemini для корректного отображения в Telegram (MarkdownV2),
    предотвращая ошибки с жирным текстом и кодовыми блоками.
    """

    # Telegram требует экранирования этих символов в обычном тексте (но НЕ в коде!)
    special_chars = r"_[]()~>#+-=|{}.!`"
    for ch in special_chars:
        text = text.replace(ch, f"\\{ch}")

    # Убираем лишние пробелы перед и после кодовых блоков
    text = re.sub(r'\s*```\w*\n', '```\n', text)
    text = re.sub(r'\n```\s*', '\n```', text)

    # Гарантируем, что Telegram правильно рендерит код
    text = re.sub(r'```(\w+)?\n(.*?)\n```', lambda m: f"```\n{m.group(2)}\n```", text, flags=re.DOTALL)

    return text


# Проверка, упомянули ли бота
def is_bot_mentioned(message: types.Message):
    triggers = ["vai", "вай", "VAI", "Vai", "Вай"]
    text = message.text.lower()
    return (
        any(trigger in text for trigger in triggers) or 
        (message.reply_to_message and message.reply_to_message.from_user.id == bot.id)
    )


# Функция для проверки соединения с интернетом
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


# Обработка текстовых сообщений и запрос в Gemini
@dp.message()
async def chat_with_gemini(message: types.Message):
    logging.info(f"Получено сообщение: {message.text} от {message.from_user.id}")

    if message.chat.type != 'private' and not is_bot_mentioned(message):
        return

    user_text = message.text
    for trigger in ["vai", "вай", "VAI", "Vai", "Вай"]:
        user_text = user_text.replace(trigger, "").strip()

    # Проверяем, спрашивает ли пользователь про владельца, разработчика или предназначение
    if any(keyword in user_text.lower() for keyword in OWNER_KEYWORDS):
        await message.answer(format_gemini_response(f"{OWNER_RESPONSES[0]}"), parse_mode="MarkdownV2")
        return

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        # Проверяем подключение перед запросом
        if not await check_internet():
            raise ConnectionError("Нет подключения к интернету")

        response = model.generate_content(user_text).text
        formatted_response = format_gemini_response(response)

        # Логируем ответ перед отправкой
        logging.info(f"Ответ Gemini:\n{formatted_response}")

        await message.answer(formatted_response, parse_mode="MarkdownV2")
    
    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Не удаётся подключиться к облакам Vandili.", parse_mode="MarkdownV2")

    except ConnectionError:
        await message.answer("⚠️ Ошибка: Нет подключения к интернету. Проверьте соединение и попробуйте снова.", parse_mode="MarkdownV2")
    
    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")
        await message.answer(f"❌ Ошибка запроса: `{format_gemini_response(str(e))}`", parse_mode="MarkdownV2")


# Запуск
async def main():
    logging.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
