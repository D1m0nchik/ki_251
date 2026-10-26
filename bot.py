import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    # Кнопка відкриття WebApp
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Відкрити розклад групи 📲",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}/app/")
                )
            ]
        ]
    )
    await message.answer(
        f"Привіт, {message.from_user.first_name}!\n\n"
        "Натисни кнопку нижче, щоб відкрити персональний розклад та журнал групи КІ-25-1.",
        reply_markup=kb
    )
