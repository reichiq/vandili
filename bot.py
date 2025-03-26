# ---------------------- Импорты ---------------------- #
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
    CallbackQuery, InputFile, BufferedInputFile
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

from docx import Document
from PyPDF2 import PdfReader
import json

# ---------------------- Вспомогательная функция для чтения файлов ---------------------- #
def extract_text_from_file(filename: str, file_bytes: bytes) -> str:
    if filename.endswith(".txt") or filename.endswith(".py"):
        return file_bytes.decode("utf-8", errors="ignore")
    elif filename.endswith(".pdf"):
        try:
            with BytesIO(file_bytes) as pdf_stream:
                reader = PdfReader(pdf_stream)
                return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return ""
    elif filename.endswith(".docx"):
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmpf:
                tmpf.write(file_bytes)
                tmp_path = tmpf.name
            doc = Document(tmp_path)
            os.remove(tmp_path)
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception:
            return ""
    return ""

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
model = genai.GenerativeModel(model_name="models/gemini-2.0-flash")

chat_history = {}
user_documents = {}

ENABLED_CHATS_FILE = "enabled_chats.json"
ADMIN_ID = 1936733487

SUPPORT_PROMPT_TEXT = (
    "Отправьте любое сообщение (текст, фото, видео, файлы, аудио, голосовые) — всё дойдёт до поддержки."
)

def thread_kwargs(message: Message) -> dict:
    """
    Если это супергруппа/группа с топиками, вернём словарь
    {"message_thread_id": ...}, иначе пусто.
    """
    if (
        message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]
        and message.message_thread_id is not None
    ):
        return {"message_thread_id": message.message_thread_id}
    return {}

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

# ---------------------- Обработчики команд ---------------------- #
@dp.message(Command("start"))
async def cmd_start(message: Message):
    # Если пользователь пришёл по ссылке /start support (например, из группы)
    if message.chat.type == ChatType.PRIVATE and message.text.startswith("/start support"):
        support_mode_users.add(message.from_user.id)
        await message.answer(SUPPORT_PROMPT_TEXT)
        return

    # Обычный старт
    greet = """Привет! Я <b>VAI</b> — интеллектуальный помощник 😊

Вот что я умею:
• Читаю PDF, DOCX, TXT и .py-файлы — просто отправь мне файл.
• Отвечаю на вопросы по содержимому файла.
• Помогаю с кодом — напиши #рефактор и вставь код.
• Показываю изображения по ключевым словам.
• Поддерживаю команды /help и режим поддержки.

Всегда на связи!"""
    await bot.send_message(
        chat_id=message.chat.id,
        text=greet,
        **thread_kwargs(message)
    )

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
        await bot.send_message(
            chat_id=message.chat.id,
            text="Бот отключён в этом чате.",
            **thread_kwargs(message)
        )
        logging.info(f"[BOT] Бот отключён в группе {message.chat.id}")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        # В личке — колбэк-кнопка
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(
                text="✉️ Написать в поддержку",
                callback_data="support_request"
            )]]
        )
        await bot.send_message(
            chat_id=message.chat.id,
            text="Если возник вопрос или хочешь сообщить об ошибке — напиши нам:",
            reply_markup=keyboard
        )
    else:
        # В группе — ссылка на ЛС с intent для поддержки
        private_url = f"https://t.me/{BOT_USERNAME}?start=support"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(
                text="✉️ Написать в поддержку",
                url=private_url
            )]]
        )
        await bot.send_message(
            chat_id=message.chat.id,
            text="Если возник вопрос или хочешь сообщить об ошибке — напиши мне в личку:",
            reply_markup=keyboard,
            **thread_kwargs(message)
        )

