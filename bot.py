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

# === ВАЖНО: морфология ===
import pymorphy2

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # username бота без @

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Инициализируем морфоанализатор
morph = pymorphy2.MorphAnalyzer()

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-1.5-pro-latest")

chat_history = {}

CAPTION_LIMIT = 950
TELEGRAM_MSG_LIMIT = 4096

IMAGE_TRIGGERS_RU = [
    "покажи", "покажи мне", "хочу увидеть", "пришли фото", "фото"
]

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

RU_EN_DICT = {
    "обезьян": "monkey",
    "тигр": "tiger",
    "кошка": "cat",
    "собак": "dog",
    "пейзаж": "landscape",
    "чайка": "seagull",
    "париж": "paris",
}

### Шаг 1. Функции обработки текста

def format_gemini_response(text: str) -> str:
    """
    1) ```…``` => <pre><code>…</code></pre>
    2) Экранируем HTML
    3) **…** -> <b>…</b>, *…* -> <i>…</i>, `…` -> <code>…</code>
    4) Убираем фразы "не могу показывать картинки"
    5) "* " -> "• "
    """
    code_blocks = {}

    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        return placeholder

    # Шаг 1: ищем блоки ```…```
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)
    # Шаг 2: экранируем
    text = escape(text)
    # Вставляем плейсхолдеры обратно
    for placeholder, block_html in code_blocks.items():
        text = text.replace(escape(placeholder), block_html)

    # Простейшая "Markdown"
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)

    # Удаляем "не могу показывать картинки"
    text = re.sub(r"(Я являюсь текстовым ассистентом.*выводить графику\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I am a text-based model.*cannot directly show images\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I can’t show images directly\.)", "", text, flags=re.IGNORECASE)

    # Заменяем "* " в начале строки на "• "
    lines = text.split('\n')
    new_lines = []
    for line in lines:
        stripped = line.lstrip()
        prefix_len = len(line) - len(stripped)
        if stripped.startswith('* ') and not stripped.startswith('**'):
            replaced_line = (' ' * prefix_len) + '• ' + stripped[2:]
            new_lines.append(replaced_line)
        else:
            new_lines.append(line)
    text = '\n'.join(new_lines)

    return text.strip()

def split_smart(text: str, limit: int) -> list[str]:
    """
    "Умная" разбивка, чтобы не превышать limit.
    Ищем '. ' или ' ', иначе рубим жёстко.
    """
    results = []
    start = 0
    length = len(text)
    while start < length:
        remain = length - start
        if remain <= limit:
            results.append(text[start:].strip())
            break
        candidate = text[start : start+limit]
        cut_pos = candidate.rfind('. ')
        if cut_pos == -1:
            cut_pos = candidate.rfind(' ')
            if cut_pos == -1:
                cut_pos = len(candidate)
        else:
            cut_pos += 1
        chunk = text[start : start+cut_pos].strip()
        if chunk:
            results.append(chunk)
        start += cut_pos
    return [x for x in results if x]

### Шаг 2. Поиск "покажи X"

def parse_russian_show_request(user_text: str):
    """
    Ищем "покажи X" => (show_image, rus_word, en_word, leftover).
    """
    lower_text = user_text.lower()
    triggered = any(trig in lower_text for trig in IMAGE_TRIGGERS_RU)
    if not triggered:
        return (False, "", "", user_text)
    match = re.search(r"(покажи|хочу увидеть|пришли фото)\s+([\w\d]+)", lower_text)
    if match:
        rus_word = match.group(2)
    else:
        rus_word = ""
    pattern_remove = rf"(покажи|хочу увидеть|пришли фото)\s+{rus_word}"
    leftover = re.sub(pattern_remove, "", user_text, flags=re.IGNORECASE).strip()
    en_word = ""
    for k, v in RU_EN_DICT.items():
        if k in rus_word:
            en_word = v
            break
    if not en_word:
        en_word = rus_word
    return (True, rus_word, en_word, leftover)

### Шаг 3. Морфологическая подмена "о нём", "о ней" и т.д.

def get_prepositional_form(rus_word: str) -> str:
    """
    Пробуем поставить rus_word в предложный падеж (loct).
    Например, "Париж" -> "Париже", "кошка" -> "кошке".
    Если не получается, вернём исходное.
    """
    parsed = morph.parse(rus_word)
    if not parsed:
        return rus_word  # не смогли распарсить
    # Берём первую (или самую "вероятную") интерпретацию
    p = parsed[0]
    loct = p.inflect({'loct'})
    if loct:
        return loct.word
    return rus_word


