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

# Лимит для сообщений (Telegram позволяет ~4096 символов, но иногда лучше ставить поменьше)
MESSAGE_LIMIT = 4096
# Лимит caption для фото
CAPTION_LIMIT = 950


def format_gemini_response(text: str) -> str:
    """
    Преобразует текст от Gemini, находя блоки ```…```, превращая их в <pre><code>…</code></pre>,
    а также экранирует HTML-спецсимволы и обрабатывает простейшую Markdown-разметку.
    """
    code_blocks = {}

    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        return placeholder

    # Заменяем ```...``` на плейсхолдеры
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)

    # Экранируем остатки текста (чтобы <, >, & и прочие символы не ломали HTML)
    text = escape(text)

    # Возвращаем на место <pre><code> … </code></pre>
    for placeholder, block in code_blocks.items():
        # при вставке используем escape(placeholder), т.к. placeholder тоже экранирован
        text = text.replace(escape(placeholder), block)

    # Преобразуем **…** -> <b>…</b>, *…* -> <i>…</i>, `…` -> <code>…</code>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)

    return text.strip()


def get_safe_prompt(text: str) -> str:
    """
    Преобразует запрос пользователя в короткий prompt для поиска на Unsplash.
    Например: 'покажи тигра' -> 'тигра'
    """
    text = re.sub(r'[.,!?\-\n]', ' ', text.lower())
    match = re.search(r'покажи(?:\s+мне)?\s+(\w+)', text)
    if match:
        return match.group(1)
    return re.sub(r"[^a-zA-Zа-яА-Я0-9\s]", "", text).strip().split(" ")[0]


async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    """
    Поиск изображения через Unsplash API.
    Возвращает URL или None в случае ошибки.
    """
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


def parse_html_with_codeblocks(html_text: str):
    """
    Разбивает финальный HTML-текст на список "токенов", 
    где каждый токен — кортеж вида (type, content):
      - ('code', '<pre><code>...</code></pre>')  для кодовых блоков
      - ('text', '...') для обычного текста
    Нужно, чтобы мы не "рвали" кодовые блоки при разбивке.
    """
    tokens = []
    pattern = re.compile(r'(<pre><code.*?>.*?</code></pre>)', re.DOTALL)
    parts = pattern.split(html_text)

    for part in parts:
        if not part:
            continue
        if part.startswith('<pre><code'):
            tokens.append(("code", part))
        else:
            tokens.append(("text", part))
    return tokens


def build_caption_and_rest(html_text: str, max_caption_len: int = CAPTION_LIMIT):
    """
    Делит итоговый HTML-текст на две части: 
    1) caption (до max_caption_len символов),
    2) leftover (всё, что не влезло в caption).
    При этом кодовые блоки ('<pre><code>...</code></pre>') НЕ дробятся.
    Если целиком блок кода не влезает — отправляем его целиком в leftover.
    """
    tokens = parse_html_with_codeblocks(html_text)
    current_len = 0
    caption_builder = []
    leftover_builder = []

    for (ttype, content) in tokens:
        if len(content) > max_caption_len:
            # Целиком блок больше лимита — уходит целиком в leftover
            leftover_builder.append((ttype, content))
        else:
            # Проверяем, влезает ли вместе с уже накопленным
            if current_len + len(content) <= max_caption_len:
                caption_builder.append((ttype, content))
                current_len += len(content)
            else:
                leftover_builder.append((ttype, content))

    # Преобразуем токены обратно в строки
    caption_str = "".join([c for _, c in caption_builder]).strip()
    leftover_tokens = leftover_builder  # список (type, content)

    return caption_str, leftover_tokens


def split_text_smart(text: str, limit: int = MESSAGE_LIMIT) -> list:
    """
    "Смысловая" разбивка длинного текста на части, 
    стараясь разрезать по '. ' или хотя бы по пробелу.
    """
    chunks = []
    chunk_start = 0
    text_len = len(text)

    while chunk_start < text_len:
        # если остаток короче лимита - берем целиком
        if (text_len - chunk_start) <= limit:
            chunks.append(text[chunk_start:].strip())
            break
        # ищем точку с пробелом ближайшую к пределу
        slice_end = chunk_start + limit
        slice_chunk = text[chunk_start:slice_end]
        # пытаемся найти точку с пробелом
        idx = slice_chunk.rfind('. ')
        if idx == -1:
            # если нету точки, пробуем искать пробел
            idx = slice_chunk.rfind(' ')
            if idx == -1:
                # тогда режем ровно где лимит
                idx = limit
        else:
            idx += 1  # чтобы точка осталась в чанкe
        chunks.append(text[chunk_start: chunk_start + idx].strip())
        chunk_start += idx

    return [c for c in chunks if c]  # убираем пустые