# ---------------------- Режим поддержки (callback) ---------------------- #
@dp.callback_query(F.data == "support_request")
async def handle_support_click(callback: CallbackQuery):
    """
    Срабатывает только в ЛС, потому что в группе у нас URL-кнопка.
    """
    # Закрываем колбэк, чтобы не было «вечной загрузки»
    await callback.answer()

    # Включаем режим поддержки
    support_mode_users.add(callback.from_user.id)
    await callback.message.answer(SUPPORT_PROMPT_TEXT)

@dp.message()
async def handle_all_messages(message: Message):
    uid = message.from_user.id

    # 1. Если пользователь в режиме поддержки — пересылаем сообщение админу и сообщаем о пересылке
    if uid in support_mode_users:
        support_mode_users.discard(uid)  # Отключаем режим поддержки
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
                    chat_id=ADMIN_ID,
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
                    chat_id=ADMIN_ID,
                    video=BufferedInputFile(video_bytes, filename="video.mp4"),
                    caption=content
                )
            else:
                await bot.send_message(chat_id=ADMIN_ID, text=content)
            
            # Отправляем подтверждение пользователю
            await message.answer("Сообщение отправлено в поддержку.")

        except Exception as e:
            logging.warning(f"[BOT] Ошибка при пересылке в поддержку: {e}")
            await message.answer("Произошла ошибка при отправке сообщения в поддержку.")

        return  # Выход из функции – не обрабатываем дальше

    # 2. Если это файл — читаем его и сохраняем
    if message.document:
        file = await bot.get_file(message.document.file_id)
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                file_bytes = await resp.read()
        text = extract_text_from_file(message.document.file_name, file_bytes)
        if text:
            user_documents[uid] = text
            await message.answer("✅ Файл получен! Можешь задать вопрос по его содержимому.")
        else:
            await message.answer("⚠️ Не удалось извлечь текст из файла.")
        return  # После файла – тоже выходим

    # 3. Логируем
    logging.info(f"[DEBUG] Message from {uid}: content_type={message.content_type}, has_document={bool(message.document)}, text={message.text!r}")

    # 4. Обычная обработка сообщений
    await handle_msg(message)

# ---------------------- Дополнительный декоратор для "вай покажи ..." ---------------------- #
@dp.message(F.text.lower().startswith("вай покажи"))
async def group_show_request(message: Message):
    # Просто вызываем основную функцию обработки
    await handle_msg(message)

# ---------------------- Логика бота / генерация ответа Gemini ---------------------- #
async def generate_and_send_gemini_response(cid, full_prompt, show_image, rus_word, leftover):
    gemini_text = ""

    # Анализ вопроса: если сложный — усиливаем запрос
    analysis_keywords = [
        "почему", "зачем", "на кого", "кто", "что такое", "влияние",
        "философ", "отрицал", "повлиял", "смысл", "экзистенциализм", "опроверг"
    ]
    needs_expansion = any(k in full_prompt.lower() for k in analysis_keywords)
    if needs_expansion:
        smart_prompt = (
            "Ответь чётко и по делу. Если в вопросе несколько частей — ответь на каждую. "
            "Приводи имена и конкретные примеры, если они есть. Не повторяй вопрос, просто ответь:\n\n"
        )
        full_prompt = smart_prompt + full_prompt

    # Короткая подпись к картинке
    if show_image and rus_word and not leftover:
        gemini_text = generate_short_caption(rus_word)
        return gemini_text

    if full_prompt:
        chat_history.setdefault(cid, []).append({"role": "user", "parts": [full_prompt]})
        if len(chat_history[cid]) > 5:
            chat_history[cid].pop(0)

        try:
            await bot.send_chat_action(chat_id=cid, action="typing")
            resp = model.generate_content(chat_history[cid])
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

NAME_COMMANDS = [
    "как тебя зовут", "твое имя", "твоё имя", "what is your name", "who are you"
]
INFO_COMMANDS = [
    "кто тебя создал", "кто ты", "кто разработчик", "кто твой автор",
    "кто твой создатель", "чей ты бот", "кем ты был создан",
    "кто хозяин", "кто твой владелец", "в смысле кто твой создатель"
]

