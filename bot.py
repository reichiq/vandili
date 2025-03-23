import logging
import os
import re
import random
import aiohttp
from io import BytesIO
from aiogram.client.default import DefaultBotProperties
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, Message
from html import escape
from dotenv import load_dotenv
from pathlib import Path
import asyncio
import google.generativeai as genai
import tempfile
from aiogram.filters import Command  # <-- нужно для @dp.message(Command("start"))

# Загрузка .env
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-1.5-pro-latest")

chat_history = {}

INFO_COMMANDS = [
    "кто тебя создал", "кто ты", "кто разработчик", "кто твой автор",
    "кто твой создатель", "чей ты бот", "кем ты был создан", "кто хозяин",
    "кто твой владелец", "в смысле кто твой создатель"
]

OWNER_REPLIES = [
    "Я — <b>VAI</b>, Telegram-бот, созданный <i>Vandili</i>.",
    "Мой создатель — <b>Vandili</b>. Я работаю для него.",
    "Я принадлежу <i>Vandili</i>, он мой автор.",
    "Создан <b>Vandili</b> — именно он дал мне жизнь.",
    "Я бот <b>Vandili</b>. Всё просто.",
    "Я продукт <i>Vandili</i>. Он мой единственный владелец."
]

IMAGE_TRIGGERS = [
    "покажи", "покажи мне", "фото", "изображение", "отправь фото",
    "пришли картинку", "прикрепи фото", "покажи картинку", 
    "дай фото", "дай изображение", "картинка"
]

UNWANTED_GEMINI_PHRASES = [
    "Извини, я не могу показывать изображения",
    "я не могу показывать изображения",
    "I cannot display images",
    "I can't display images",
    "I am a text-based model",
    "I'm a text-based model",
    "I can't show images"
]

def format_gemini_response(text: str) -> str:
    """Форматируем ответ от Gemini в Telegram HTML."""
    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK__"
        return placeholder

    # Удаляем код-блоки
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)
    # Удаляем вставочные заглушки от Gemini
    text = re.sub(r"\[.*?(фото|изображени|вставьте).*?\]", "", text, flags=re.IGNORECASE)
    # Экранируем HTML
    text = escape(text)

    # Markdown → HTML
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)

    # Удаляем нежелательные фразы: «я не могу показывать изображения» и т.п.
    for phrase in UNWANTED_GEMINI_PHRASES:
        if phrase.lower() in text.lower():
            text = re.sub(phrase, "", text, flags=re.IGNORECASE)

    return text.strip()

def get_safe_prompt(text: str) -> str:
    """Извлекаем осмысленный prompt для Unsplash, убирая стоп-слова."""
    text = re.sub(r'[.,!?\-\n]', ' ', text.lower())
    text = re.sub(r"\b(расскажи|покажи|мне|про|факт|фото|изображение|прикрепи|дай|и|о|об|отправь|что|такое|интересное)\b", "", text)
    words = text.strip().split()
    return " ".join(words[:3]) if words else "random"

async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    """Запрашиваем Unsplash, получаем URL regular-качества."""
    url = f"https://api.unsplash.com/photos/random?query={prompt}&client_id={access_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                logging.info(f"[UNSPLASH] status: {response.status}")
                if response.status == 200:
                    data = await response.json()
                    if "urls" in data and "regular" in data["urls"]:
                        return data["urls"]["regular"]
    except Exception as e:
        logging.warning(f"Ошибка при получении изображения: {e}")
    return None

def split_text(text: str, max_length: int = 950):
    """Разбиваем текст по точкам, чтобы не рвать предложения."""
    parts = []
    while len(text) > max_length:
        split_index = text[:max_length].rfind('. ')
        if split_index == -1:
            split_index = max_length
        parts.append(text[:split_index+1].strip())
        text = text[split_index+1:].strip()
    if text:
        parts.append(text)
    return parts

