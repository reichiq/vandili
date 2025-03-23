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

# Загружаем переменные окружения (BOT_TOKEN, GEMINI_API_KEY, UNSPLASH_ACCESS_KEY, BOT_USERNAME и т.д.)
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # например: "VAI_Bot" (без @)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Настраиваем доступ к модели Gemini (PaLM, Bard, etc.)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-1.5-pro-latest")

# Храним историю диалога для каждого chat_id
chat_history = {}

# Лимиты Telegram
CAPTION_LIMIT = 950        # Максимум символов для подписи (caption) под фото
TELEGRAM_MSG_LIMIT = 4096  # Примерный максимум символов одного HTML-сообщения

# Триггеры (на русском), которые означают "покажи фото/картинку"
IMAGE_TRIGGERS_RU = [
    "покажи", "покажи мне", "хочу увидеть", "пришли фото", "фото"
]

# Несколько команд/фраз для имени бота
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

# Простейший словарь для RU->EN, чтобы отправить корректный запрос Unsplash
RU_EN_DICT = {
    "обезьян": "monkey",
    "тигр": "tiger",
    "кошка": "cat",
    "собак": "dog",
    "пейзаж": "landscape",
    "чайка": "seagull",
    # Можно продолжать заполнять...
}


def format_gemini_response(text: str) -> str:
    """
    Преобразует текст от Gemini:
      - ```…``` -> <pre><code>…</code></pre>
      - Экранирует HTML-спецсимволы
      - **…** -> <b>…</b>, *…* -> <i>…</i>, `…` -> <code>…</code>
      - Убирает возможные фразы Gemini о том, что "он не может показать изображения"
    """
    code_blocks = {}

    # Тройные бэктики -> <pre><code>…</code></pre>
    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = f'<pre><code class="language-{lang}">{code}</code></pre>'
        return placeholder

    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)

    # Экранируем остальные спецсимволы
    text = escape(text)

    # Возвращаем <pre><code>...</code></pre>
    for placeholder, block_html in code_blocks.items():
        text = text.replace(escape(placeholder), block_html)

    # Обрабатываем **…**, *…*, `…`
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)

    # Убираем фразы про "не могу показывать картинки"
    text = re.sub(r"(Я являюсь текстовым ассистентом.*выводить графику\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I am a text-based model.*cannot directly show images\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I can’t show images directly\.)", "", text, flags=re.IGNORECASE)

    return text.strip()


def split_smart(text: str, limit: int) -> list[str]:
    """
    "Умная" разбивка текста на фрагменты не более limit символов,
    стараясь не обрывать предложения/слова (ищем ". " или хотя бы " ").
    """
    results = []
    start = 0
    length = len(text)

    while start < length:
        remaining = length - start
        if remaining <= limit:
            results.append(text[start:].strip())
            break

        candidate = text[start : start + limit]
        cut_pos = candidate.rfind('. ')
        if cut_pos == -1:
            cut_pos = candidate.rfind(' ')
            if cut_pos == -1:
                # Ни пробела, ни точки — обрезаем жёстко
                cut_pos = len(candidate)
        else:
            # Включим точку, если '. '
            cut_pos += 1

        chunk = text[start : start + cut_pos].strip()
        if chunk:
            results.append(chunk)

        start += cut_pos

    return [x for x in results if x]


def parse_russian_show_request(user_text: str) -> tuple[bool, str, str]:
    """
    Ищем в тексте русское "покажи X" (или "хочу увидеть" и т.п.).
    Возвращаем кортеж:
      ( show_image: bool, image_query_en: str, text_for_gemini: str )

    Пример:
     "покажи обезьяну и расскажи про нее" ->
       -> show_image=True, image_query_en="monkey", text_for_gemini="и расскажи про нее"
    """
    lower_text = user_text.lower()

    # Проверяем, есть ли один из триггеров
    triggered = any(trig in lower_text for trig in IMAGE_TRIGGERS_RU)
    if not triggered:
        return (False, "", user_text)

    # Пытаемся выделить слово после "покажи"/"хочу увидеть"/"пришли фото"
    match = re.search(r"(покажи|хочу увидеть|пришли фото)\s+([\w\d]+)", lower_text)
    if match:
        rus_word = match.group(2)
    else:
        rus_word = ""

    # Убираем "покажи <rus_word>" из исходного текста, чтобы остаток пошёл в Gemini
    pattern_remove = rf"(покажи|хочу увидеть|пришли фото)\s+{rus_word}"
    cleaned_text = re.sub(pattern_remove, "", user_text, flags=re.IGNORECASE).strip()

    # Пробуем найти в словаре RU_EN_DICT
    image_query_en = ""
    for k, v in RU_EN_DICT.items():
        # Пример: k="обезьян", v="monkey", если k in "обезьяну" -> image_query_en="monkey"
        if k in rus_word:
            image_query_en = v
            break

    # Если ничего не нашли, просто используем rus_word как есть (может Unsplash что-нибудь найдёт)
    if not image_query_en:
        image_query_en = rus_word

    return (True, image_query_en, cleaned_text)