OWNER_REPLIES = [
    "Я — <b>VAI</b>, создан командой <i>Vandili</i> 😎",
    "Мой создатель — <b>Vandili</b>. Я работаю для них 😉",
    "Я принадлежу <i>Vandili</i>, они моя команда ✨",
    "Создан <b>Vandili</b> — именно они дали мне жизнь 🤝",
    "Я бот <b>Vandili</b>. Всё просто 🤗",
    "Я продукт <i>Vandili</i>. Они мои создатели 😇"
]

RU_EN_DICT = {
    "обезьяна": "monkey",
    "тигр": "tiger",
    "кошка": "cat",
    "собака": "dog",
    "пейзаж": "landscape",
    "чайка": "seagull",
    "париж": "paris",
    "утконос": "platypus",
    "пудель": "poodle",
    "медоед": "honey badger"
}

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

def format_gemini_response(text: str) -> str:
    """
    Приводим ответ Gemini к HTML-формату (жирный/курсив/код),
    и вырезаем/заменяем любые упоминания о том, что бот — от Google.
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

    # 1. Вырезаем тройные бэктики с кодом
    text = re.sub(r"```(\w+)?\n([\s\S]+?)```", extract_code, text)

    # 2. Экранируем HTML-символы
    text = escape(text)

    # 3. Возвращаем кодовые блоки на место
    for placeholder, block_html in code_blocks.items():
        text = text.replace(escape(placeholder), block_html)

    # 4. **bold** -> <b>...</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # *italic* -> <i>...</i>
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    # `inline code` -> <code>...</code>
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)

    # 5. Удаляем лишние фразы о том, что ИИ не может показывать картинки
    text = re.sub(r"\[.*?(изображение|рисунок).+?\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(Я являюсь текстовым ассистентом.*выводить графику\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I am a text-based model.*cannot directly show images\.)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(I can’t show images directly\.)", "", text, flags=re.IGNORECASE)

    # 6. Заменяем "* " на "• " (списки)
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

    # 7. Убираем любые упоминания, что бот от Google,
    #    и заменяем «я большая языковая модель» на «Я VAI, создан командой Vandili»
    text = re.sub(r"(?i)\bi am a large language model\b", "I am VAI, created by Vandili", text)
    text = re.sub(r"(?i)\bi'm a large language model\b", "I'm VAI, created by Vandili", text)
    text = re.sub(r"(?i)\bgoogle\b", "Vandili", text)

    # Русские варианты
    text = re.sub(r"я большая языковая модель(?:.*?)(?=\.)", "Я VAI, создан командой Vandili", text, flags=re.IGNORECASE)
    text = re.sub(r"я большая языковая модель", "Я VAI, создан командой Vandili", text, flags=re.IGNORECASE)
    text = re.sub(r"я\s*—\s*большая языковая модель", "Я — VAI, создан командой Vandili", text, flags=re.IGNORECASE)

    return text

async def get_unsplash_image_url(prompt: str, access_key: str) -> str:
    """
    Получаем случайное фото с Unsplash по ключевому слову.
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
    Если слова нет в словаре RU_EN_DICT, пробуем перевести через Google Translate API.
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
        logging.warning(f"Ошибка при переводе слова '{rus_word}': {e}")
        return rus_word

def generate_short_caption(rus_word: str) -> str:
    """
    Генерируем короткую (до 15 слов) подпись к изображению.
    """
    short_prompt = (
        "ИНСТРУКЦИЯ: Ты — творческий помощник, который умеет писать очень короткие, дружелюбные подписи "
        "на русском языке. Не упоминай, что ты ИИ или Google. Старайся не превышать 15 слов.\n\n"
        f"ЗАДАЧА: Придумай одну короткую, дружелюбную подпись для картинки с «{rus_word}». "
        "Можно с лёгкой эмоцией или юмором, не более 15 слов."
    )
    try:
        response = model.generate_content([
            {
                "role": "user",
                "parts": [short_prompt]
            }
        ])
        caption = format_gemini_response(response.text.strip())
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

    if rus_word in RU_EN_DICT:
        en_word = RU_EN_DICT[rus_word]
    else:
        en_word = fallback_translate_to_english(rus_word)

    return (True, rus_word, en_word, leftover) if rus_word else (False, "", "", user_text)

