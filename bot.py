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
# Попробуем подключить googletrans
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

############################
# Вопросы про создателя
############################
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

############################
# Вопросы про имя бота
############################
NAME_COMMANDS = [
    "как тебя зовут",
    "твое имя",
    "твоё имя",
    "what is your name",
    "who are you"
]

NAME_REPLY = "Меня зовут <b>VAI</b>. Рад познакомиться!"

############################
# Триггеры для показа фото
############################
IMAGE_TRIGGERS = [
    "покажи", "покажи мне", "фото", "изображение", "отправь фото",
    "пришли картинку", "прикрепи фото", "покажи картинку",
    "дай фото", "дай изображение", "картинка"
]

############################
# Автозамена RU->EN
############################
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

############################
# Расширенные рег. выражения, вырезаем отговорки
############################
UNWANTED_REGEX = [
    # «извини, я не могу показать…»
    r"(извини.*?не могу (?:напрямую\s+)?показать.*?(\.|$))",
    r"(я\s+не\s+могу\s+показать\s+.*?(\.|$))",
    r"(я\s+текстова\w+\s+модель.*?(\.|$))",
    r"(не\s+име\w+\s+возмож\w+\s+взаимодейств\w+.*?(\.|$))",
    # «я могу помочь вам найти...»
    r"(?:я могу помочь (?:вам\s+)?найти\s+(изображени|картинки).*?(\.|$))",
    # Google/Bing/Yandex упоминания
    r"(google\s*(images)?|yandex\s*(картинк(и|ах))|bing)",
    r"(вы\s+можете\s+найти\s+.*?(google|yandex|bing).*)",
    # Любые ссылки
    r"(https?:\/\/[^\s)]+)",
]

def remove_unwanted_phrases(text: str) -> str:
    for pattern in UNWANTED_REGEX:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    return text

############################
# Сократить текст, если чисто "покажи"
############################
def maybe_shorten_text(original_text: str, user_input: str) -> str:
    if re.search(r"\bпокажи\b", user_input.lower()) and not re.search(r"(расскажи|опиши|факты|пару\s+фактов)", user_input.lower()):
        sentences = re.split(r'(?<=[.!?])\s+', original_text)
        return " ".join(sentences[:2]).strip()
    return original_text

############################
# format_gemini_response
############################
def format_gemini_response(text: str, user_input: str) -> str:
    """Убираем код-блоки, вставки, ссылки, отговорки, Google…"""
    def extract_code(match):
        return ""
    # 1) code-block
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)

    # 2) [вставьте фото...]
    text = re.sub(r"\[.*?(фото|изображени|вставьте|вставить|insert|картинку).*?\]", "", text, flags=re.IGNORECASE)

    # 3) HTML-escape
    text = escape(text)

    # 4) markdown => html
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)

    # 5) вырезаем «не могу показать» и т.п. + ссылки
    text = remove_unwanted_phrases(text)

    # 6) укорачиваем, если только «покажи»
    text = maybe_shorten_text(text.strip(), user_input)
    return text.strip()

############################
# Ген. prompt для Unsplash
############################
def get_safe_prompt(user_input: str) -> str:
    text = user_input.lower()
    text = re.sub(r'[.,!?\-\n]', ' ', text)
    text = re.sub(r"\b(расскажи|покажи|мне|про|факт|фото|изображение|прикрепи|дай|и|о|об|отправь|что|такое|интересное)\b", "", text)
    words = text.strip().split()
    for i, w in enumerate(words):
        if w in PROMPT_FIX:
            words[i] = PROMPT_FIX[w]
    cleaned = " ".join(words).strip()
    if not cleaned:
        return "random"
    if USE_TRANSLATOR:
        translated = translator.translate(cleaned, src="ru", dest="en").text
        logging.info(f"[BOT] Translate RU->EN: '{cleaned}' -> '{translated}'")
        return translated.strip() or "random"
    else:
        return cleaned

############################
# Unsplash запрос
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
# Длина > 950 => режем
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
# /start
############################
@dp.message(Command("start"))
async def cmd_start(message: Message):
    greet_text = (
        "Привет! Я <b>VAI</b> — бот, созданный <i>Vandili</i>.\n\n"
        "Могу отвечать на вопросы и присылать картинки.\n\n"
        "Например:\n"
        "• «покажи кота»\n"
        "• «расскажи про Париж»\n\n"
        "Если хочешь узнать обо мне — спроси «кто тебя создал» или «как тебя зовут».\n\n"
        "Приятного общения! 🦾"
    )
    await message.answer(greet_text, parse_mode=ParseMode.HTML)

############################
# Основной обработчик
############################
@dp.message()
async def handle_message(message: Message):
    user_input = message.text.strip()
    chat_id = message.chat.id
    logging.info(f"[BOT] Получено: '{user_input}', chat_id={chat_id}")

    # Если спрашивают про имя
    if any(name_trigger in user_input.lower() for name_trigger in NAME_COMMANDS):
        await bot.send_chat_action(chat_id, action="typing")
        await asyncio.sleep(1)
        await message.answer("Меня зовут <b>VAI</b>!", parse_mode=ParseMode.HTML)
        return

    # Если спрашивают про создателя
    if any(trigger in user_input.lower() for trigger in INFO_COMMANDS):
        reply = random.choice(OWNER_REPLIES)
        await asyncio.sleep(1)
        await bot.send_chat_action(chat_id, action="typing")
        await message.answer(reply, parse_mode=ParseMode.HTML)
        return

    # Запоминаем историю
    chat_history.setdefault(chat_id, []).append({"role": "user", "parts": [user_input]})
    if len(chat_history[chat_id]) > 5:
        chat_history[chat_id].pop(0)

    try:
        await bot.send_chat_action(chat_id, action="typing")
        # Генерируем ответ
        response = model.generate_content(chat_history[chat_id])
        gemini_text = format_gemini_response(response.text, user_input)
        logging.info(f"[GEMINI] => {gemini_text[:200]}...")

        # Prompt Unsplash
        prompt = get_safe_prompt(user_input)
        image_url = await get_unsplash_image_url(prompt, UNSPLASH_ACCESS_KEY)

        triggered = any(t in user_input.lower() for t in IMAGE_TRIGGERS)
        logging.info(f"[BOT] triggered => {triggered} | image_url => {image_url}")

        if image_url and triggered:
            # Присылаем фото
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if resp.status == 200:
                        photo_bytes = await resp.read()
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmpfile:
                            tmpfile.write(photo_bytes)
                            tmp_path = tmpfile.name

                        chunks = split_text(gemini_text)
                        try:
                            await bot.send_chat_action(chat_id, action="upload_photo")
                            file = FSInputFile(tmp_path, filename="image.jpg")
                            cap = chunks[0] if chunks else "..."
                            await bot.send_photo(chat_id, photo=file, caption=cap, parse_mode=ParseMode.HTML)

                            for chunk in chunks[1:]:
                                await message.answer(chunk, parse_mode=ParseMode.HTML)

                        finally:
                            import os
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)
                        return

        # Иначе — текст
        for chunk in split_text(gemini_text):
            await message.answer(chunk, parse_mode=ParseMode.HTML)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Нет связи с облаками.")
    except ConnectionError:
        await message.answer("⚠️ Нет подключения к интернету.")
    except Exception as e:
        logging.error(f"[BOT] ошибка: {e}")
        from html import escape
        await message.answer(f"❌ Ошибка: {escape(str(e))}", parse_mode=ParseMode.HTML)

############################
# Запуск
############################
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
