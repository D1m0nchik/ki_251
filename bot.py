import os
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

from database import get_db_pool

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-domain.com/app")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Відкрити додаток", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"Привіт, <b>{message.from_user.full_name}</b>!\n\n"
        "Тут доступний розклад та журнал відвідуваності групи <b>КІ-25-1</b>.\n"
        "Натисніть кнопку нижче для запуску застосунку 👇",
        reply_markup=kb
    )

@dp.callback_query(lambda c: c.data and c.data.startswith("done_reminder_"))
async def process_reminder_done(callback: types.CallbackQuery):
    reminder_id = int(callback.data.replace("done_reminder_", ""))
    tg_id = callback.from_user.id

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO reminder_completions (reminder_id, telegram_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
        """, reminder_id, tg_id)

    await callback.answer("Чудово! Ви відмітили завдання як виконане. Автонагадування вимкнено.", show_alert=True)
    try:
        current_html = callback.message.html_text or ""
        if "✅ <b>Ви відмітили це завдання як виконане!</b>" not in current_html:
            await callback.message.edit_text(
                f"{current_html}\n\n✅ <b>Ви відмітили це завдання як виконане!</b>",
                parse_mode="HTML"
            )
    except Exception:
        pass
