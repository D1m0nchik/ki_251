import os
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from database import get_db_pool

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://ki-251.onrender.com/app")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_start_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Відкрити розклад та журнал", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    tg_id = message.from_user.id
    full_name = message.from_user.full_name or "Студент"
    
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", tg_id)
        if not user:
            await conn.execute("""
                INSERT INTO users (telegram_id, full_name, subgroup, role, elective_group)
                VALUES ($1, $2, 1, 'student', 0)
                ON CONFLICT (telegram_id) DO NOTHING
            """, tg_id, full_name)

    await message.answer(
        f"Привіт, <b>{full_name}</b>!\n\n"
        f"Це бот групи <b>КІ-25-1</b>.\n"
        f"Тут ти можеш переглядати розклад занять, відмічати виконані завдання, голосувати в опитуваннях та отримувати сповіщення.",
        parse_mode="HTML",
        reply_markup=get_start_keyboard()
    )

@dp.callback_query(F.data.startswith("done_reminder_"))
async def handle_done_reminder_callback(callback: types.CallbackQuery):
    reminder_id = int(callback.data.split("_")[2])
    tg_id = callback.from_user.id

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rem = await conn.fetchrow("SELECT title, is_active FROM reminders WHERE id = $1", reminder_id)
        if not rem or not rem["is_active"]:
            await callback.answer("Це завдання вже неактивне.", show_alert=True)
            return

        await conn.execute("""
            INSERT INTO reminder_completions (reminder_id, telegram_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
        """, reminder_id, tg_id)

    await callback.answer("✅ Чудово! Завдання позначено як виконане.", show_alert=True)
    try:
        await callback.message.edit_text(
            callback.message.text + "\n\n<i>✅ Ви відмітили це завдання як виконане!</i>",
            parse_mode="HTML"
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("poll_vote_"))
async def handle_poll_vote_callback(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    poll_id = int(parts[2])
    option_id = int(parts[3])
    tg_id = callback.from_user.id

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        poll = await conn.fetchrow("SELECT is_active, is_multiple FROM polls WHERE id = $1", poll_id)
        if not poll or not poll["is_active"]:
            await callback.answer("Це опитування вже завершено!", show_alert=True)
            return

        existing = await conn.fetchrow(
            "SELECT id FROM poll_votes WHERE poll_id = $1 AND telegram_id = $2 AND option_id = $3",
            poll_id, tg_id, option_id
        )

        if existing:
            await conn.execute("DELETE FROM poll_votes WHERE id = $1", existing["id"])
            await callback.answer("Ваш голос знято.")
        else:
            if not poll["is_multiple"]:
                await conn.execute("DELETE FROM poll_votes WHERE poll_id = $1 AND telegram_id = $2", poll_id, tg_id)
            
            await conn.execute("""
                INSERT INTO poll_votes (poll_id, telegram_id, option_id)
                VALUES ($1, $2, $3)
                ON CONFLICT DO NOTHING
            """, poll_id, tg_id, option_id)
            await callback.answer("Голос враховано!")
