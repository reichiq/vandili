import logging
import os
import re
import random
import aiohttp
from io import BytesIO
import tempfile
import json
from string import punctuation

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode, ChatType
from aiogram.types import (
    FSInputFile, Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, InputFile, BufferedInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from pymorphy3 import MorphAnalyzer

from dotenv import load_dotenv
from pathlib import Path
import asyncio

import google.generativeai as genai
from google.cloud import translate
from google.oauth2 import service_account

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

# Используем модель Gemini 2.0-flash
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-2.0-flash")

chat_history = {}

ENABLED_CHATS_FILE = "enabled_chats.json"
ADMIN_ID = 1936733487

# Текст, который бот присылает в ЛС, когда переходит в «режим поддержки»
SUPPORT_PROMPT_TEXT = (
    "Отправьте любое сообщение (текст, фото, видео, файлы, аудио, голосовые) — всё дойдёт до поддержки."
)

support_mode_users = set()

# ---------------------- Вспомогательные функции ---------------------- #
def thread_kwargs(message: Message) -> dict:
    """
    Если это супергруппа/группа с топиками, вернём словарь {"message_thread_id": ...}, иначе пусто.
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

# ---------------------- Обработчики команд ---------------------- #
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """
    Обработчик команды /start с возможным аргументом, напр. /start support
    """
    # Разбираем аргумент после /start (если есть)
    parts = message.text.split(maxsplit=1)
    arg = ""
    if len(parts) > 1:
        arg = parts[1].strip().lower()

    if arg == "support":
        # Пользователь пришёл по ссылке t.me/<бот>?start=support
        # Включаем режим поддержки
        support_mode_users.add(message.from_user.id)
        await message.answer(
            "Вы в режиме поддержки!\n\n" + SUPPORT_PROMPT_TEXT,
            **thread_kwargs(message)
        )
        return

    # Обычный старт (без аргумента)
    greet = (
        "Привет! Я <b>VAI</b> — интеллектуальный помощник 😊\n\n"
        "Просто напиши мне, и я постараюсь ответить или помочь.\n"
        "Всегда на связи!"
    )
    await message.answer(greet, **thread_kwargs(message))

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
            **thread_kwargs(message)
        )
        logging.info(f"[BOT] Бот отключён в группе {message.chat.id}")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """
    1) В личке: колбэк-кнопка «Написать в поддержку».
    2) В группе: ссылка на личку бота с ?start=support,
       чтобы пользователь при нажатии сразу попал в режим поддержки.
    """
    if message.chat.type == ChatType.PRIVATE:
        # В личке — колбэк-кнопка
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
            reply_markup=keyboard
        )
    else:
        # В группе — ссылка на личку + параметр start=support
        private_url = f"https://t.me/{BOT_USERNAME}?start=support"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✉️ Написать в поддержку",
                        url=private_url
                    )
                ]
            ]
        )
        await message.answer(
            "Если возник вопрос или хочешь сообщить об ошибке — напиши мне в личку:",
            reply_markup=keyboard,
            **thread_kwargs(message)
        )

# ---------------------- Режим поддержки (callback в ЛС) ---------------------- #
@dp.callback_query(F.data == "support_request")
async def handle_support_click(callback: CallbackQuery):
    """
    Срабатывает только в ЛС, где колбэк-кнопка «Написать в поддержку».
    """
    await callback.answer()
    support_mode_users.add(callback.from_user.id)
    await callback.message.answer(SUPPORT_PROMPT_TEXT)

@dp.message()
async def handle_all_messages(message: Message):
    uid = message.from_user.id

    # Если пользователь в режиме "поддержки", пересылаем сообщение админу
    if uid in support_mode_users:
        await forward_to_support(message)
    else:
        # Иначе обрабатываем обычные сообщения
        await handle_msg(message)

# ---------------------- Логика пересылки в поддержку ---------------------- #
async def forward_to_support(message: Message):
    uid = message.from_user.id
    caption = message.caption or message.text or "[Без текста]"
    username_part = f" (@{message.from_user.username})" if message.from_user.username else ""
    content = (
        f"\u2728 <b>Новое сообщение в поддержку</b> от <b>{message.from_user.full_name}</b>{username_part} "
        f"(id: <code>{uid}</code>):\n\n{caption}"
    )

    try:
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

        elif message.document:
            file = await bot.get_file(message.document.file_id)
            url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    doc_bytes = await resp.read()
            await bot.send_document(
                chat_id=ADMIN_ID,
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
                chat_id=ADMIN_ID,
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
                chat_id=ADMIN_ID,
                voice=BufferedInputFile(voice_bytes, filename="voice.ogg"),
                caption=content
            )

        else:
            # Если просто текст, без вложений
            await bot.send_message(ADMIN_ID, content)

        # Ответ пользователю в ЛС
        if message.chat.type == ChatType.PRIVATE:
            await message.answer("Спасибо! Ваше сообщение отправлено в поддержку.")

    except Exception as e:
        logging.error(f"[BOT] Ошибка при пересылке в поддержку: {e}")
        if message.chat.type == ChatType.PRIVATE:
            await message.answer("Произошла ошибка при отправке сообщения. Попробуйте позже.")

# ---------------------- Генерация ответа Gemini и "вай покажи" ---------------------- #
async def generate_and_send_gemini_response(cid, full_prompt, show_image, rus_word, leftover):
    gemini_text = ""

    # Если нужно только короткая подпись для картинки
    if show_image and rus_word and not leftover:
        gemini_text = generate_short_caption(rus_word)
    else:
        if full_prompt:
            chat_history.setdefault(cid, []).append({"role": "user", "parts": [full_prompt]})
            # Чтобы не копился слишком большой контекст, обрезаем историю
            if len(chat_history[cid]) > 5:
                chat_history[cid].pop(0)

            try:
                # Показываем "typing"
                await bot.send_chat_action(chat_id=cid, action="typing")
                # Генерация Gemini
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

# ---------------------- Основная логика (Gemini и т.д.) ---------------------- #
async def handle_msg(message: Message, prompt_mode: bool = False):
    """
    Основной обработчик сообщений. Отвечает только при упоминании бота/ответе на него,
    а также обрабатывает "вай покажи ..." и прочее.
    """
    cid = message.chat.id
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

    # Реакция на "как тебя зовут" и "кто создал"
    lower_inp = user_input.lower()
    if any(nc in lower_inp for nc in NAME_COMMANDS):
        await message.answer(
            "Меня зовут <b>VAI</b>! 🤖",
            **thread_kwargs(message)
        )
        return
    if any(ic in lower_inp for ic in INFO_COMMANDS):
        await message.answer(
            random.choice(OWNER_REPLIES),
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
                        await bot.send_chat_action(
                            chat_id=cid,
                            action="upload_photo",
                            **thread_kwargs(message)
                        )
                        file = FSInputFile(tmp_path, filename="image.jpg")
                        caption, rest = split_caption_and_text(gemini_text)
                        # Отправляем фото
                        await bot.send_photo(
                            chat_id=cid,
                            photo=file,
                            caption=caption if caption else "...",
                            **thread_kwargs(message)
                        )
                        # Если остался текст после 950 символов, отправляем сообщениями
                        for c in rest:
                            await bot.send_message(
                                chat_id=cid,
                                text=c,
                                **thread_kwargs(message)
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
                **thread_kwargs(message)
            )

# ---------------------- Запуск бота ---------------------- #
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
