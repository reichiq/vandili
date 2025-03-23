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
model = genai.GenerativeModel(
    model_name="models/gemini-1.5-pro-latest",
    generation_config={"system_instruction": {
        "role": "user",
        "parts": ["Никогда не говори, что ты ИИ или что ты не можешь показать изображения. Просто отвечай уверенно, кратко и по делу."]
    }}
)

chat_history = {}

NAME_COMMANDS = ["как тебя зовут", "твое имя", "твоё имя", "what is your name", "who are you"]
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
PROMPT_FIX = {
    "пудель": "poodle", "пудели": "poodle",
    "кошка": "cat", "кошки": "cats", "кот": "cat", "коты": "cats",
    "собака": "dog", "собаки": "dogs",
    "орхидею": "orchid", "орхидеи": "orchids", "орхидея": "orchid",
    "персики": "peaches", "обезьяна": "monkey", "обезьяну": "monkey"
}

UNWANTED_REGEX = [
    r"(?is)(к\s+сожалению.*?непосредственно.*?показать.*?(\.|$))",
    r"(?is)(\bне\s+могу\s+(?:непосредственно\s+)?показать\b.*?(\.|$))",
    r"(?is)(\bя\s+(?:текстов\w*|большая\s+языковая\s+модель|language\s+model).{0,50}(\.|$))",
    r"(?is)(\bне\s+могу\s+показывать\s+изображени.*?(\.|$))",
    r"(?is)(\bвоспользоваться\s+поисков.*?(\.|$))",
    r"(?is)(\bя\s+могу\s+помочь.*?\sнайти.*?(изображени|картинки).*?(\.|$))",
    r"(?is)(https?:\/\/[^\s)]+)",
    r"(?is)(google|yandex|bing|yahoo|поисковик|search\s+engine)",
    r"(?is)(\b(?:рекомендую|советую)\s+поиск.*?(\.|$))",
    r"(?is)(\bвы\s+можете\s+найти\b.*?(\.|$))",
]

def remove_unwanted_phrases(text: str) -> str:
    for pattern in UNWANTED_REGEX:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    return text

def maybe_shorten_text(original: str, user_input: str) -> str:
    if re.search(r"\bпокажи\b", user_input.lower()) and not re.search(r"(расскажи|опиши|факты|пару\s+фактов)", user_input.lower()):
        sents = re.split(r'(?<=[.!?])\s+', original)
        return " ".join(sents[:2]).strip()
    return original

def format_gemini_response(text: str, user_input: str) -> str:
    text = re.sub(r"```(?:\w+)?\n([\s\S]+?)```", "", text)
    text = re.sub(r"\[.*?(фото|изображени|вставьте|вставить|insert|картинку).*?\]", "", text, flags=re.IGNORECASE)
    text = escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    text = re.sub(r'^\s*\*\s+', '• ', text, flags=re.MULTILINE)
    text = remove_unwanted_phrases(text)
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
        result = translator.translate(cleaned, src="ru", dest="en").text
        logging.info(f"[BOT] RU->EN: '{cleaned}' => '{result}'")
        return result.strip() or "random"
    return cleaned

async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    url = f"https://api.unsplash.com/photos/random?query={prompt}&client_id={access_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                logging.info(f"[UNSPLASH] Status={resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    return data["urls"]["regular"]
    except Exception as e:
        logging.warning(f"Unsplash error: {e}")
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

@dp.message(Command("start"))
async def cmd_start(message: Message):
    greet = (
        "Привет! Я <b>VAI</b> — интеллектуальный помощник.\n\n"
        "Я могу отвечать на самые разные вопросы, делиться фактами, рассказывать интересное и даже показывать изображения по твоему запросу.\n\n"
        "Попробуй, например:\n"
        "• «покажи тигра»\n"
        "• «расскажи про Луну»\n\n"
        "Всегда рад пообщаться! 🧠✨"
    )
    await message.answer(greet)

@dp.message()
async def handle_msg(message: Message):
    user_input = message.text.strip()
    cid = message.chat.id
    logging.info(f"[BOT] cid={cid}, text='{user_input}'")

    if "сложи два числа" in user_input.lower():
        explanation = (
            "<b>Пример Python-кода:</b>\n\n"
            "<pre>def сложить_числа(a, b):\n"
            "    \"\"\"Складывает два числа и возвращает результат.\"\"\"\n"
            "    сумма = a + b\n"
            "    return сумма\n\n"
            "число1 = float(input(\"Введите первое число: \"))\n"
            "число2 = float(input(\"Введите второе число: \"))\n"
            "результат = сложить_числа(число1, число2)\n"
            "print(\"Сумма:\", результат)</pre>\n\n"
            "<b>Объяснение:</b>\n"
            "• Функция принимает два аргумента и возвращает их сумму.\n"
            "• Пользователь вводит два числа с клавиатуры.\n"
            "• Результат отображается в консоли."
        )
        await message.answer(explanation, parse_mode=ParseMode.HTML)
        return

    if any(name_trig in user_input.lower() for name_trig in NAME_COMMANDS):
        await message.answer("Меня зовут <b>VAI</b>!")
        return

    if any(info_trig in user_input.lower() for info_trig in INFO_COMMANDS):
        r = random.choice(OWNER_REPLIES)
        await message.answer(r)
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
        logging.info(f"[BOT] triggered={triggered}, image={image_url}")

        if image_url and triggered:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(image_url) as r:
                    if r.status == 200:
                        photo_bytes = await r.read()
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmpf:
                            tmpf.write(photo_bytes)
                            tmp_path = tmpf.name

                        parts = split_text(gemini_text)
                        try:
                            await bot.send_chat_action(cid, "upload_photo")
                            file = FSInputFile(tmp_path, filename="image.jpg")
                            cpt = parts[0] if parts else "..."
                            await bot.send_photo(cid, file, caption=cpt)
                            for chunk in parts[1:]:
                                await message.answer(chunk)
                        finally:
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)
                        return

        for chunk in split_text(gemini_text):
            await message.answer(chunk)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Ошибка: Нет связи с облаками.")
    except ConnectionError:
        await message.answer("⚠️ Нет подключения к интернету.")
    except Exception as e:
        logging.error(f"[BOT] Error: {e}")
        await message.answer(f"❌ Ошибка: {escape(str(e))}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