def replace_pronouns_morph(leftover: str, rus_word: str) -> str:
    """
    Ищем "о нём/ней" и подменяем на "о <rus_word (предложн.п.)>".
    Аналогично можно добавить "в нём" -> "в <rus_word (предложн.)>" и т.д.

    Например, leftover="и расскажи о нём"
      rus_word="Париж" => get_prepositional_form("Париж") -> "Париже"
      => "и расскажи о Париже"
    """
    # Получаем "Париже" и пр.
    word_prep = get_prepositional_form(rus_word)
    
    # создаём шаблон "о <word_prep>"
    # Регулярки:
    pronoun_map = {
        r"\bо\s+нем\b":  f"о {word_prep}",
        r"\bо\s+нём\b":  f"о {word_prep}",
        r"\bо\s+ней\b":  f"о {word_prep}",
    }
    for pattern, repl in pronoun_map.items():
        leftover = re.sub(pattern, repl, leftover, flags=re.IGNORECASE)
    return leftover


### Шаг 4. Unsplash

async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    if not prompt:
        return None
    url = f"https://api.unsplash.com/photos/random?query={prompt}&client_id={access_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data['urls']['regular']
    except Exception as e:
        logging.warning(f"Ошибка при получении изображения: {e}")
    return None

### Шаг 5. Основной Handler

@dp.message(Command("start"))
async def cmd_start(message: Message):
    greet = (
        "Привет! Я <b>VAI</b> — интеллектуальный помощник.\n\n"
        "Напиши: «покажи Париж и расскажи о нём» — я покажу фото и факты.\n"
        "Теперь я умею более правильно склонять слова (спасибо pymorphy2!).\n\n"
        "Всегда рад помочь!"
    )
    await message.answer(greet)


@dp.message()
async def handle_msg(message: Message):
    # 1) Если группа/супергруппа — проверяем, звали ли бота
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        text_lower = (message.text or "").lower()
        mention_bot = False
        if BOT_USERNAME:
            mention_bot = f"@{BOT_USERNAME.lower()}" in text_lower
        is_reply_to_bot = (
            message.reply_to_message
            and message.reply_to_message.from_user
            and (message.reply_to_message.from_user.id == bot.id)
        )
        mention_keywords = ["vai", "вай", "вэй"]
        mention_by_name = any(keyword in text_lower for keyword in mention_keywords)
        if not mention_bot and not is_reply_to_bot and not mention_by_name:
            return

    user_input = message.text.strip()
    cid = message.chat.id
    logging.info(f"[BOT] cid={cid}, text='{user_input}'")

    # 2) Если команды "имя/автор"
    lower_inp = user_input.lower()
    if any(nc in lower_inp for nc in NAME_COMMANDS):
        await message.answer("Меня зовут <b>VAI</b>!")
        return
    if any(ic in lower_inp for ic in INFO_COMMANDS):
        r = random.choice(OWNER_REPLIES)
        await message.answer(r)
        return

    # 3) Парсим "покажи X"
    show_image, rus_word, image_en, leftover = parse_russian_show_request(user_input)

    # 4) Подменяем "о нём/ней" => "о <rus_word в предл.падеже>"
    if show_image and rus_word:
        leftover = replace_pronouns_morph(leftover, rus_word)

    gemini_text = ""
    leftover = leftover.strip()
    if leftover:
        # вызываем Gemini
        chat_history.setdefault(cid, []).append({"role": "user", "parts": [leftover]})
        if len(chat_history[cid]) > 5:
            chat_history[cid].pop(0)

        try:
            await bot.send_chat_action(cid, "typing")
            resp = model.generate_content(chat_history[cid])
            gemini_text = format_gemini_response(resp.text)
        except Exception as e:
            logging.error(f"[BOT] Error from Gemini: {e}")
            gemini_text = f"⚠️ Ошибка LLM: {escape(str(e))}"

    # 5) Если show_image => Unsplash
    image_url = None
    if show_image and image_en:
        image_url = await get_unsplash_image_url(image_en, UNSPLASH_ACCESS_KEY)

    # 6) Отправляем фото
    if image_url:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(image_url) as r:
                if r.status == 200:
                    photo_bytes = await r.read()
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmpf:
                        tmpf.write(photo_bytes)
                        tmp_path = tmpf.name
                    try:
                        await bot.send_chat_action(cid, "upload_photo")
                        file = FSInputFile(tmp_path, filename="image.jpg")
                        if gemini_text and len(gemini_text) <= CAPTION_LIMIT:
                            await bot.send_photo(cid, file, caption=gemini_text)
                            gemini_text = ""
                        else:
                            await bot.send_photo(cid, file, caption="...")
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)

    # 7) Отправляем текст, если остался
    if gemini_text:
        if len(gemini_text) <= TELEGRAM_MSG_LIMIT:
            await message.answer(gemini_text)
        else:
            chunks = split_smart(gemini_text, TELEGRAM_MSG_LIMIT)
            for c in chunks:
                await message.answer(c)


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
