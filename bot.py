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
# Попытка подключить googletrans
############################
try:
    from googletrans import Translator
    translator = Translator()
    USE_TRANSLATOR = True
except ImportError:
    translator = None
    USE_TRANSLATOR = False

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-1.5-pro-latest")

###########################
# Память по chat_id
###########################
chat_history = {}

# Спрашивают имя бота
NAME_COMMANDS = [
    "как тебя зовут",
    "твое имя", "твоё имя",
    "what is your name", "who are you"
]
NAME_REPLY = "Меня зовут <b>VAI</b>!"

# Спрашивают про создателя
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

# Триггеры для картинки
IMAGE_TRIGGERS = [
    "покажи", "покажи мне", "фото", "изображение", "отправь фото",
    "пришли картинку", "прикрепи фото", "покажи картинку",
    "дай фото", "дай изображение", "картинка"
]

# Автозамена для Unsplash
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
# Расширенные паттерны (ещё жёстче)
############################
UNWANTED_REGEX = [
    # Любое упоминание «не могу показать…» (много вариантов)
    r"(?is)(извини.*?не могу.*?показать.*?(\.|$))",
    r"(?is)(я\s+не\s+могу\s+(?:напрямую\s+)?показать.*?(\.|$))",
    r"(?is)(не\s+могу\s+непосредственно\s+показать.*?(\.|$))",
    r"(?is)(?:я\s+текстова\w+\s+модель.*?(\.|$))",
    r"(?is)(не\s+име\w+\s+возмож\w+.*?(\.|$))",
    r"(?is)(я могу помочь.*?найти.*?(изображени|картинки).*?(\.|$))",
    # Любые ссылки
    r"(?is)(https?:\/\/[^\s)]+)",
    # Любое упоминание поисковиков
    r"(?is)(google|yandex|bing|search engine|поисковик|поисковой\s+системе)",
    # «я рекомендую» + «поищите / поискать» и тп
    r"(?is)(я\s+рекомендую\s+поиск.*?(\.|$))",
    r"(?is)(вы\s+можете\s+найти\s+.*?(google|yandex|bing).*)",
]

def remove_unwanted_phrases(text: str) -> str:
    for pattern in UNWANTED_REGEX:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    return text

############################
# Сокращение текста, если "покажи"
############################
def maybe_shorten_text(original: str, user_input: str) -> str:
    if re.search(r"\bпокажи\b", user_input.lower()) and not re.search(r"(расскажи|опиши|факты|пару\s+фактов)", user_input.lower()):
        sents = re.split(r'(?<=[.!?])\s+', original)
        return " ".join(sents[:2]).strip()
    return original

def format_gemini_response(text: str, user_input: str) -> str:
    """Убираем код-блоки, ссылки, отговорки, поисковики и сокращаем."""
    # 1) Код-блоки
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", "", text)

    # 2) [вставить фото...]
    text = re.sub(r"\[.*?(фото|изображени|вставьте|вставить|insert|картинку).*?\]", "", text, flags=re.IGNORECASE)

    # 3) Экранируем
    text = escape(text)

    # 4) Markdown → HTML
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)

    # 5) Вырезаем отговорки/ссылки/поисковики
    text = remove_unwanted_phrases(text)

    # 6) Сокращаем, если чисто "покажи"
    text = maybe_shorten_text(text.strip(), user_input)
    return text.strip()

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
        tr = translator.translate(cleaned, src="ru", dest="en").text
        logging.info(f"[BOT] RU->EN '{cleaned}' => '{tr}'")
        return tr.strip() or "random"
    return cleaned

async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    url = f"https://api.unsplash.com/photos/random?query={prompt}&client_id={access_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                logging.info(f"[UNSPLASH] {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    if "urls" in data and "regular" in data["urls"]:
                        return data["urls"]["regular"]
    except Exception as e:
        logging.warning(f"Ошибка Unsplash: {e}")
    return None

def split_text(text: str, max_len=950):
    parts = []
    while len(text) > max_len:
        idx = text[:max_len].rfind('. ')
        if idx == -1:
            idx = max_len
        parts.append(text[:idx+1].strip())
        text = text[idx+1:].strip()
    if text:
        parts.append(text)
    return parts

# /start
@dp.message(Command("start"))
async def cmd_start(message: Message):
    greet = (
        "Привет! Я <b>VAI</b>, бот, созданный <i>Vandili</i>.\n\n"
        "Отвечаю на вопросы и присылаю картинки.\n\n"
        "• «покажи кота»\n"
        "• «расскажи про Париж»\n\n"
        "Если хочешь узнать обо мне: «кто тебя создал» или «как тебя зовут».\n\n"
        "Приятного общения! 🦾"
    )
    await message.answer(greet, parse_mode=ParseMode.HTML)

@dp.message()
async def handle_message(message: Message):
    user_input = message.text.strip()
    cid = message.chat.id
    logging.info(f"[BOT] {cid} => '{user_input}'")

    # Спрашивают имя
    if any(name_trig in user_input.lower() for name_trig in NAME_COMMANDS):
        await message.answer("Меня зовут <b>VAI</b>!", parse_mode=ParseMode.HTML)
        return

    # Спрашивают о создателе
    if any(info_trig in user_input.lower() for info_trig in INFO_COMMANDS):
        rep = random.choice(OWNER_REPLIES)
        await message.answer(rep, parse_mode=ParseMode.HTML)
        return

    chat_history.setdefault(cid, []).append({"role": "user", "parts": [user_input]})
    if len(chat_history[cid]) > 5:
        chat_history[cid].pop(0)

    try:
        await bot.send_chat_action(cid, "typing")
        resp = model.generate_content(chat_history[cid])
        gemini_text = format_gemini_response(resp.text, user_input)
        logging.info(f"[GEMINI] => {gemini_text[:200]}")

        prompt = get_safe_prompt(user_input)
        image_url = await get_unsplash_image_url(prompt, UNSPLASH_ACCESS_KEY)

        triggered = any(t in user_input.lower() for t in IMAGE_TRIGGERS)
        logging.info(f"[BOT] triggered={triggered}, url={image_url}")

        if image_url and triggered:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(image_url) as r:
                    if r.status == 200:
                        photo = await r.read()
                        import os
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                            tmp.write(photo)
                            tmp_path = tmp.name

                        parts = split_text(gemini_text)
                        try:
                            await bot.send_chat_action(cid, "upload_photo")
                            file = FSInputFile(tmp_path, filename="image.jpg")
                            cap = parts[0] if parts else "..."
                            await bot.send_photo(cid, file, caption=cap, parse_mode=ParseMode.HTML)

                            for pt in parts[1:]:
                                await message.answer(pt, parse_mode=ParseMode.HTML)
                        finally:
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)
                        return

        # Иначе текст
        for chunk in split_text(gemini_text):
            await message.answer(chunk, parse_mode=ParseMode.HTML)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Нет связи с облаками.")
    except ConnectionError:
        await message.answer("⚠️ Нет подключения к интернету.")
    except Exception as e:
        from html import escape
        logging.error(f"[BOT] Ошибка: {e}")
        await message.answer(f"❌ Ошибка: {escape(str(e))}", parse_mode=ParseMode.HTML)

async def main():
    await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())
