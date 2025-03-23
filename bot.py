import logging
import os
import re
import random
import aiohttp
from io import BytesIO
from aiogram.client.default import DefaultBotProperties
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode, ChatType
from aiogram.types import FSInputFile, Message
from html import escape
from dotenv import load_dotenv
from pathlib import Path
import asyncio
import google.generativeai as genai
import tempfile
from aiogram.filters import Command
import pymorphy2

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

morph = pymorphy2.MorphAnalyzer()

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-1.5-pro-latest")

chat_history = {}
CAPTION_LIMIT = 950
TELEGRAM_MSG_LIMIT = 4096

IMAGE_TRIGGERS_RU = ["покажи", "покажи мне", "хочу увидеть", "пришли фото", "фото"]

NAME_COMMANDS = ["как тебя зовут", "твое имя", "твоё имя", "what is your name", "who are you"]
INFO_COMMANDS = ["кто тебя создал", "кто ты", "кто разработчик", "кто твой автор", "кто твой создатель", "чей ты бот", "кем ты был создан", "кто хозяин", "кто твой владелец"]
OWNER_REPLIES = [
    "Я — <b>VAI</b>, Telegram-бот, созданный <i>Vandili</i>.",
    "Мой создатель — <b>Vandili</b>. Я работаю для него.",
    "Я принадлежу <i>Vandili</i>, он мой автор.",
    "Создан <b>Vandili</b> — именно он дал мне жизнь.",
    "Я бот <b>Vandili</b>. Всё просто.",
    "Я продукт <i>Vandili</i>. Он мой единственный владелец."
]

RU_EN_DICT = {
    "обезьян": "monkey",
    "тигр": "tiger",
    "кошка": "cat",
    "собак": "dog",
    "пейзаж": "landscape",
    "чайка": "seagull",
    "париж": "paris",
    "утконос": "platypus",
}

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
    for placeholder, block_html in code_blocks.items():
        text = text.replace(escape(placeholder), block_html)

    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)

    text = re.sub(r"(I can’t show images directly\.|I am a text-based model.*?graphics\.|Я являюсь текстовым ассистентом.*?)", "", text, flags=re.IGNORECASE)

    lines = text.split('\n')
    text = '\n'.join((' ' * (len(l) - len(l.lstrip())) + '• ' + l.lstrip()[2:] if l.lstrip().startswith('* ') else l) for l in lines)

    return text.strip()

def split_smart(text: str, limit: int) -> list[str]:
    results = []
    start = 0
    while start < len(text):
        chunk = text[start:start+limit]
        cut = chunk.rfind('. ')
        if cut == -1:
            cut = chunk.rfind(' ')
        if cut == -1:
            cut = limit
        results.append(text[start:start+cut].strip())
        start += cut
    return results

def parse_russian_show_request(user_text: str):
    lower = user_text.lower()
    if not any(t in lower for t in IMAGE_TRIGGERS_RU):
        return (False, "", "", user_text)
    match = re.search(r"(покажи|хочу увидеть|пришли фото)\s+([\w\d]+)", lower)
    rus_word = match.group(2) if match else ""
    leftover = re.sub(rf"(покажи|хочу увидеть|пришли фото)\s+{rus_word}", "", user_text, flags=re.IGNORECASE).strip()
    en_word = RU_EN_DICT.get(rus_word, rus_word)
    return (True, rus_word, en_word, leftover)

def get_prepositional_form(rus_word: str) -> str:
    parsed = morph.parse(rus_word)
    if not parsed:
        return rus_word
    form = parsed[0].inflect({"loct"})
    return form.word if form else rus_word

def replace_pronouns_morph(text: str, word: str) -> str:
    prep = get_prepositional_form(word)
    text = re.sub(r"\bо\s+(н[её]м|ней)\b", f"о {prep}", text, flags=re.IGNORECASE)
    return text

async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    url = f"https://api.unsplash.com/photos/random?query={prompt}&client_id={access_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['urls']['regular']
    except Exception as e:
        logging.warning(f"[Unsplash] error: {e}")
    return None

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я <b>VAI</b> — интеллектуальный помощник.\n\n"
        "Напиши: «покажи Париж и расскажи о нём» — я покажу фото и факты.\n"
        "Теперь я умею склонять слова и грамотно отвечать :)"
    )

@dp.message()
async def handle_msg(message: Message):
    text = message.text.strip()
    cid = message.chat.id
    lower = text.lower()

    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if not (f"@{BOT_USERNAME.lower()}" in lower or any(k in lower for k in ["vai", "вай", "вэй"])):
            return

    if any(q in lower for q in NAME_COMMANDS):
        return await message.answer("Меня зовут <b>VAI</b>!")
    if any(q in lower for q in INFO_COMMANDS):
        return await message.answer(random.choice(OWNER_REPLIES))

    show_image, rus_word, image_en, leftover = parse_russian_show_request(text)
    if show_image:
        leftover = replace_pronouns_morph(leftover, rus_word)

    gemini_text = ""
    if leftover:
        chat_history.setdefault(cid, []).append({"role": "user", "parts": [leftover]})
        chat_history[cid] = chat_history[cid][-5:]
        try:
            await bot.send_chat_action(cid, "typing")
            response = model.generate_content(chat_history[cid])
            gemini_text = format_gemini_response(response.text)
        except Exception as e:
            gemini_text = f"<i>Ошибка: {escape(str(e))}</i>"

    photo_sent = False
    if show_image and image_en:
        url = await get_unsplash_image_url(image_en, UNSPLASH_ACCESS_KEY)
        if url:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url) as r:
                    if r.status == 200:
                        photo = await r.read()
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                            tmp.write(photo)
                            tmp_path = tmp.name
                        try:
                            file = FSInputFile(tmp_path, filename="image.jpg")
                            await bot.send_chat_action(cid, "upload_photo")
                            if gemini_text and len(gemini_text) <= CAPTION_LIMIT:
                                await bot.send_photo(cid, file, caption=gemini_text)
                                gemini_text = ""
                            else:
                                await bot.send_photo(cid, file, caption="Вот изображение:")
                            photo_sent = True
                        finally:
                            os.remove(tmp_path)

    if gemini_text:
        chunks = split_smart(gemini_text, TELEGRAM_MSG_LIMIT)
        for ch in chunks:
            await message.answer(ch)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
