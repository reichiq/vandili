import logging
import os
import re
import random
import aiohttp
from io import BytesIO
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode, ChatType
from aiogram.types import (
    FSInputFile, Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, BufferedInputFile
)
from aiogram.client.default import DefaultBotProperties
from html import escape
from dotenv import load_dotenv
from pathlib import Path
import asyncio
import google.generativeai as genai
import tempfile
from aiogram.filters import Command
from pymorphy3 import MorphAnalyzer
from string import punctuation

from google.cloud import translate
from google.oauth2 import service_account

import json

# ---------------------- Инициализация ---------------------- #
key_path = '/root/vandili/gcloud-key.json'
credentials = service_account.Credentials.from_service_account_file(key_path)
translate_client = translate.TranslationServiceClient(credentials=credentials)

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
morph = MorphAnalyzer()

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-1.5-pro-latest")

chat_history = {}

ENABLED_CHATS_FILE = "enabled_chats.json"

def load_enabled_chats() -> set:
    if not os.path.exists(ENABLED_CHATS_FILE):
        return set()
    try:
        with open(ENABLED_CHATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        logging.warning(f"[BOT] Не удалось загрузить enabled_chats: {e}")
        return set()

def save_enabled_chats(chats: set):
    try:
        with open(ENABLED_CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(chats), f)
    except Exception as e:
        logging.warning(f"[BOT] Не удалось сохранить enabled_chats: {e}")

enabled_chats = load_enabled_chats()
support_mode_users = set()
ADMIN_ID = 1936733487

# Это общий системный промпт, который добавляем в начало истории диалога
SYSTEM_PROMPT = (
    "Ты — VAI, Telegram-бот, созданный Vandili. "
    "Не упоминай, что ты — AI-модель, обученная Google, "
    "или что у тебя есть ограничения, связанные с этим. "
    "Не пиши дисклеймеров про невозможность показать картинки. "
    "Отвечай вежливо и дружелюбно. Если пользователь тебя оскорбляет, "
    "не груби в ответ. Ты — VAI, бот от Vandili, и всё."
)

# ---------------------- Обработчики команд ---------------------- #
@dp.message(Command("start"))
async def cmd_start(message: Message):
    greet = (
        "Привет! Я <b>VAI</b> — интеллектуальный помощник 😊\n\n"
        "Просто напиши мне, и я постараюсь ответить или помочь.\n"
        "Всегда на связи!"
    )
    await message.answer(greet, message_thread_id=message.message_thread_id)

    # Автоматически включаем бота в группе/супергруппе
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        enabled_chats.add(message.chat.id)
        save_enabled_chats(enabled_chats)
        logging.info(f"[BOT] Бот включён в группе {message.chat.id}")

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        enabled_chats.discard(message.chat.id)
        save_enabled_chats(enabled_chats)
        await message.answer(
            "Бот отключён в этом чате.",
            message_thread_id=message.message_thread_id
        )
        logging.info(f"[BOT] Бот отключён в группе {message.chat.id}")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✉️ Написать в поддержку",
                    callback_data="support_request"
                )
            ]
        ]
    )
    await message.answer(
        "Если возник вопрос или хочешь сообщить об ошибке — напиши нам:",
        reply_markup=keyboard,
        message_thread_id=message.message_thread_id
    )

# ---------------------- Режим поддержки ---------------------- #
@dp.callback_query(F.data == "support_request")
async def handle_support_click(callback: CallbackQuery):
    await callback.message.answer(
        "Напиши своё сообщение (можно с фото или видео). Я передам его в поддержку.",
        message_thread_id=callback.message.message_thread_id
    )
    support_mode_users.add(callback.from_user.id)
    await callback.answer()