def build_messages_from_tokens(tokens: list, limit: int = MESSAGE_LIMIT) -> list:
    """
    Получаем список токенов вида (type, content). Собираем итоговые сообщения, 
    НЕ разбивая код ('code'), а тексты ('text') — дробим "смыслово", если они превышают лимит.
    
    Возвращает список строк, каждая не длиннее limit (примерно).
    """
    messages = []
    for ttype, content in tokens:
        if ttype == 'code':
            # Код не дробим - сразу отдельное сообщение
            # Но если блок больше лимита, Teleгram может отвергнуть.
            messages.append(content.strip())
        else:
            # ttype == 'text'
            if len(content) <= limit:
                messages.append(content.strip())
            else:
                # Применяем смысловую разбивку
                parts = split_text_smart(content, limit)
                messages.extend(parts)
    return messages


@dp.message(Command("start"))
async def cmd_start(message: Message):
    greet = (
        "Привет! Я <b>VAI</b> — интеллектуальный помощник.\n\n"
        "Я могу отвечать на самые разные вопросы, делиться фактами, "
        "рассказывать интересное и даже показывать изображения по твоему запросу.\n\n"
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

    # Если пользователь спрашивает имя
    if any(name_trig in user_input.lower() for name_trig in NAME_COMMANDS):
        await message.answer("Меня зовут <b>VAI</b>!")
        return

    # Если спрашивает, кто создал
    if any(info_trig in user_input.lower() for info_trig in INFO_COMMANDS):
        r = random.choice(OWNER_REPLIES)
        await message.answer(r)
        return

    # Сохраняем историю для Gemini
    chat_history.setdefault(cid, []).append({"role": "user", "parts": [user_input]})
    if len(chat_history[cid]) > 5:
        chat_history[cid].pop(0)

    try:
        await bot.send_chat_action(cid, "typing")
        resp = model.generate_content(chat_history[cid])
        gemini_text = format_gemini_response(resp.text)
        logging.info(f"[GEMINI] => {gemini_text[:200]}")

        # Проверяем, запрашивал ли пользователь изображение
        prompt = get_safe_prompt(user_input)
        image_url = await get_unsplash_image_url(prompt, UNSPLASH_ACCESS_KEY)
        triggered = any(t in user_input.lower() for t in IMAGE_TRIGGERS)
        logging.info(f"[BOT] triggered={triggered}, image={image_url}")

        if image_url and triggered:
            # 1) Скачиваем картинку
            async with aiohttp.ClientSession() as sess:
                async with sess.get(image_url) as r:
                    if r.status == 200:
                        photo_bytes = await r.read()
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmpf:
                            tmpf.write(photo_bytes)
                            tmp_path = tmpf.name

                        # 2) Делаем разделение на caption/ leftover
                        caption, leftover_tokens = build_caption_and_rest(gemini_text, CAPTION_LIMIT)

                        try:
                            await bot.send_chat_action(cid, "upload_photo")
                            file = FSInputFile(tmp_path, filename="image.jpg")

                            # Если caption пуст — хоть что-то поставим
                            if not caption.strip():
                                caption = "..."

                            # Отправляем фото + caption
                            await bot.send_photo(cid, file, caption=caption)

                            # 3) leftover_tokens => "смыслово" дробим (если нужно) и шлём сообщениями
                            if leftover_tokens:
                                # Превращаем leftover_tokens в список сообщений (each <= MESSAGE_LIMIT)
                                leftover_messages = build_messages_from_tokens(leftover_tokens, MESSAGE_LIMIT)
                                for msg_chunk in leftover_messages:
                                    await message.answer(msg_chunk)

                        finally:
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)
                        return

        # Если не было картинки или не сработал триггер
        # Отправляем весь текст "смыслово" разбитым, чтобы не превысить MESSAGE_LIMIT
        tokens = parse_html_with_codeblocks(gemini_text)
        splitted_messages = build_messages_from_tokens(tokens, MESSAGE_LIMIT)

        for msg_part in splitted_messages:
            await message.answer(msg_part)

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
