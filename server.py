from pydantic import BaseModel
from typing import List, Dict
from bot import bot
from parser import fetch_and_save_schedule

# Вкажи свій справжній числовий Telegram ID, щоб бот автоматично призначив тебе старостою
STAROSTA_TELEGRAM_ID = int(os.environ.get("STAROSTA_TELEGRAM_ID", 0))

@app.get("/api/user_data")
async def get_user_data(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = user.get("id")
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,)) as cur:
            row = await cur.fetchone()
            role = "starosta" if tg_id == STAROSTA_TELEGRAM_ID else "student"
            name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or "Студент"

            if not row:
                await db.execute(
                    "INSERT INTO users (telegram_id, full_name, subgroup, role) VALUES (?, ?, 1, ?)",
                    (tg_id, name, role)
                )
                await db.commit()
                return {"telegram_id": tg_id, "full_name": name, "subgroup": 1, "role": role}
            
            # Якщо користувач є головним старостою, гарантуємо йому роль
            if tg_id == STAROSTA_TELEGRAM_ID and row["role"] != "starosta":
                await db.execute("UPDATE users SET role = 'starosta' WHERE telegram_id = ?", (tg_id,))
                await db.commit()
                return {**dict(row), "role": "starosta"}

            return dict(row)

# Отримання списку студентів на пару (для старости)
@app.get("/api/attendance/students")
async def get_students_for_lesson(schedule_id: int, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        
        # Перевірка ролі
        async with db.execute("SELECT role FROM users WHERE telegram_id = ?", (user.get("id"),)) as cur:
            u = await cur.fetchone()
            if not u or u["role"] != "starosta":
                raise HTTPException(status_code=403, detail="Доступ заборонено")

        # Отримуємо підгрупу цієї пари
        async with db.execute("SELECT target_subgroup FROM schedule WHERE id = ?", (schedule_id,)) as cur:
            lesson = await cur.fetchone()
            if not lesson:
                raise HTTPException(status_code=404, detail="Пару не знайдено")
            target_sg = lesson["target_subgroup"]

        # Витягуємо студентів, які мають бути на цій парі, та їхній збережений статус
        query = """
            SELECT u.telegram_id, u.full_name, u.subgroup, COALESCE(a.status, 'present') as status
            FROM users u
            LEFT JOIN attendance a ON a.telegram_id = u.telegram_id AND a.schedule_id = ?
            WHERE (? = 0 OR u.subgroup = ?)
            ORDER BY u.full_name ASC
        """
        async with db.execute(query, (schedule_id, target_sg, target_sg)) as cur:
            return [dict(r) for r in await cur.fetchall()]

# Збереження журналу старостою
class AttendanceSaveRequest(BaseModel):
    schedule_id: int
    records: Dict[int, str]  # { telegram_id: 'present'/'absent'/'late'/'excused' }

@app.post("/api/attendance/save")
async def save_attendance(data: AttendanceSaveRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT role FROM users WHERE telegram_id = ?", (user.get("id"),)) as cur:
            u = await cur.fetchone()
            if not u or u["role"] != "starosta":
                raise HTTPException(status_code=403, detail="Доступ заборонено")

        for tg_id, status in data.records.items():
            await db.execute("""
                INSERT INTO attendance (schedule_id, telegram_id, status)
                VALUES (?, ?, ?)
                ON CONFLICT(schedule_id, telegram_id) DO UPDATE SET status = excluded.status
            """, (data.schedule_id, tg_id, status))
        await db.commit()
    return {"status": "ok"}

# Персональна статистика студента
@app.get("/api/my_stats")
async def get_my_stats(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = user.get("id")

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        
        # Загальна кількість пар та пропусків
        async with db.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END) as present_count,
                SUM(CASE WHEN status = 'absent' THEN 1 ELSE 0 END) as absent_count,
                SUM(CASE WHEN status = 'late' THEN 1 ELSE 0 END) as late_count,
                SUM(CASE WHEN status = 'excused' THEN 1 ELSE 0 END) as excused_count
            FROM attendance
            WHERE telegram_id = ?
        """, (tg_id,)) as cur:
            general = dict(await cur.fetchone())

        # Статистика за окремими дисциплінами
        async with db.execute("""
            SELECT 
                s.subject,
                COUNT(a.id) as total_subject_lessons,
                SUM(CASE WHEN a.status = 'absent' THEN 1 ELSE 0 END) as subject_absent
            FROM schedule s
            JOIN attendance a ON a.schedule_id = s.id
            WHERE a.telegram_id = ?
            GROUP BY s.subject
        """, (tg_id,)) as cur:
            by_subjects = [dict(r) for r in await cur.fetchall()]

    return {"general": general, "subjects": by_subjects}

# Масова розсилка оголошення всій групі
class BroadcastRequest(BaseModel):
    title: str
    message: str

@app.post("/api/broadcast")
async def broadcast_announcement(data: BroadcastRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT role FROM users WHERE telegram_id = ?", (user.get("id"),)) as cur:
            u = await cur.fetchone()
            if not u or u["role"] != "starosta":
                raise HTTPException(status_code=403, detail="Тільки староста може робити розсилку")

        # Отримуємо ID всіх зареєстрованих студентів
        async with db.execute("SELECT telegram_id FROM users") as cur:
            students = await cur.fetchall()

    text = f"📢 <b>{data.title}</b>\n\n{data.message}\n\n<i>— Повідомлення від старости</i>"
    success_count = 0
    for s in students:
        try:
            await bot.send_message(chat_id=s["telegram_id"], text=text, parse_mode="HTML")
            success_count += 1
        except Exception:
            pass # Якщо користувач заблокував бота

    return {"sent_to": success_count}

# Завантаження розкладу за вказаним діапазоном дат
class DateRangeRequest(BaseModel):
    sdate: str
    edate: str

@app.post("/api/schedule/update_range")
async def update_schedule_range(data: DateRangeRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    # Тільки староста може оновлювати діапазон
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT role FROM users WHERE telegram_id = ?", (user.get("id"),)) as cur:
            u = await cur.fetchone()
            if not u or u["role"] != "starosta":
                raise HTTPException(status_code=403, detail="Доступ заборонено")

    await fetch_and_save_schedule(sdate=data.sdate, edate=data.edate)
    return {"status": "success"}