#######################################
# Обработчик /start (приветственное)
#######################################
from aiogram.filters import Command

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Отправляем приветствие и короткую инструкцию."""
    greet_text = (
        "Привет! Я <b>VAI</b> — бот, созданный <i>Vandili</i>.\n\n"
        "Я умею:\n"
        "• Отвечать на твои вопросы (Gemini)\n"
        "• Присылать картинки (Unsplash)\n\n"
        "Просто напиши «покажи кота» или «расскажи про Париж»!\n"
        "Если хочешь узнать обо мне — спроси «кто тебя создал».\n\n"
        "Приятного общения! 🦾"
    )
    await message.answer(greet_text, parse_mode=ParseMode.HTML)

#######################################
# Основной обработчик текстовых сообщений
#######################################
@dp.message()
async def handle_message(message: Message):
    user_input = message.text.strip()
    user_id = message.from_user.id
    logging.info(f"[BOT] Получено: '{user_input}', от user_id={user_id}")

    # Проверка команд о владельце
    if any(trigger in user_input.lower() for trigger in INFO_COMMANDS):
        reply = random.choice(OWNER_REPLIES)
        logging.info("[BOT] Отправляю инфо-ответ (Vandili).")
        await bot.send_chat_action(message.chat.id, action="typing")
        await asyncio.sleep(1.2)
        await message.answer(reply, parse_mode=ParseMode.HTML)
        return

    # Чат-история
    chat_history.setdefault(user_id, []).append({"role": "user", "parts": [user_input]})
    if len(chat_history[user_id]) > 5:
        chat_history[user_id].pop(0)

    try:
        await bot.send_chat_action(message.chat.id, action="typing")
        # Генерация текста
        response = model.generate_content(chat_history[user_id])
        gemini_text = format_gemini_response(response.text)
        logging.info(f"[GEMINI] Итоговый текст: {gemini_text[:200]}...")

        # Prompt для Unsplash
        image_prompt = get_safe_prompt(user_input)
        logging.info(f"[BOT] image_prompt='{image_prompt}'")
        image_url = await get_unsplash_image_url(image_prompt, UNSPLASH_ACCESS_KEY)
        logging.info(f"[BOT] image_url={image_url}")

        # Проверяем триггер
        triggered = any(t in user_input.lower() for t in IMAGE_TRIGGERS)
        logging.info(f"[BOT] triggered={triggered}")

        # Если есть URL + триггер => отправляем фото
        if image_url and triggered:
            logging.info("[BOT] Пробую скачать файл с Unsplash и отправить фото...")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(image_url) as resp:
                        if resp.status == 200:
                            photo_bytes = await resp.read()
                            size = len(photo_bytes)
                            logging.info(f"[BOT] скачано {size} байт.")
                            # Сохраним во временный файл
                            import os
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmpfile:
                                tmpfile.write(photo_bytes)
                                tmp_path = tmpfile.name

                            chunks = split_text(gemini_text)
                            try:
                                await bot.send_chat_action(message.chat.id, action="upload_photo")
                                file = FSInputFile(tmp_path, filename="image.jpg")
                                await bot.send_photo(
                                    chat_id=message.chat.id,
                                    photo=file,
                                    caption=chunks[0] if chunks else "...",
                                    parse_mode=ParseMode.HTML
                                )

                                for chunk in chunks[1:]:
                                    await message.answer(chunk, parse_mode=ParseMode.HTML)

                            finally:
                                # Удалим временный файл
                                if tmp_path and os.path.exists(tmp_path):
                                    os.remove(tmp_path)
                            return
                        else:
                            logging.warning(f"[BOT] resp.status={resp.status}, не отправляю фото.")
            except Exception as e:
                logging.warning(f"[BOT] Ошибка при отправке изображения: {e}")

        # Если что-то пошло не так => просто текст
        logging.info("[BOT] Отправляю текст без фото.")
        for chunk in split_text(gemini_text):
            await message.answer(chunk, parse_mode=ParseMode.HTML)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Не удаётся подключиться к облакам Vandili.")
    except ConnectionError:
        await message.answer("⚠️ Нет подключения к интернету.")
    except Exception as e:
        logging.error(f"[BOT] Общая ошибка: {e}")
        await message.answer(f"❌ Ошибка: {escape(str(e))}", parse_mode=ParseMode.HTML)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
