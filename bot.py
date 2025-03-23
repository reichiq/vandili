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

CAPTION_LIMIT = 950        # Максимум символов для подписи (caption) под фото
TELEGRAM_MSG_LIMIT = 4096  # Примерный максимальный размер одного HTML-сообщения

def format_gemini_response(text: str) -> str:
    """
    Преобразует текст от Gemini:
     - ```…``` -> <pre><code>...</code></pre>
     - экранирует HTML-спецсимволы,
     - **…** -> <b>…</b>, *…* -> <i>…</i>, `…` -> <code>…</code>
    """
    code_blocks = {}

    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        return placeholder

    # 1) Ищем тройные бэктики ```…```
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)
    # 2) Экранируем всё остальное
    text = escape(text)
    # 3) Возвращаем <pre><code>...</code></pre> на место
    for placeholder, block_html in code_blocks.items():
        text = text.replace(escape(placeholder), block_html)
    # 4) **…** / *…* / `…`
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    return text.strip()


def get_safe_prompt(text: str) -> str:
    """
    Для "покажи тигра" -> "тигра". Находит первое разумное слово для Unsplash.
    """
    text = re.sub(r'[.,!?\-\n]', ' ', text.lower())
    match = re.search(r'покажи(?:\s+мне)?\s+(\w+)', text)
    if match:
        return match.group(1)
    return re.sub(r"[^a-zA-Zа-яА-Я0-9\s]", "", text).strip().split(" ")[0]


async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    """
    Вызывает Unsplash API, пытаясь получить рандомное фото.
    Возвращает URL или None.
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


def split_smart(text: str, limit: int) -> list[str]:
    """
    "Умная" разбивка текста на фрагменты не более `limit` символов,
    старается искать ближайшее "`. `" (точка + пробел) или хотя бы пробел `" "` 
    (если не найдёт, режет жёстко).
    
    Примерно такая логика:
    1) Берём кусок в `limit` символов.
    2) В нём ищем rfind('. ') -> если есть, режем тут (с учётом точки).
    3) Если нет '. ', пробуем rfind(' ').
    4) Если нет и пробела — режем жёстко на `limit`.
    5) Добавляем результат в список, идём дальше.
    """
    results = []
    start = 0
    length = len(text)

    while start < length:
        # Остаток текста меньше лимита?
        if (length - start) <= limit:
            # Берём всё целиком
            results.append(text[start:].strip())
            break

        # Иначе берём кандидат длиной limit
        candidate = text[start : start + limit]
        cut_pos = candidate.rfind('. ')
        if cut_pos == -1:
            # Не нашли точку + пробел
            cut_pos = candidate.rfind(' ')
            if cut_pos == -1:
                # Даже пробела нет - придётся рубить жёстко
                cut_pos = len(candidate)
            else:
                # Иначе отсекаем по пробелу
                pass
        else:
            # Нашли '. ', включим саму точку
            cut_pos += 1

        # Берём кусок до cut_pos
        chunk = text[start : start + cut_pos].strip()
        if chunk:
            results.append(chunk)
        # Сдвигаемся вперёд на cut_pos
        start += cut_pos

    # Убираем пустые фрагменты на всякий
    return [r for r in results if r]


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

    # Проверяем, не спрашивает ли имя
    if any(name_trig in user_input.lower() for name_trig in NAME_COMMANDS):
        await message.answer("Меня зовут <b>VAI</b>!")
        return

    # Проверяем, не спрашивает ли автора
    if any(info_trig in user_input.lower() for info_trig in INFO_COMMANDS):
        await message.answer(random.choice(OWNER_REPLIES))
        return

    # Сохраняем в историю для Gemini
    chat_history.setdefault(cid, []).append({"role": "user", "parts": [user_input]})
    if len(chat_history[cid]) > 5:
        chat_history[cid].pop(0)

    try:
        await bot.send_chat_action(cid, "typing")
        # Запрашиваем текст у Gemini
        resp = model.generate_content(chat_history[cid])
        gemini_text = format_gemini_response(resp.text)
        logging.info(f"[GEMINI] => {gemini_text[:200]}")

        # Проверяем, нужна ли картинка
        prompt = get_safe_prompt(user_input)
        image_url = await get_unsplash_image_url(prompt, UNSPLASH_ACCESS_KEY)
        triggered = any(t in user_input.lower() for t in IMAGE_TRIGGERS)
        logging.info(f"[BOT] triggered={triggered}, image={image_url}")

        # Если картинка найдена и пользователь её просил
        if image_url and triggered:
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

                            # Уместим ли весь текст в caption?
                            if len(gemini_text) <= CAPTION_LIMIT:
                                # Целиком идёт в caption
                                if len(gemini_text) <= TELEGRAM_MSG_LIMIT:
                                    # И точно не превысит лимита одного сообщения
                                    await bot.send_photo(cid, file, caption=gemini_text)
                                else:
                                    # Если вдруг текст (даже при 950) > 4096, бывает редко
                                    # Но чисто теоретически: тогда отправим фото + caption, а лишнее - нет
                                    # или можно выбросить ошибку
                                    chunks = split_smart(gemini_text, TELEGRAM_MSG_LIMIT)
                                    # Первый кусок (точно влезает, раз len(gemini_text)<=950)
                                    await bot.send_photo(cid, file, caption=chunks[0])
                                    # Остальные куски отдельно
                                    for ch in chunks[1:]:
                                        await message.answer(ch)

                            else:
                                # Текст не влезает в caption => ставим '…'
                                await bot.send_photo(cid, file, caption="…")
                                # И отправляем полный текст по "умной" разбивке, чтобы не превысить 4096
                                if len(gemini_text) <= TELEGRAM_MSG_LIMIT:
                                    await message.answer(gemini_text)
                                else:
                                    chunks = split_smart(gemini_text, TELEGRAM_MSG_LIMIT)
                                    for ch in chunks:
                                        await message.answer(ch)

                        finally:
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)

                        return

        # Если картинки нет или пользователь не просил
        # Просто отправляем "умно" разбитый текст (не более 4096 символов за раз)
        if len(gemini_text) <= TELEGRAM_MSG_LIMIT:
            await message.answer(gemini_text)
        else:
            chunks = split_smart(gemini_text, TELEGRAM_MSG_LIMIT)
            for ch in chunks:
                await message.answer(ch)

    except Exception as e:
        logging.error(f"[BOT] Error: {e}")
        await message.answer(f"❌ Ошибка: {escape(str(e))}")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
