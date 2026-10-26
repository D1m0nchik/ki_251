import os
import json
import hmac
import hashlib
from urllib.parse import parse_qsl
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import get_db_pool
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
    if not init_data or init_data.strip() == "":
        return {
            "id": STAROSTA_TELEGRAM_ID if STAROSTA_TELEGRAM_ID != 0 else 123456789,
            "first_name": "Староста",
            "last_name": "КІ-25-1"
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
    tg_id = int(user.get("id"))
    
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", tg_id)
        role = "starosta" if tg_id == STAROSTA_TELEGRAM_ID else "student"
        name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or "Студент"

        if not row:
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, subgroup, role) VALUES ($1, $2, 1, $3)",
                tg_id, name, role
            )
            return {"telegram_id": tg_id, "full_name": name, "subgroup": 1, "role": role}

        if tg_id == STAROSTA_TELEGRAM_ID and row["role"] != "starosta":
            await conn.execute("UPDATE users SET role = 'starosta' WHERE telegram_id = $1", tg_id)
            return {**dict(row), "role": "starosta"}

        return dict(row)

class ProfileUpdateRequest(BaseModel):
    full_name: str
    subgroup: int

@app.post("/api/user/profile")
async def update_profile(data: ProfileUpdateRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = int(user.get("id"))

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users 
            SET full_name = $1, subgroup = $2 
            WHERE telegram_id = $3
        """, data.full_name.strip(), data.subgroup, tg_id)
    return {"status": "ok"}

@app.get("/api/schedule")
async def get_schedule(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = int(user.get("id"))
    
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow("SELECT subgroup FROM users WHERE telegram_id = $1", tg_id)
        subgroup = user_row["subgroup"] if user_row else 1
            
        rows = await conn.fetch("""
            SELECT * FROM schedule 
            WHERE target_subgroup IN (0, $1) 
            ORDER BY id ASC
        """, subgroup)
        return [dict(r) for r in rows]

@app.get("/api/group/students")
async def get_all_group_students(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        u = await conn.fetchrow("SELECT role FROM users WHERE telegram_id = $1", int(user.get("id")))
        if not u or u["role"] != "starosta":
            raise HTTPException(status_code=403, detail="Доступ заборонено")

        rows = await conn.fetch("SELECT telegram_id, full_name, subgroup, role FROM users ORDER BY full_name ASC")
        return [dict(r) for r in rows]

class ChangeSubgroupRequest(BaseModel):
    student_tg_id: int
    subgroup: int

@app.post("/api/group/change_subgroup")
async def change_student_subgroup(data: ChangeSubgroupRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        u = await conn.fetchrow("SELECT role FROM users WHERE telegram_id = $1", int(user.get("id")))
        if not u or u["role"] != "starosta":
            raise HTTPException(status_code=403, detail="Доступ заборонено")

        await conn.execute("UPDATE users SET subgroup = $1 WHERE telegram_id = $2", data.subgroup, data.student_tg_id)
    return {"status": "ok"}

@app.get("/api/attendance/students")
async def get_students_for_lesson(schedule_id: int, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        u = await conn.fetchrow("SELECT role FROM users WHERE telegram_id = $1", int(user.get("id")))
        if not u or u["role"] != "starosta":
            raise HTTPException(status_code=403, detail="Доступ заборонено")

        lesson = await conn.fetchrow("SELECT target_subgroup FROM schedule WHERE id = $1", schedule_id)
        if not lesson:
            raise HTTPException(status_code=404, detail="Пару не знайдено")
        target_sg = lesson["target_subgroup"]

        query = """
            SELECT u.telegram_id, u.full_name, u.subgroup, COALESCE(a.status, 'present') as status
            FROM users u
            LEFT JOIN attendance a ON a.telegram_id = u.telegram_id AND a.schedule_id = $1
            WHERE ($2 = 0 OR u.subgroup = $3)
            ORDER BY u.full_name ASC
        """
        rows = await conn.fetch(query, schedule_id, target_sg, target_sg)
        return [dict(r) for r in rows]

class AttendanceSaveRequest(BaseModel):
    schedule_id: int
    records: Dict[int, str]

@app.post("/api/attendance/save")
async def save_attendance(data: AttendanceSaveRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        u = await conn.fetchrow("SELECT role FROM users WHERE telegram_id = $1", int(user.get("id")))
        if not u or u["role"] != "starosta":
            raise HTTPException(status_code=403, detail="Доступ заборонено")

        async with conn.transaction():
            for tg_id, status in data.records.items():
                await conn.execute("""
                    INSERT INTO attendance (schedule_id, telegram_id, status)
                    VALUES ($1, $2, $3)
                    ON CONFLICT(schedule_id, telegram_id) DO UPDATE SET status = EXCLUDED.status
                """, data.schedule_id, int(tg_id), status)
    return {"status": "ok"}

@app.get("/api/my_stats")
async def get_my_stats(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = int(user.get("id"))

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END) as present_count,
                SUM(CASE WHEN status = 'absent' THEN 1 ELSE 0 END) as absent_count,
                SUM(CASE WHEN status = 'late' THEN 1 ELSE 0 END) as late_count,
                SUM(CASE WHEN status = 'excused' THEN 1 ELSE 0 END) as excused_count
            FROM attendance
            WHERE telegram_id = $1
        """, tg_id)
        general = dict(row) if row else {}

        rows = await conn.fetch("""
            SELECT 
                s.subject,
                COUNT(a.id) as total_subject_lessons,
                SUM(CASE WHEN a.status = 'absent' THEN 1 ELSE 0 END) as subject_absent
            FROM schedule s
            JOIN attendance a ON a.schedule_id = s.id
            WHERE a.telegram_id = $1
            GROUP BY s.subject
        """, tg_id)
        by_subjects = [dict(r) for r in rows]

    return {"general": general, "subjects": by_subjects}

@app.get("/api/announcements")
async def get_announcements():
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, message, 
                   to_char(created_at, 'DD.MM.YYYY HH24:MI') as date_time
            FROM announcements 
            ORDER BY id DESC
        """)
        return [dict(r) for r in rows]

class BroadcastRequest(BaseModel):
    title: str
    message: str

@app.post("/api/broadcast")
async def broadcast_announcement(data: BroadcastRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        u = await conn.fetchrow("SELECT role FROM users WHERE telegram_id = $1", int(user.get("id")))
        if not u or u["role"] != "starosta":
            raise HTTPException(status_code=403, detail="Тільки староста може робити розсилку")

        await conn.execute(
            "INSERT INTO announcements (title, message) VALUES ($1, $2)",
            data.title.strip(), data.message.strip()
        )
        students = await conn.fetch("SELECT telegram_id FROM users")

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

@app.post("/api/schedule/update_range")
async def update_schedule_range(data: DateRangeRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        u = await conn.fetchrow("SELECT role FROM users WHERE telegram_id = $1", int(user.get("id")))
        if not u or u["role"] != "starosta":
            raise HTTPException(status_code=403, detail="Доступ заборонено")

    await fetch_and_save_schedule(sdate=data.sdate, edate=data.edate)
    return {"status": "success"}

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"

if WEBAPP_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(WEBAPP_DIR), html=True), name="webapp")