async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    """
    Запрос к Unsplash API. Возвращает URL или None.
    """
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


@dp.message(Command("start"))
async def cmd_start(message: Message):
    greet = (
        "Привет! Я <b>VAI</b> — интеллектуальный помощник.\n\n"
        "Я могу отвечать на самые разные вопросы, делиться фактами, "
        "рассказывать интересное и даже показывать изображения по твоему запросу.\n\n"
        "Попробуй, например:\n"
        "• «покажи обезьяну» (получишь фото)\n"
        "• «покажи обезьяну и расскажи про нее пару фактов» (фото и рассказ)\n\n"
        "Всегда рад пообщаться! 🧠✨"
    )
    await message.answer(greet)


@dp.message()
async def handle_msg(message: Message):
    """
    Основной обработчик сообщений.
    1) Если в группе/супергруппе: проверяем упоминание/Reply/ключевые слова для вызова бота.
    2) Обрабатываем команды (имя/автор).
    3) Парсим "покажи X" -> перевести X => запрос к Unsplash.
    4) Остальное -> Gemini.
    5) Отправляем картинку (если удалось), + отправляем ответ Gemini (если есть).
    """

    # --- 1) Если это группа или супергруппа, проверяем, "звали" ли бота ---
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        text_lower = (message.text or "").lower()

        # a) Проверка упоминания @BOT_USERNAME
        mention_bot = False
        if BOT_USERNAME:
            mention_bot = (f"@{BOT_USERNAME.lower()}" in text_lower)

        # b) Проверка Reply на сообщение бота
        is_reply_to_bot = (
            message.reply_to_message
            and message.reply_to_message.from_user
            and (message.reply_to_message.from_user.id == bot.id)
        )

        # c) Проверка упоминания "vai", "вай", "вэй" без @
        mention_keywords = ["vai", "вай", "вэй"]
        mention_by_name = any(keyword in text_lower for keyword in mention_keywords)

        # Если нет ни (mention_bot), ни (reply), ни (mention_by_name), то игнорируем
        if not mention_bot and not is_reply_to_bot and not mention_by_name:
            return

    user_input = message.text.strip()
    cid = message.chat.id
    logging.info(f"[BOT] cid={cid}, text='{user_input}'")

    # --- 2) Проверяем простые команды: имя и автор ---
    low_input = user_input.lower()
    if any(name_trig in low_input for name_trig in NAME_COMMANDS):
        await message.answer("Меня зовут <b>VAI</b>!")
        return
    if any(info_trig in low_input for info_trig in INFO_COMMANDS):
        r = random.choice(OWNER_REPLIES)
        await message.answer(r)
        return

    # --- 3) Разбираем "покажи …" по-русски ---
    show_image, image_en, text_for_gemini = parse_russian_show_request(user_input)

    # --- 4) Если в запросе остался текст после "покажи X" (или вообще не было "покажи") ---
    gemini_text = ""
    text_for_gemini = text_for_gemini.strip()
    if text_for_gemini:
        # Сохраняем в чат-истории
        chat_history.setdefault(cid, []).append({"role": "user", "parts": [text_for_gemini]})
        if len(chat_history[cid]) > 5:
            chat_history[cid].pop(0)

        try:
            await bot.send_chat_action(cid, "typing")
            resp = model.generate_content(chat_history[cid])
            gemini_text = format_gemini_response(resp.text)
        except Exception as e:
            logging.error(f"[BOT] Error from Gemini: {e}")
            gemini_text = f"⚠️ Ошибка при получении ответа от LLM: {escape(str(e))}"

    # --- 5) Если нужно показать картинку, обращаемся к Unsplash ---
    image_url = None
    if show_image and image_en:
        image_url = await get_unsplash_image_url(image_en, UNSPLASH_ACCESS_KEY)

    # --- 6) Отправляем фото, если есть ---
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

                        # Если весь gemini_text <= CAPTION_LIMIT (950), сунем его туда
                        if gemini_text and len(gemini_text) <= CAPTION_LIMIT:
                            # Теоретически может быть риск, если gemini_text близок к 4096
                            # Но обычно caption не вызывает "Message too long".
                            await bot.send_photo(cid, file, caption=gemini_text)
                            gemini_text = ""  # Уже отправили текст
                        else:
                            # Иначе только caption="..."
                            await bot.send_photo(cid, file, caption="...")
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)

    # --- 7) Отправляем остаток текста, если остался ---
    if gemini_text:
        if len(gemini_text) <= TELEGRAM_MSG_LIMIT:
            await message.answer(gemini_text)
        else:
            # Разбиваем "умно" на куски по 4096
            chunks = split_smart(gemini_text, TELEGRAM_MSG_LIMIT)
            for ch in chunks:
                await message.answer(ch)


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
