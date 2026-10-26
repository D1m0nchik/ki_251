import os
import json
import hmac
import hashlib
from urllib.parse import parse_qsl
from pathlib import Path
from typing import List, Dict

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import aiosqlite

from database import DB_NAME
from bot import bot
from parser import fetch_and_save_schedule

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
STAROSTA_TELEGRAM_ID = int(os.environ.get("STAROSTA_TELEGRAM_ID", 0))

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def verify_telegram_data(init_data: str) -> dict:
    """Перевіряє валідність Telegram initData або повертає тестового користувача для браузера."""
    if not init_data or init_data.strip() == "":
        return {
            "id": STAROSTA_TELEGRAM_ID if STAROSTA_TELEGRAM_ID != 0 else 123456789,
            "first_name": "Староста",
            "last_name": "КІ-25-1",
            "username": "starosta"
        }
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_received = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash != hash_received:
            return {
                "id": STAROSTA_TELEGRAM_ID if STAROSTA_TELEGRAM_ID != 0 else 123456789,
                "first_name": "Студент",
                "last_name": "Тест"
            }
            
        return json.loads(parsed_data.get("user", "{}"))
    except Exception:
        return {
            "id": STAROSTA_TELEGRAM_ID if STAROSTA_TELEGRAM_ID != 0 else 123456789,
            "first_name": "Студент",
            "last_name": "Тест"
        }

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
            
            if tg_id == STAROSTA_TELEGRAM_ID and row["role"] != "starosta":
                await db.execute("UPDATE users SET role = 'starosta' WHERE telegram_id = ?", (tg_id,))
                await db.commit()
                return {**dict(row), "role": "starosta"}

            return dict(row)

@app.get("/api/schedule")
async def get_schedule(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = user.get("id")
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT subgroup FROM users WHERE telegram_id = ?", (tg_id,)) as cur:
            user_row = await cur.fetchone()
            subgroup = user_row["subgroup"] if user_row else 1
            
        # Сортування за id зберігає хронологічну послідовність пар
        async with db.execute("""
            SELECT * FROM schedule 
            WHERE target_subgroup IN (0, ?) 
            ORDER BY id ASC
        """, (subgroup,)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

@app.get("/api/attendance/students")
async def get_students_for_lesson(schedule_id: int, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        
        async with db.execute("SELECT role FROM users WHERE telegram_id = ?", (user.get("id"),)) as cur:
            u = await cur.fetchone()
            if not u or u["role"] != "starosta":
                raise HTTPException(status_code=403, detail="Доступ заборонено")

        async with db.execute("SELECT target_subgroup FROM schedule WHERE id = ?", (schedule_id,)) as cur:
            lesson = await cur.fetchone()
            if not lesson:
                raise HTTPException(status_code=404, detail="Пару не знайдено")
            target_sg = lesson["target_subgroup"]

        query = """
            SELECT u.telegram_id, u.full_name, u.subgroup, COALESCE(a.status, 'present') as status
            FROM users u
            LEFT JOIN attendance a ON a.telegram_id = u.telegram_id AND a.schedule_id = ?
            WHERE (? = 0 OR u.subgroup = ?)
            ORDER BY u.full_name ASC
        """
        async with db.execute(query, (schedule_id, target_sg, target_sg)) as cur:
            return [dict(r) for r in await cur.fetchall()]

class AttendanceSaveRequest(BaseModel):
    schedule_id: int
    records: Dict[int, str]

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

@app.get("/api/my_stats")
async def get_my_stats(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = user.get("id")

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        
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
            row = await cur.fetchone()
            general = dict(row) if row else {}

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

class BroadcastRequest(BaseModel):
    title: str
    message: str

# Отримання списку всіх оголошень (для відображення у додатку)
@app.get("/api/announcements")
async def get_announcements():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, title, message, 
                   strftime('%d.%m.%Y %H:%M', created_at, '+3 hours') as date_time
            FROM announcements 
            ORDER BY id DESC
        """) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

# Оновлена розсилка зі збереженням у базу
@app.post("/api/broadcast")
async def broadcast_announcement(data: BroadcastRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT role FROM users WHERE telegram_id = ?", (user.get("id"),)) as cur:
            u = await cur.fetchone()
            if not u or u["role"] != "starosta":
                raise HTTPException(status_code=403, detail="Тільки староста може робити розсилку")

        # 1. Зберігаємо оголошення в базу даних
        await db.execute(
            "INSERT INTO announcements (title, message) VALUES (?, ?)",
            (data.title.strip(), data.message.strip())
        )
        await db.commit()

        # 2. Отримуємо студентів для розсилки в Telegram
        async with db.execute("SELECT telegram_id FROM users") as cur:
            students = await cur.fetchall()

    # 3. Розсилаємо повідомлення усім студентам
    text = f"📢 <b>{data.title}</b>\n\n{data.message}\n\n<i>— Повідомлення від старости</i>"
    success_count = 0
    for s in students:
        try:
            await bot.send_message(chat_id=s["telegram_id"], text=text, parse_mode="HTML")
            success_count += 1
        except Exception:
            pass

    return {"sent_to": success_count}

class DateRangeRequest(BaseModel):
    sdate: str
    edate: str

# 1. Студент зберігає своє справжнє ПІБ та підгрупу
class ProfileUpdateRequest(BaseModel):
    full_name: str
    subgroup: int

@app.post("/api/user/profile")
async def update_profile(data: ProfileUpdateRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = user.get("id")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            UPDATE users 
            SET full_name = ?, subgroup = ? 
            WHERE telegram_id = ?
        """, (data.full_name.strip(), data.subgroup, tg_id))
        await db.commit()
    return {"status": "ok"}

# 2. Староста отримує повний список студентів для керування
@app.get("/api/group/students")
async def get_all_group_students(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT role FROM users WHERE telegram_id = ?", (user.get("id"),)) as cur:
            u = await cur.fetchone()
            if not u or u["role"] != "starosta":
                raise HTTPException(status_code=403, detail="Доступ заборонено")

        async with db.execute("SELECT telegram_id, full_name, subgroup, role FROM users ORDER BY full_name ASC") as cur:
            return [dict(r) for r in await cur.fetchall()]

# 3. Староста перемикає підгрупу будь-якому студенту
class ChangeSubgroupRequest(BaseModel):
    student_tg_id: int
    subgroup: int

@app.post("/api/group/change_subgroup")
async def change_student_subgroup(data: ChangeSubgroupRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT role FROM users WHERE telegram_id = ?", (user.get("id"),)) as cur:
            u = await cur.fetchone()
            if not u or u["role"] != "starosta":
                raise HTTPException(status_code=403, detail="Доступ заборонено")

        await db.execute("UPDATE users SET subgroup = ? WHERE telegram_id = ?", (data.subgroup, data.student_tg_id))
        await db.commit()
    return {"status": "ok"}
    
@app.post("/api/schedule/update_range")
async def update_schedule_range(data: DateRangeRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT role FROM users WHERE telegram_id = ?", (user.get("id"),)) as cur:
            u = await cur.fetchone()
            if not u or u["role"] != "starosta":
                raise HTTPException(status_code=403, detail="Доступ заборонено")

    await fetch_and_save_schedule(sdate=data.sdate, edate=data.edate)
    return {"status": "success"}

# Роздача статичних файлів інтерфейсу
BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"

if WEBAPP_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(WEBAPP_DIR), html=True), name="webapp")
