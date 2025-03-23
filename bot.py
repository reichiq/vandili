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
from aiogram.filters import Command

############################
# Попробуем подключить переводчик googletrans
############################
try:
    from googletrans import Translator
    translator = Translator()
    USE_TRANSLATOR = True
except ImportError:
    translator = None
    USE_TRANSLATOR = False

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

############################
# Память для каждого чата
############################
chat_history = {}

INFO_COMMANDS = [
    "кто тебя создал", "кто ты", "кто разработчик", "кто твой автор",
    "кто твой создатель", "чей ты бот", "кем ты был создан",
    "кто хозяин", "кто твой владелец", "в смысле кто твой создатель"
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

###########################
# Словарь авто-замен (рус->англ)
###########################
PROMPT_FIX = {
    "пудель": "poodle",
    "пудели": "poodle",
    "кошка": "cat",
    "кошки": "cats",
    "кот": "cat",
    "коты": "cats",
    "собака": "dog",
    "собаки": "dogs",
    "орхидею": "orchid",
    "орхидеи": "orchids",
    "орхидея": "orchid",
    "персики": "peaches",
    "обезьяна": "monkey",
    "обезьяну": "monkey"
}

###########################
# Расширенные регулярки, чтобы вырезать
# "я текстовая модель" + "ищи в Google" и т.д.
###########################
UNWANTED_REGEX = [
    # Отговорки "не могу показать" (без привязки к одним словам)
    r"(?:извини.*?не могу (напрямую )?показать.*?изображени.*?(\.|$))",
    r"(?:я\s+не\s+могу\s+показать\s+.*?(\.|$))",
    r"(?:я\s+текстова\w+\s+модель.*?(\.|$))",
    r"(?:не име\w+ возмож\w+ взаимодействовать.*?(\.|$))",

    # Ссылки на Google/Yandex
    r"(?:google\s*(images)?|yandex\s*(картинк(и|ах)))",
    r"(?:ищите\s+(в\s+)?google|ищите\s+(в\s+)?yandex)",
    r"(?:вы\s+можете\s+найти\s+.*?google\s+images.*?(\.|$))"
]

###########################
# Функция удаления нежелательного текста
###########################
def remove_unwanted_phrases(text: str) -> str:
    for pattern in UNWANTED_REGEX:
        # Удаляем кусок целиком (примерно до точки или перевода строки)
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    return text

############################
# format_gemini_response
############################
def format_gemini_response(text: str) -> str:
    """
    Убираем код-блоки, вставки типа [вставить картинку],
    вырезаем все отговорки, ссылки на google.
    Переводим Markdown -> HTML.
    """
    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK__"
        return placeholder

    # Удаляем code-blockи
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)

    # [вставить картинку..., insert..., фото...]
    text = re.sub(r"\[.*?(фото|изображени|вставьте|вставить|insert|картинку).*?\]", "", text, flags=re.IGNORECASE)

    # HTML-escape
    text = escape(text)

    # Markdown => HTML
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)

    # Удаляем нежелательные отговорки по расширенным regex
    text = remove_unwanted_phrases(text)

    return text.strip()

############################
# Генерация prompt для Unsplash
############################
def get_safe_prompt(user_input: str) -> str:
    """
    1) Удаляем знаки и стоп-слова
    2) Заменяем некоторые слова через PROMPT_FIX
    3) Переводим (если googletrans есть)
    """
    text = user_input.lower()
    # Удаляем пунктуацию
    text = re.sub(r'[.,!?\-\n]', ' ', text)
    # Убираем стоп-слова
    text = re.sub(
        r"\b(расскажи|покажи|мне|про|факт|фото|изображение|прикрепи|дай|и|о|об|отправь|что|такое|интересное)\b",
        "",
        text
    )
    words = text.strip().split()

    # PROMPT_FIX
    for i, w in enumerate(words):
        if w in PROMPT_FIX:
            words[i] = PROMPT_FIX[w]

    cleaned = " ".join(words).strip()
    if not cleaned:
        return "random"

    # Переводим всю оставшуюся фразу
    if USE_TRANSLATOR:
        translated = translator.translate(cleaned, src="ru", dest="en").text
        logging.info(f"[BOT] Translate RU->EN: '{cleaned}' -> '{translated}'")
        prompt = translated.strip()
        return prompt if prompt else "random"
    else:
        return cleaned

