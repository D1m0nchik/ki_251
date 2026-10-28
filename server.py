import os
import json
import hmac
import hashlib
import csv
import io
import re
import asyncio
from urllib.parse import parse_qsl
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import get_db_pool
from bot import bot
from parser import fetch_and_save_schedule

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
STAROSTA_TELEGRAM_ID = int(os.environ.get("STAROSTA_TELEGRAM_ID", 0))

# Часовий пояс України
KYIV_TZ = ZoneInfo("Europe/Kyiv")

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

def linkify_telegram(text: str) -> str:
    url_pattern = r'(https?://[^\s<>"]+|www\.[^\s<>"]+|t\.me/[^\s<>"]+)'
    def replace_url(match):
        url = match.group(0)
        href = url if url.startswith(("http://", "https://")) else f"https://{url}"
        return f'<a href="{href}">{url}</a>'
    return re.sub(url_pattern, replace_url, text)

def get_reminder_keyboard(reminder_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я виконав", callback_data=f"done_reminder_{reminder_id}")]
    ])

def parse_to_utc(dt_str: str) -> datetime:
    """Приймає рядок з форми і перетворює його з Київського часу в UTC для бази даних"""
    clean_str = dt_str.replace("Z", "")
    dt_naive = datetime.fromisoformat(clean_str)
    # Якщо таймзона не була передана з фронту — вважаємо, що це час по Києву
    if dt_naive.tzinfo is None:
        dt_kyiv = dt_naive.replace(tzinfo=KYIV_TZ)
    else:
        dt_kyiv = dt_naive.astimezone(KYIV_TZ)
    return dt_kyiv.astimezone(timezone.utc)

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

SQL_DATE_CONVERT = "TO_DATE(SUBSTRING(date_str, 1, 10), 'DD.MM.YYYY')"

@app.get("/api/schedule")
async def get_schedule(
    sdate: Optional[str] = Query(None),
    edate: Optional[str] = Query(None),
    x_telegram_init_data: str = Header(...)
):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = int(user.get("id"))
    
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow("SELECT role, subgroup FROM users WHERE telegram_id = $1", tg_id)
        role = user_row["role"] if user_row else "student"
        subgroup = user_row["subgroup"] if user_row else 1

        # 1. Пріоритетний діапазон пошуку (якщо користувач обрав період вручну)
        if sdate and edate and len(sdate.strip()) >= 10 and len(edate.strip()) >= 10:
            s_clean = sdate.strip()[:10]
            e_clean = edate.strip()[:10]
            
            if role == "starosta":
                query = f"""
                    SELECT * FROM schedule 
                    WHERE {SQL_DATE_CONVERT} >= TO_DATE($1, 'DD.MM.YYYY')
                      AND {SQL_DATE_CONVERT} <= TO_DATE($2, 'DD.MM.YYYY')
                    ORDER BY {SQL_DATE_CONVERT} ASC, lesson_number ASC
                """
                rows = await conn.fetch(query, s_clean, e_clean)
            else:
                query = f"""
                    SELECT * FROM schedule 
                    WHERE target_subgroup IN (0, $1)
                      AND {SQL_DATE_CONVERT} >= TO_DATE($2, 'DD.MM.YYYY')
                      AND {SQL_DATE_CONVERT} <= TO_DATE($3, 'DD.MM.YYYY')
                    ORDER BY {SQL_DATE_CONVERT} ASC, lesson_number ASC
                """
                rows = await conn.fetch(query, subgroup, s_clean, e_clean)
            return [dict(r) for r in rows]

        # 2. Дефолтний режим: обчислюємо дату за Києвом (ігноруючи UTC бази)
        now_kyiv = datetime.now(KYIV_TZ).date()
        days_to_sunday = 6 - now_kyiv.weekday()
        sunday_kyiv = now_kyiv + timedelta(days=days_to_sunday)

        today_str = now_kyiv.strftime("%d.%m.%Y")
        sunday_str = sunday_kyiv.strftime("%d.%m.%Y")

        if role == "starosta":
            query = f"""
                SELECT * FROM schedule 
                WHERE {SQL_DATE_CONVERT} >= TO_DATE($1, 'DD.MM.YYYY')
                  AND {SQL_DATE_CONVERT} <= TO_DATE($2, 'DD.MM.YYYY')
                ORDER BY {SQL_DATE_CONVERT} ASC, lesson_number ASC
            """
            rows = await conn.fetch(query, today_str, sunday_str)
        else:
            query = f"""
                SELECT * FROM schedule 
                WHERE target_subgroup IN (0, $1)
                  AND {SQL_DATE_CONVERT} >= TO_DATE($2, 'DD.MM.YYYY')
                  AND {SQL_DATE_CONVERT} <= TO_DATE($3, 'DD.MM.YYYY')
                ORDER BY {SQL_DATE_CONVERT} ASC, lesson_number ASC
            """
            rows = await conn.fetch(query, subgroup, today_str, sunday_str)

        return [dict(r) for r in rows]