async def handle_msg(message: Message, prompt_mode: bool = False):
    """
    Основной обработчик сообщений. Отвечает только при упоминании бота/ответе на него,
    а также обрабатывает "вай покажи ..." и прочее.
    """
    cid = message.chat.id
    user_input = (message.text or "").strip()
    # Ответ на вопрос по содержимому ранее загруженного файла
    if "файл" in user_input.lower() and message.from_user.id in user_documents:
        text = user_documents[message.from_user.id]
        short_summary_prompt = (
            "Кратко и по делу объясни, что делает этот код или что содержится в этом файле. "
            "Изложи это для пользователя, который только что загрузил файл:\n\n"
            f"{text}"
        )
        gemini_response = await generate_and_send_gemini_response(
            cid, short_summary_prompt, False, "", ""
        )
        await bot.send_message(chat_id=cid, text=gemini_response, **thread_kwargs(message))
        return

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

    # Реакция на "как тебя зовут" и "кто создал"
    lower_inp = user_input.lower()
    if any(nc in lower_inp for nc in NAME_COMMANDS):
        await bot.send_message(
            chat_id=cid,
            text="Меня зовут <b>VAI</b>! 🤖",
            **thread_kwargs(message)
        )
        return
    if any(ic in lower_inp for ic in INFO_COMMANDS):
        await bot.send_message(
            chat_id=cid,
            text=random.choice(OWNER_REPLIES),
            **thread_kwargs(message)
        )
        return

    # Проверяем, нет ли запроса "вай покажи ..."
    show_image, rus_word, image_en, leftover = parse_russian_show_request(user_input)
    if show_image and rus_word:
        # Если в leftover осталось "вай" (или "vai") как отдельное слово — убираем
        leftover = re.sub(r"\b(вай|vai)\b", "", leftover, flags=re.IGNORECASE).strip()
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
        cid, full_prompt, show_image, rus_word, leftover
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
                        await bot.send_chat_action(chat_id=cid, action="upload_photo", **thread_kwargs(message))
                        file = FSInputFile(tmp_path, filename="image.jpg")
                        caption, rest = split_caption_and_text(gemini_text)
                        await bot.send_photo(
                            chat_id=cid,
                            photo=file,
                            caption=caption if caption else "...",
                            **thread_kwargs(message)
                        )
                        for c in rest:
                            await bot.send_message(chat_id=cid, text=c, **thread_kwargs(message))
                    finally:
                        os.remove(tmp_path)
    elif gemini_text:
        chunks = split_smart(gemini_text, TELEGRAM_MSG_LIMIT)
        for c in chunks:
            await bot.send_message(chat_id=cid, text=c, **thread_kwargs(message))

# ---------------------- Запуск бота ---------------------- #
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


# Повторное определение extract_text_from_file (если нужно, можно удалить дубликат)
from docx import Document
from PyPDF2 import PdfReader

def extract_text_from_file(filename: str, file_bytes: bytes) -> str:
    if filename.endswith(".txt") or filename.endswith(".py"):
        return file_bytes.decode("utf-8", errors="ignore")
    elif filename.endswith(".pdf"):
        try:
            with BytesIO(file_bytes) as pdf_stream:
                reader = PdfReader(pdf_stream)
                return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return ""
    elif filename.endswith(".docx"):
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmpf:
                tmpf.write(file_bytes)
                tmp_path = tmpf.name
            doc = Document(tmp_path)
            os.remove(tmp_path)
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception:
            return ""
    return ""
