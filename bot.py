import os
import logging
import re
import aiohttp
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY or not UNSPLASH_ACCESS_KEY:
    raise ValueError("Проверь .env: отсутствует токен Telegram, Gemini или Unsplash.")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("models/gemini-1.5-pro-latest")

bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# Функция очистки текста от HTML
def clean_html(text: str) -> str:
    return re.sub(r"<.*?>", "", text)

# Запрос к Unsplash API
async def fetch_unsplash_image(query: str) -> str | None:
    url = f"https://api.unsplash.com/photos/random?query={query}&orientation=landscape&client_id={UNSPLASH_ACCESS_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("urls", {}).get("regular")
    return None

# Определение, есть ли в запросе просьба показать фото
def is_photo_request(text: str) -> bool:
    return any(kw in text.lower() for kw in ["покажи", "покажи фото", "изображение", "покажи картинку", "покажи арт"])

# Обработка запроса
@dp.message()
async def handle_message(message: Message):
    user_text = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name

    try:
        # Запрос в Gemini
        gemini_response = await model.generate_content_async(user_text)
        gemini_text = gemini_response.text.strip()

        # Проверка запроса на фото
        if is_photo_request(user_text):
            image_query = user_text.replace("покажи", "").strip()
            photo_url = await fetch_unsplash_image(image_query)

            if photo_url:
                # Обрезаем caption если слишком длинный
                if len(gemini_text) > 900:
                    short_text = gemini_text[:900].rsplit(".", 1)[0] + "..."
                    await bot.send_photo(message.chat.id, photo_url, caption=short_text)
                    await message.answer(gemini_text)  # Полный текст отдельно
                else:
                    await bot.send_photo(message.chat.id, photo_url, caption=gemini_text)
            else:
                await message.answer("😕 Не удалось найти подходящее изображение.")

        else:
            await message.answer(gemini_text)

    except aiohttp.ClientConnectionError:
        await message.answer("🚫 Не удаётся подключиться к интернету.")
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await message.answer(f"❌ Ошибка: {clean_html(str(e))}")

# Запуск
if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))