@dp.message()
async def handle_all_messages(message: Message):
    uid = message.from_user.id

    # Если пользователь в режиме "поддержки", пересылаем сообщение админу
    if uid in support_mode_users:
        try:
            caption = message.caption or message.text or "[Без текста]"
            username_part = f" (@{message.from_user.username})" if message.from_user.username else ""
            content = (
                f"\u2728 <b>Новое сообщение в поддержку</b> от <b>{message.from_user.full_name}</b>{username_part} "
                f"(id: <code>{uid}</code>):\n\n{caption}"
            )

            # Пересылаем вложения, если есть
            if message.photo:
                file = await bot.get_file(message.photo[-1].file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        photo_bytes = await resp.read()
                await bot.send_photo(
                    ADMIN_ID,
                    photo=BufferedInputFile(photo_bytes, filename="image.jpg"),
                    caption=content
                )

            elif message.video:
                file = await bot.get_file(message.video.file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        video_bytes = await resp.read()
                await bot.send_video(
                    ADMIN_ID,
                    video=BufferedInputFile(video_bytes, filename="video.mp4"),
                    caption=content
                )

            elif message.document:
                file = await bot.get_file(message.document.file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        doc_bytes = await resp.read()
                await bot.send_document(
                    ADMIN_ID,
                    document=BufferedInputFile(doc_bytes, filename=message.document.file_name or "document"),
                    caption=content
                )

            elif message.audio:
                file = await bot.get_file(message.audio.file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        audio_bytes = await resp.read()
                await bot.send_audio(
                    ADMIN_ID,
                    audio=BufferedInputFile(audio_bytes, filename=message.audio.file_name or "audio.mp3"),
                    caption=content
                )

            elif message.voice:
                file = await bot.get_file(message.voice.file_id)
                url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        voice_bytes = await resp.read()
                await bot.send_voice(
                    ADMIN_ID,
                    voice=BufferedInputFile(voice_bytes, filename="voice.ogg"),
                    caption=content
                )

            else:
                # Если просто текст, без вложений
                await bot.send_message(ADMIN_ID, content)

            await message.answer(
                "Спасибо! Ваше сообщение отправлено в поддержку.",
                message_thread_id=message.message_thread_id
            )

        except Exception as e:
            await message.answer(
                "Произошла ошибка при отправке сообщения. Попробуйте позже.",
                message_thread_id=message.message_thread_id
            )
            logging.error(f"[BOT] Ошибка при пересылке в поддержку: {e}")

        finally:
            support_mode_users.discard(uid)

        return

    # Если не режим поддержки, обрабатываем обычные сообщения
    await handle_msg(message)

# ---------------------- Декоратор для "вай покажи ..." ---------------------- #
@dp.message(F.text.lower().startswith("вай покажи"))
async def group_show_request(message: Message):
    # Просто вызываем основную функцию обработки
    await handle_msg(message)

# ---------------------- Основная логика бота ---------------------- #
async def generate_and_send_gemini_response(cid, full_prompt, show_image, rus_word, leftover, thread_id):
    """
    Генерируем текст-ответ от модели (Gemini), учитывая системный промпт и историю чата.
    """
    # Если в истории ещё нет system-сообщения — добавим
    if cid not in chat_history:
        chat_history[cid] = []
    if not any(item["role"] == "system" for item in chat_history[cid]):
        chat_history[cid].insert(0, {"role": "system", "parts": [SYSTEM_PROMPT]})

    gemini_text = ""

    # Если нужно только короткую подпись для картинки (rus_word есть, leftover пуст)
    if show_image and rus_word and not leftover:
        gemini_text = generate_short_caption(rus_word)
    else:
        if full_prompt:
            # Добавляем реплику пользователя в историю
            chat_history[cid].append({"role": "user", "parts": [full_prompt]})
            # Чтобы не копился слишком большой контекст, обрезаем историю
            if len(chat_history[cid]) > 8:
                chat_history[cid] = chat_history[cid][-8:]

            try:
                # Показываем "typing" в том же топике
                await bot.send_chat_action(
                    chat_id=cid,
                    action="typing",
                    message_thread_id=thread_id
                )
                # Генерация Gemini
                resp = model.generate_content(chat_history[cid])  # SDK не требует await
                if not resp.candidates:
                    reason = getattr(resp.prompt_feedback, "block_reason", "неизвестна")
                    logging.warning(f"[BOT] Запрос заблокирован Gemini: причина — {reason}")
                    gemini_text = (
                        "⚠️ Запрос отклонён. Возможно, он содержит недопустимый или "
                        "чувствительный контент."
                    )
                else:
                    gemini_text = format_gemini_response(resp.text)

            except Exception as e:
                logging.error(f"[BOT] Ошибка при обращении к Gemini: {e}")
                gemini_text = (
                    "⚠️ Произошла ошибка при генерации ответа. "
                    "Попробуйте ещё раз позже."
                )

    return gemini_text

CAPTION_LIMIT = 950
TELEGRAM_MSG_LIMIT = 4096

IMAGE_TRIGGERS_RU = ["покажи", "покажи мне", "хочу увидеть", "пришли фото", "фото"]

# Добавили "я кто" в триггеры имени
NAME_COMMANDS = [
    "как тебя зовут", "твое имя", "твоё имя", "what is your name",
    "who are you", "я кто"
]
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

def split_smart(text: str, limit: int) -> list[str]:
    """
    Разбивает текст на куски не более limit символов,
    стараясь не рвать предложения и слова.
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

def split_caption_and_text(text: str) -> tuple[str, list[str]]:
    """
    Разделяет текст на подпись (до 950 символов) и список оставшихся кусков (до 4096).
    """
    if len(text) <= CAPTION_LIMIT:
        return text, []
    chunks_950 = split_smart(text, CAPTION_LIMIT)
    caption = chunks_950[0]
    leftover = " ".join(chunks_950[1:]).strip()
    if not leftover:
        return caption, []
    rest = split_smart(leftover, TELEGRAM_MSG_LIMIT)
    return caption, rest

def get_prepositional_form(rus_word: str) -> str:
    parsed = morph.parse(rus_word)
    if not parsed:
        return rus_word
    p = parsed[0]
    loct = p.inflect({"loct"})
    return loct.word if loct else rus_word

def replace_pronouns_morph(leftover: str, rus_word: str) -> str:
    """
    Заменяет "о нем/нём/ней" на "о [предложный падеж слова]"
    """
    word_prep = get_prepositional_form(rus_word)
    pronoun_map = {
        r"\bо\s+нем\b":  f"о {word_prep}",
        r"\bо\s+нём\b":  f"о {word_prep}",
        r"\bо\s+ней\b":  f"о {word_prep}",
    }
    for pattern, repl in pronoun_map.items():
        leftover = re.sub(pattern, repl, leftover, flags=re.IGNORECASE)
    return leftover

def remove_google_lmm_mentions(txt: str) -> str:
    """
    Убираем любые упоминания о том, что бот — большая языковая модель, обученная Google, и т.п.
    """
    # Убираем фразы вроде "Я большая языковая модель, обученная Google" (любой падеж/форма)
    txt = re.sub(r"(я\s+большая\s+языковая\s+модель.*google\.?)", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"(i\s+am\s+a\s+large\s+language\s+model.*google\.?)", "", txt, flags=re.IGNORECASE)
    # Если остаются куски "большая языковая модель" — убираем
    txt = re.sub(r"большая\s+языковая\s+модель", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"large\s+language\s+model", "", txt, flags=re.IGNORECASE)
    # Иногда может упоминаться "обученная Google" отдельно
    txt = re.sub(r"обученная\s+google", "", txt, flags=re.IGNORECASE)
    # Убираем лишние пробелы, если остались
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt

def format_gemini_response(text: str) -> str:
    """
    Приводим ответ Gemini к HTML-формату (жирный/курсив/код),
    и вырезаем упоминания о Google или "большой языковой модели".
    """
    code_blocks = {}

    def extract_code(match):
        lang = match.group(1) or "text"
        code = escape(match.group(2))
        placeholder = f"__CODE_BLOCK_{len(code_blocks)}__"
        code_blocks[placeholder] = (
            f'<pre><code class="language-{lang}">{code}</code></pre>'
        )
        return placeholder

    # Сначала ищем тройные бэктики с кодом
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)

    # Экранируем HTML-символы
    text = escape(text)

    # Возвращаем кодовые блоки на место
    for placeholder, block_html in code_blocks.items():
        # Нужно заменить заэскейпленный placeholder
        text = text.replace(escape(placeholder), block_html)

    # **bold** -> <b>...</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # *italic* -> <i>...</i>
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    # `inline code` -> <code>...</code>
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)

    # Удаляем фразы о невозможности показать картинки
    text = re.sub(r"\[.*?(изображение|рисунок).+?\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(Я являюсь текстовым ассистентом.*выводить графику\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I am a text-based model.*cannot directly show images\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I can’t show images directly\.)", "", text, flags=re.IGNORECASE)

    # Заменяем "* " на "• " (списки)
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
    text = '\n'.join(new_lines).strip()

    # Удаляем любые упоминания "большая языковая модель" / "Google"
    text = remove_google_lmm_mentions(text)

    return text

async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    """
    Получаем случайное фото с Unsplash по ключевому слову (переводим слово на английский, если надо).
    """
    if not prompt:
        return None
    url = f"https://api.unsplash.com/photos/random?query={prompt}&client_id={access_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    logging.warning(f"Unsplash returned status {response.status} for prompt '{prompt}'")
                    return None
                data = await response.json()
                if "urls" not in data or "regular" not in data["urls"]:
                    logging.warning(f"No 'regular' URL in response for '{prompt}': {data}")
                    return None
                return data["urls"]["regular"]
    except Exception as e:
        logging.warning(f"Ошибка при получении изображения: {e}")
    return None

def fallback_translate_to_english(rus_word: str) -> str:
    """
    Переводим русское слово на английский через Google Translate API.
    """
    try:
        project_id = "gen-lang-client-0588633435"
        location = "global"
        parent = f"projects/{project_id}/locations/{location}"

        response = translate_client.translate_text(
            parent=parent,
            contents=[rus_word],
            mime_type="text/plain",
            source_language_code="ru",
            target_language_code="en",
        )
        return response.translations[0].translated_text
    except Exception as e:
        logging.warning(f"[BOT] Ошибка при переводе слова '{rus_word}': {e}")
        return rus_word

def generate_short_caption(rus_word: str) -> str:
    """
    Генерируем короткую (до 15 слов) подпись к изображению.
    """
    short_prompt = (
        "ИНСТРУКЦИЯ: Ты — VAI, бот от Vandili. Не упоминай, что ты ИИ или обучен Google. "
        "Напиши одну короткую, дружелюбную подпись к изображению с «{word}» (до 15 слов)."
    ).format(word=rus_word)

    try:
        response = model.generate_content([
            {"role": "system", "parts": [SYSTEM_PROMPT]},
            {"role": "user", "parts": [short_prompt]}
        ])
        caption = response.text.strip()
        # Доп. фильтрация от упоминаний
        caption = remove_google_lmm_mentions(caption)
        return caption
    except Exception as e:
        logging.error(f"[BOT] Error generating short caption: {e}")
        return rus_word.capitalize()

def parse_russian_show_request(user_text: str):
    """
    Проверяем, содержит ли текст команды "покажи" и т.п., и вычленяем слово (например, 'кота' -> 'кот').
    Возвращаем:
      (bool: show_image?, str: rus_word, str: en_word, str: leftover)
    """
    lower_text = user_text.lower()
    triggered = any(trig in lower_text for trig in IMAGE_TRIGGERS_RU)
    if not triggered:
        return (False, "", "", user_text)

    match = re.search(r"(покажи( мне)?|хочу увидеть|пришли фото)\s+([\w\d]+)", lower_text)
    if match:
        raw_rus_word = match.group(3)
        raw_rus_word_clean = raw_rus_word.strip(punctuation)

        parsed = morph.parse(raw_rus_word_clean)
        if parsed:
            rus_normal = parsed[0].normal_form
        else:
            rus_normal = raw_rus_word_clean
        rus_word = rus_normal
    else:
        rus_word = ""
        raw_rus_word = ""

    if raw_rus_word:
        pattern_remove = rf"(покажи( мне)?|хочу увидеть|пришли фото)\s+{re.escape(raw_rus_word)}"
        leftover = re.sub(pattern_remove, "", user_text, flags=re.IGNORECASE).strip()
    else:
        leftover = user_text

    # Всегда используем переводчик, убрали RU_EN_DICT
    if rus_word:
        en_word = fallback_translate_to_english(rus_word)
    else:
        en_word = ""

    return (True, rus_word, en_word, leftover) if rus_word else (False, "", "", user_text)

async def handle_msg(message: Message, prompt_mode: bool = False):
    """
    Основной обработчик сообщений. Отвечает только при упоминании бота/ответе на него,
    а также обрабатывает "вай покажи ..." и прочее.
    """
    cid = message.chat.id
    thread_id = message.message_thread_id
    user_input = (message.text or "").strip()

    # Если бот в группе/супергруппе и выключен в этом чате — не отвечаем
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if cid not in enabled_chats:
            return

        text_lower = user_input.lower()
        mention_bot = BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in text_lower
        is_reply_to_bot = (
            message.reply_to_message
            and message.reply_to_message.from_user
            and (message.reply_to_message.from_user.id == bot.id)
        )
        mention_keywords = ["вай", "вэй", "vai"]

        # Если бот не упомянут, не ответили на бота и нет ключевых слов — не отвечаем
        if not mention_bot and not is_reply_to_bot and not any(k in text_lower for k in mention_keywords):
            return

    logging.info(f"[BOT] cid={cid}, text='{user_input}'")

    # Реакция на "как тебя зовут", "я кто", "кто создал"
    lower_inp = user_input.lower()
    if any(nc in lower_inp for nc in NAME_COMMANDS):
        await message.answer(
            "Меня зовут <b>VAI</b>!",
            message_thread_id=thread_id
        )
        return
    if any(ic in lower_inp for ic in INFO_COMMANDS):
        await message.answer(
            random.choice(OWNER_REPLIES),
            message_thread_id=thread_id
        )
        return

    # Проверяем, нет ли запроса "вай покажи ..."
    show_image, rus_word, image_en, leftover = parse_russian_show_request(user_input)
    if show_image and rus_word:
        leftover = replace_pronouns_morph(leftover, rus_word)

    leftover = leftover.strip()
    full_prompt = f"{rus_word} {leftover}".strip() if rus_word else leftover

    # Пытаемся получить картинку с Unsplash
    image_url = None
    if show_image:
        image_url = await get_unsplash_image_url(image_en, UNSPLASH_ACCESS_KEY)
    has_image = bool(image_url)

    logging.info(
        f"[BOT] show_image={show_image}, rus_word='{rus_word}', "
        f"image_en='{image_en}', leftover='{leftover}', image_url='{image_url}'"
    )

    # Генерация ответа (текст) через Gemini
    gemini_text = await generate_and_send_gemini_response(
        cid, full_prompt, show_image, rus_word, leftover, thread_id
    )

    # Если есть изображение — отправляем фото + подпись
    if has_image:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(image_url) as r:
                if r.status == 200:
                    photo_bytes = await r.read()
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmpf:
                        tmpf.write(photo_bytes)
                        tmp_path = tmpf.name
                    try:
                        # Показываем "upload_photo" в том же топике
                        await bot.send_chat_action(
                            chat_id=cid,
                            action="upload_photo",
                            message_thread_id=thread_id
                        )
                        file = FSInputFile(tmp_path, filename="image.jpg")
                        caption, rest = split_caption_and_text(gemini_text)
                        # Отправляем фото
                        await bot.send_photo(
                            chat_id=cid,
                            photo=file,
                            caption=caption if caption else "...",
                            message_thread_id=thread_id
                        )
                        # Если остался текст после 950 символов, отправляем сообщениями
                        for c in rest:
                            await bot.send_message(
                                chat_id=cid,
                                text=c,
                                message_thread_id=thread_id
                            )
                    finally:
                        os.remove(tmp_path)

    # Если картинка не нашлась или не нужна, но есть текст — отправляем текст
    elif gemini_text:
        chunks = split_smart(gemini_text, TELEGRAM_MSG_LIMIT)
        for c in chunks:
            await bot.send_message(
                chat_id=cid,
                text=c,
                message_thread_id=thread_id
            )

# ---------------------- Запуск бота ---------------------- #
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