class DateRangeRequest(BaseModel):
    sdate: str
    edate: str

@app.post("/api/schedule/fetch_range")
async def fetch_schedule_range(data: DateRangeRequest, x_telegram_init_data: str = Header(...)):
    await fetch_and_save_schedule(sdate=data.sdate.strip(), edate=data.edate.strip())
    return await get_schedule(
        sdate=data.sdate.strip(), 
        edate=data.edate.strip(), 
        x_telegram_init_data=x_telegram_init_data
    )

@app.post("/api/schedule/sync_month")
async def sync_month_schedule():
    now = datetime.now()
    sdate = now.strftime("%d.%m.%Y")
    edate = (now + timedelta(days=30)).strftime("%d.%m.%Y")
    await fetch_and_save_schedule(sdate=sdate, edate=edate)
    return {"status": "success", "synced_from": sdate, "synced_to": edate}

class ReminderCreateRequest(BaseModel):
    title: str
    description: Optional[str] = ""
    start_at: Optional[str] = None
    deadline_at: str

@app.get("/api/reminders/active")
async def get_active_reminders(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = int(user.get("id"))

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT r.id, r.title, r.description,
                   to_char(r.start_at AT TIME ZONE 'Europe/Kyiv', 'DD.MM.YYYY HH24:MI') as start_str,
                   to_char(r.deadline_at AT TIME ZONE 'Europe/Kyiv', 'DD.MM.YYYY HH24:MI') as deadline_str,
                   r.deadline_at,
                   r.is_active,
                   (c.id IS NOT NULL) as is_completed_by_me,
                   (SELECT COUNT(*) FROM reminder_completions rc WHERE rc.reminder_id = r.id) as completed_count
            FROM reminders r
            LEFT JOIN reminder_completions c ON c.reminder_id = r.id AND c.telegram_id = $1
            WHERE r.is_active = TRUE AND r.deadline_at > NOW()
            ORDER BY r.deadline_at ASC
        """, tg_id)
        
        result = []
        for r in rows:
            d = dict(r)
            # Перетворюємо дедлайн в ISO-формат з таймзоною UTC
            if d.get("deadline_at"):
                d["deadline_at"] = d["deadline_at"].isoformat()
            result.append(d)
        return result

@app.post("/api/reminders/create")
async def create_reminder(data: ReminderCreateRequest, x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        u = await conn.fetchrow("SELECT role FROM users WHERE telegram_id = $1", int(user.get("id")))
        if not u or u["role"] != "starosta":
            raise HTTPException(status_code=403, detail="Тільки староста може створювати нагадування")

        deadline_utc = parse_to_utc(data.deadline_at)
        start_utc = parse_to_utc(data.start_at) if data.start_at else datetime.now(timezone.utc)

        rem_row = await conn.fetchrow("""
            INSERT INTO reminders (title, description, start_at, deadline_at)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """, data.title.strip(), data.description.strip(), start_utc, deadline_utc)
        reminder_id = rem_row["id"]

        students = await conn.fetch("SELECT telegram_id FROM users")

    # Відображення для повідомлення в боті по Києву
    deadline_kyiv_str = deadline_utc.astimezone(KYIV_TZ).strftime('%d.%m.%Y %H:%M')
    desc_block = f"{data.description.strip()}\n\n" if data.description.strip() else ""
    text = (
        f"⏳ <b>УВАГА! НОВИЙ ДЕДЛАЙН</b>\n\n"
        f"📌 <b>{data.title.strip()}</b>\n"
        f"{desc_block}"
        f"🕒 <b>Дедлайн:</b> до {deadline_kyiv_str}\n\n"
        f"<i>Коли здасте роботу, натисніть кнопку нижче, щоб вимкнути автонагадування.</i>"
    )

    kb = get_reminder_keyboard(reminder_id)
    for s in students:
        try:
            await bot.send_message(chat_id=s["telegram_id"], text=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    return {"status": "ok", "reminder_id": reminder_id}

@app.post("/api/reminders/complete")
async def toggle_complete_reminder(reminder_id: int = Query(...), x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = int(user.get("id"))

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM reminder_completions WHERE reminder_id = $1 AND telegram_id = $2",
            reminder_id, tg_id
        )
        if row:
            await conn.execute("DELETE FROM reminder_completions WHERE id = $1", row["id"])
            return {"status": "ok", "completed": False}
        else:
            await conn.execute(
                "INSERT INTO reminder_completions (reminder_id, telegram_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                reminder_id, tg_id
            )
            return {"status": "ok", "completed": True}

@app.post("/api/reminders/delete")
async def delete_reminder(reminder_id: int = Query(...), x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        u = await conn.fetchrow("SELECT role FROM users WHERE telegram_id = $1", int(user.get("id")))
        if not u or u["role"] != "starosta":
            raise HTTPException(status_code=403, detail="Доступ заборонено")

        await conn.execute("UPDATE reminders SET is_active = FALSE WHERE id = $1", reminder_id)
    return {"status": "ok"}

async def background_reminder_notifier():
    """Фоновий таск перевірки щохвилини для точного спрацьовування"""
    while True:
        try:
            await asyncio.sleep(60)
            pool = await get_db_pool()
            if not pool:
                continue

            async with pool.acquire() as conn:
                # 1. Завдання, дедлайн яких минув
                expired_reminders = await conn.fetch("""
                    SELECT id, title, description, deadline_at
                    FROM reminders
                    WHERE is_active = TRUE AND deadline_at <= NOW()
                """)

                for r in expired_reminders:
                    rem_id = r["id"]
                    d_kyiv = r["deadline_at"].astimezone(KYIV_TZ).strftime('%d.%m.%Y %H:%M')

                    completed_students = await conn.fetch("""
                        SELECT u.telegram_id, u.full_name
                        FROM users u
                        JOIN reminder_completions rc ON rc.telegram_id = u.telegram_id
                        WHERE rc.reminder_id = $1
                    """, rem_id)

                    debtors = await conn.fetch("""
                        SELECT u.telegram_id, u.full_name
                        FROM users u
                        LEFT JOIN reminder_completions rc 
                          ON rc.telegram_id = u.telegram_id AND rc.reminder_id = $1
                        WHERE rc.id IS NULL
                    """, rem_id)

                    # Сповіщення боржникам
                    expired_msg_student = (
                        f"🛑 <b>ТЕРМІН ЗДАЧІ ВИЧЕРПАНО!</b>\n\n"
                        f"📌 Завдання: <b>{r['title']}</b>\n"
                        f"Час дедлайну минув: <b>{d_kyiv}</b>.\n\n"
                        f"⚠️ <i>Ви не відмітили завдання як виконане. Робота вважається простроченою!</i>"
                    )

                    for s in debtors:
                        try:
                            await bot.send_message(
                                chat_id=s["telegram_id"],
                                text=expired_msg_student,
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass

                    # Звіт старості
                    if STAROSTA_TELEGRAM_ID:
                        debtors_list_str = "\n".join([f"• {d['full_name']}" for d in debtors]) or "— Немає (всі здали)"
                        completed_list_str = "\n".join([f"• {c['full_name']}" for c in completed_students]) or "— Ніхто не здав"

                        report_starosta = (
                            f"📋 <b>ДЕДЛАЙН ЗАВЕРШЕНО</b>\n\n"
                            f"📌 Завдання: <b>{r['title']}</b>\n"
                            f"Дедлайн був до: <b>{d_kyiv}</b>\n\n"
                            f"✅ <b>Здали вчасно ({len(completed_students)}):</b>\n{completed_list_str}\n\n"
                            f"❌ <b>Протермінували ({len(debtors)}):</b>\n{debtors_list_str}"
                        )
                        try:
                            await bot.send_message(
                                chat_id=STAROSTA_TELEGRAM_ID,
                                text=report_starosta,
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass

                    await conn.execute("UPDATE reminders SET is_active = FALSE WHERE id = $1", rem_id)

                # 2. Періодичні нагадування (менше 24 год, не частіше ніж раз на 6 год)
                active_reminders = await conn.fetch("""
                    SELECT id, title, description, deadline_at
                    FROM reminders
                    WHERE is_active = TRUE
                      AND deadline_at > NOW()
                      AND deadline_at <= NOW() + INTERVAL '24 hours'
                      AND (last_notified_at IS NULL OR last_notified_at <= NOW() - INTERVAL '6 hours')
                """)

                for r in active_reminders:
                    debtors = await conn.fetch("""
                        SELECT u.telegram_id 
                        FROM users u
                        LEFT JOIN reminder_completions rc 
                          ON rc.telegram_id = u.telegram_id AND rc.reminder_id = $1
                        WHERE rc.id IS NULL
                    """, r["id"])

                    if not debtors:
                        continue

                    d_kyiv = r["deadline_at"].astimezone(KYIV_TZ).strftime('%d.%m.%Y %H:%M')
                    desc_text = f"{r['description']}\n\n" if r['description'] else ""
                    msg = (
                        f"⏰ <b>НАГАДУВАННЯ ПРО ДЕДЛАЙН!</b>\n\n"
                        f"📌 <b>{r['title']}</b>\n"
                        f"Залишилося менше доби! Роботу потрібно здати до <b>{d_kyiv}</b>.\n"
                        f"{desc_text}"
                        f"<i>Якщо ви вже здали — обов'язково тисніть кнопку нижче:</i>"
                    )
                    kb = get_reminder_keyboard(r["id"])

                    for s in debtors:
                        try:
                            await bot.send_message(chat_id=s["telegram_id"], text=msg, parse_mode="HTML", reply_markup=kb)
                        except Exception:
                            pass

                    await conn.execute("UPDATE reminders SET last_notified_at = NOW() WHERE id = $1", r["id"])
        except Exception as e:
            print("Помилка нагадувача:", e)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(background_reminder_notifier())

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

@app.get("/api/group/stats")
async def get_group_stats(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        u = await conn.fetchrow("SELECT role FROM users WHERE telegram_id = $1", int(user.get("id")))
        if not u or u["role"] != "starosta":
            raise HTTPException(status_code=403, detail="Доступ заборонено")

        query = """
            SELECT 
                u.telegram_id,
                u.full_name,
                u.subgroup,
                COUNT(a.id) as total_checked,
                SUM(CASE WHEN a.status = 'present' THEN 1 ELSE 0 END) as present_count,
                SUM(CASE WHEN a.status = 'absent' THEN 1 ELSE 0 END) as absent_count,
                SUM(CASE WHEN a.status = 'late' THEN 1 ELSE 0 END) as late_count,
                SUM(CASE WHEN a.status = 'excused' THEN 1 ELSE 0 END) as excused_count
            FROM users u
            LEFT JOIN attendance a ON a.telegram_id = u.telegram_id
            GROUP BY u.telegram_id, u.full_name, u.subgroup
            ORDER BY u.full_name ASC
        """
        rows = await conn.fetch(query)
        result = []
        for r in rows:
            d = dict(r)
            total = d["total_checked"] or 0
            present = d["present_count"] or 0
            d["percent"] = round((present / total * 100)) if total > 0 else 100
            result.append(d)
        return result

@app.get("/api/attendance/export")
async def export_attendance_file(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        u = await conn.fetchrow("SELECT role FROM users WHERE telegram_id = $1", int(user.get("id")))
        if not u or u["role"] != "starosta":
            raise HTTPException(status_code=403, detail="Доступ заборонено")

        query = """
            SELECT 
                s.date_str,
                s.day_of_week,
                s.lesson_number,
                s.subject,
                u.full_name,
                u.subgroup,
                CASE 
                    WHEN a.status = 'present' THEN 'Присутній'
                    WHEN a.status = 'absent' THEN 'Пропуск (Н)'
                    WHEN a.status = 'late' THEN 'Запізнився'
                    WHEN a.status = 'excused' THEN 'Поважна причина'
                    ELSE 'Не відмічено'
                END as status_text
            FROM attendance a
            JOIN schedule s ON s.id = a.schedule_id
            JOIN users u ON u.telegram_id = a.telegram_id
            ORDER BY s.id ASC, u.full_name ASC
        """
        records = await conn.fetch(query)

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';')
    writer.writerow(["Дата", "День", "Пара", "Дисципліна", "ПІБ Студента", "Підгрупа", "Статус"])

    for r in records:
        writer.writerow([
            r["date_str"],
            r["day_of_week"],
            r["lesson_number"],
            r["subject"],
            r["full_name"],
            f"{r['subgroup']} підгрупа",
            r["status_text"]
        ])

    csv_data = output.getvalue().encode('utf-8-sig')
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=attendance_report_KI251.csv"
        }
    )

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

    formatted_message = linkify_telegram(data.message.strip())
    text = f"📢 <b>{data.title.strip()}</b>\n\n{formatted_message}\n\n<i>— Повідомлення від старости</i>"
    
    success_count = 0
    for s in students:
        try:
            await bot.send_message(
                chat_id=s["telegram_id"], 
                text=text, 
                parse_mode="HTML",
                disable_web_page_preview=False
            )
            success_count += 1
        except Exception:
            pass

    return {"sent_to": success_count}

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"

if WEBAPP_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(WEBAPP_DIR), html=True), name="webapp")