############################
# Запрос к Unsplash
############################
async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
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

############################
# Разделяем текст на части
############################
def split_text(text: str, max_length: int = 950):
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

############################
# /start (привет)
############################
@dp.message(Command("start"))
async def cmd_start(message: Message):
    greet_text = (
        "Привет! Я <b>VAI</b> — бот, созданный <i>Vandili</i>.\n\n"
        "Могу отвечать на вопросы и присылать картинки.\n\n"
        "Например:\n"
        "• «покажи кота»\n"
        "• «расскажи про Париж»\n\n"
        "Если хочешь узнать обо мне — спроси «кто тебя создал».\n\n"
        "Приятного общения! 🦾"
    )
    await message.answer(greet_text, parse_mode=ParseMode.HTML)

############################
# Основная логика
############################
@dp.message()
async def handle_message(message: Message):
    user_input = message.text.strip()
    chat_id = message.chat.id
    logging.info(f"[BOT] Получено: '{user_input}', chat_id={chat_id}")

    # Проверяем команду о создателе
    if any(trigger in user_input.lower() for trigger in INFO_COMMANDS):
        reply = random.choice(OWNER_REPLIES)
        await asyncio.sleep(1)
        await bot.send_chat_action(chat_id, action="typing")
        await message.answer(reply, parse_mode=ParseMode.HTML)
        return

    # Чат-история
    chat_history.setdefault(chat_id, []).append({"role": "user", "parts": [user_input]})
    if len(chat_history[chat_id]) > 5:
        chat_history[chat_id].pop(0)

    try:
        await bot.send_chat_action(chat_id, action="typing")
        # Генерация Gemini
        response = model.generate_content(chat_history[chat_id])
        gemini_text = format_gemini_response(response.text)
        logging.info(f"[GEMINI] text => {gemini_text[:200]}...")

        # Prompt для Unsplash
        prompt = get_safe_prompt(user_input)
        logging.info(f"[BOT] Unsplash prompt => '{prompt}'")
        image_url = await get_unsplash_image_url(prompt, UNSPLASH_ACCESS_KEY)
        logging.info(f"[BOT] image_url => {image_url}")

        triggered = any(t in user_input.lower() for t in IMAGE_TRIGGERS)
        logging.info(f"[BOT] triggered => {triggered}")

        if image_url and triggered:
            logging.info("[BOT] Скачиваю Unsplash-фото...")
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if resp.status == 200:
                        photo_bytes = await resp.read()
                        size = len(photo_bytes)
                        logging.info(f"[BOT] скачано {size} байт.")
                        import os
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmpfile:
                            tmpfile.write(photo_bytes)
                            tmp_path = tmpfile.name

                        # Нарезаем текст
                        chunks = split_text(gemini_text)
                        try:
                            await bot.send_chat_action(chat_id, action="upload_photo")
                            file = FSInputFile(tmp_path, filename="image.jpg")
                            caption_part = chunks[0] if chunks else " "
                            await bot.send_photo(
                                chat_id=chat_id,
                                photo=file,
                                caption=caption_part,
                                parse_mode=ParseMode.HTML
                            )
                            for chunk in chunks[1:]:
                                await message.answer(chunk, parse_mode=ParseMode.HTML)
                        finally:
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)
                        return
                    else:
                        logging.warning(f"[BOT] resp.status={resp.status}, без фото...")

        # Если не получилось - просто текст
        for chunk in split_text(gemini_text):
            await message.answer(chunk, parse_mode=ParseMode.HTML)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Нет связи с облаками.")
    except ConnectionError:
        await message.answer("⚠️ Нет подключения к интернету.")
    except Exception as e:
        logging.error(f"[BOT] ошибка: {e}")
        await message.answer(f"❌ Ошибка: {escape(str(e))}", parse_mode=ParseMode.HTML)

############################
# Запуск
############################
async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
