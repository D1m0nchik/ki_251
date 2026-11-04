import os
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from database import get_db_pool

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://ki-251.onrender.com/app")
STAROSTA_TELEGRAM_ID = int(os.environ.get("STAROSTA_TELEGRAM_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class PollCustomVoteState(StatesGroup):
    waiting_for_custom_text = State()

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
        
        # Якщо це староста і його ще немає в базі — реєструємо автоматично
        if not user and tg_id == STAROSTA_TELEGRAM_ID:
            await conn.execute("""
                INSERT INTO users (telegram_id, full_name, subgroup, role, elective_group)
                VALUES ($1, $2, 1, 'starosta', 0)
                ON CONFLICT (telegram_id) DO UPDATE SET role = 'starosta'
            """, tg_id, full_name)
            user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", tg_id)

        # ⛔ Перевірка білого списку: якщо користувача немає в базі — блокуємо доступ
        if not user:
            await message.answer(
                f"⛔ <b>Доступ обмежено!</b>\n\n"
                f"Вас немає в списку студентів групи <b>КІ-25-1</b>.\n"
                f"Ваш Telegram ID: <code>{tg_id}</code>\n\n"
                f"<i>Скопіюйте цей ID та надішліть старості, щоб він додав вас до бази.</i>",
                parse_mode="HTML"
            )
            return

    await message.answer(
        f"Привіт, <b>{user['full_name']}</b>!\n\n"
        f"Ти успішно авторизований у системі групи <b>КІ-25-1</b>.\n"
        f"Тут можна переглядати актуальний розклад, слідкувати за дедлайнами, проходити опитування та перевіряти відвідуваність.",
        parse_mode="HTML",
        reply_markup=get_start_keyboard()
    )

# Команда для старости: швидке додавання студента до білого списку
@dp.message(Command("add_student"))
async def cmd_add_student(message: types.Message):
    if message.from_user.id != STAROSTA_TELEGRAM_ID:
        return

    parts = message.text.split(maxsplit=3)
    if len(parts) < 4:
        await message.answer(
            "ℹ️ <b>Формат додавання студента:</b>\n"
            "<code>/add_student ТГ_АЙДІ Прізвище_Ім'я Підгрупа</code>\n\n"
            "Приклад:\n"
            "<code>/add_student 123456789 Іванов Іван 1</code>",
            parse_mode="HTML"
        )
        return

    try:
        new_tg_id = int(parts[1])
        full_name = parts[2]
        subgroup = int(parts[3])
    except ValueError:
        await message.answer("❌ Помилка: Telegram ID та підгрупа (1 або 2) мають бути числами.")
        return

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (telegram_id, full_name, subgroup, role, elective_group)
            VALUES ($1, $2, $3, 'student', 0)
            ON CONFLICT (telegram_id) DO UPDATE SET 
                full_name = EXCLUDED.full_name,
                subgroup = EXCLUDED.subgroup
        """, new_tg_id, full_name, subgroup)

    await message.answer(
        f"✅ Студента <b>{full_name}</b> успішно внесено до білого списку!\n"
        f"🆔 ID: <code>{new_tg_id}</code> | Підгрупа: <b>{subgroup}</b>\n\n"
        f"Тепер студент може написати /start і користуватися ботом.",
        parse_mode="HTML"
    )

# Команда для старости: видалення зі списку (блокування доступу)
@dp.message(Command("remove_student"))
async def cmd_remove_student(message: types.Message):
    if message.from_user.id != STAROSTA_TELEGRAM_ID:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("ℹ️ Формат: <code>/remove_student ТГ_АЙДІ</code>", parse_mode="HTML")
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ ID має бути числом.")
        return

    if target_id == STAROSTA_TELEGRAM_ID:
        await message.answer("❌ Не можна видалити старосту.")
        return

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        deleted = await conn.execute("DELETE FROM users WHERE telegram_id = $1", target_id)

    if deleted == "DELETE 0":
        await message.answer("Студента з таким ID не знайдено в базі.")
    else:
        await message.answer(f"🚫 Користувача <code>{target_id}</code> видалено з бази. Доступ заблоковано.", parse_mode="HTML")

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

@dp.callback_query(F.data.startswith("poll_vote_custom_"))
async def handle_poll_vote_custom_callback(callback: types.CallbackQuery, state: FSMContext):
    poll_id = int(callback.data.split("_")[3])
    
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        poll = await conn.fetchrow("SELECT is_active, allow_custom FROM polls WHERE id = $1", poll_id)
        if not poll or not poll["is_active"] or not poll["allow_custom"]:
            await callback.answer("Опитування вже закрито або власний варіант не дозволено.", show_alert=True)
            return

    await state.set_state(PollCustomVoteState.waiting_for_custom_text)
    await state.update_data(poll_id=poll_id)
    await callback.answer()
    await callback.message.answer(
        "✍️ <b>Введіть ваш варіант відповіді для опитування:</b>\n<i>(Надішліть текст у відповідь на це повідомлення)</i>",
        parse_mode="HTML"
    )

@dp.message(PollCustomVoteState.waiting_for_custom_text)
async def process_custom_poll_text(message: types.Message, state: FSMContext):
    custom_text = message.text.strip()
    if not custom_text:
        await message.answer("Будь ласка, введіть непустий текст варіанту:")
        return

    data = await state.get_data()
    poll_id = data.get("poll_id")
    tg_id = message.from_user.id

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        poll = await conn.fetchrow("SELECT is_active, is_multiple FROM polls WHERE id = $1", poll_id)
        if not poll or not poll["is_active"]:
            await message.answer("На жаль, це опитування вже закрите.")
            await state.clear()
            return

        if not poll["is_multiple"]:
            await conn.execute("DELETE FROM poll_votes WHERE poll_id = $1 AND telegram_id = $2", poll_id, tg_id)
        
        await conn.execute("""
            INSERT INTO poll_votes (poll_id, telegram_id, option_id, custom_text)
            VALUES ($1, $2, 999, $3)
            ON CONFLICT (poll_id, telegram_id, option_id) 
            DO UPDATE SET custom_text = EXCLUDED.custom_text
        """, poll_id, tg_id, custom_text)

    await state.clear()
    await message.answer(f"✅ Ваш варіант збережено: <b>«{custom_text}»</b>!", parse_mode="HTML")

@dp.callback_query(F.data.startswith("poll_vote_"))
async def handle_poll_vote_callback(callback: types.CallbackQuery):
    if "poll_vote_custom_" in callback.data:
        return

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
                ON CONFLICT (poll_id, telegram_id, option_id) DO NOTHING
            """, poll_id, tg_id, option_id)
            await callback.answer("Голос враховано!")
