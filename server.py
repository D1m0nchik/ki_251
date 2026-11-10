import os
import json
import hmac
import hashlib
import csv
import io
import re
import asyncio
import time
from urllib.parse import parse_qsl
from pathlib import Path
from typing import Dict, Optional, List, Literal
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Response, Query, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

from database import get_db_pool
from bot import bot
from parser import fetch_and_save_schedule

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
STAROSTA_TELEGRAM_ID = int(os.environ.get("STAROSTA_TELEGRAM_ID", 0))
CRON_SECRET = os.environ.get("CRON_SECRET", "")
ALLOWED_ORIGINS_RAW = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://ki-251.onrender.com,https://web.telegram.org"
)
ALLOWED_ORIGINS = [orig.strip() for orig in ALLOWED_ORIGINS_RAW.split(",") if orig.strip()]

KYIV_TZ = ZoneInfo("Europe/Kyiv")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"]
)

LESSON_TIMES = {
    1: ("08:00", "09:20"),
    2: ("09:30", "10:50"),
    3: ("11:00", "12:20"),
    4: ("12:50", "14:10"),
    5: ("14:20", "15:40"),
    6: ("15:50", "17:10"),
    7: ("17:20", "18:40")
}

DAYS_UA = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]

# ---------------------------------------------------------------------------
# Rate Limiting (захист пам'яті сервера)
# ---------------------------------------------------------------------------
class InMemoryRateLimiter:
    def __init__(self, requests_limit: int, time_window_seconds: int):
        self.limit = requests_limit
        self.window = time_window_seconds
        self.records: Dict[str, List[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str):
        now = time.monotonic()
        async with self._lock:
            timestamps = self.records.get(key, [])
            valid_timestamps = [t for t in timestamps if now - t < self.window]
            if len(valid_timestamps) >= self.limit:
                retry_after = int(self.window - (now - valid_timestamps[0]))
                raise HTTPException(
                    status_code=429,
                    detail=f"Забагато запитів. Зачекайте {max(retry_after, 1)} сек."
                )
            valid_timestamps.append(now)
            self.records[key] = valid_timestamps

sync_month_limiter = InMemoryRateLimiter(requests_limit=60, time_window_seconds=60)
fetch_range_limiter = InMemoryRateLimiter(requests_limit=60, time_window_seconds=60)
broadcast_limiter = InMemoryRateLimiter(requests_limit=15, time_window_seconds=60)
poll_create_limiter = InMemoryRateLimiter(requests_limit=15, time_window_seconds=60)
reminder_create_limiter = InMemoryRateLimiter(requests_limit=15, time_window_seconds=60)

# ---------------------------------------------------------------------------
# Авторизація Telegram initData
# ---------------------------------------------------------------------------
def verify_telegram_data(init_data: str) -> dict:
    if not init_data or not init_data.strip():
        raise HTTPException(
            status_code=401,
            detail="Неавторизований доступ: відсутні дані ініціалізації Telegram"
        )
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed_data:
            raise HTTPException(status_code=401, detail="Недійсний підпис запиту")

        hash_received = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(calculated_hash, hash_received):
            raise HTTPException(
                status_code=403,
                detail="Помилка перевірки цілісності: підроблений або недійсний підпис"
            )

        user_json = parsed_data.get("user")
        if not user_json:
            raise HTTPException(status_code=401, detail="Дані користувача відсутні в initData")

        return json.loads(user_json)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Помилка валідації Telegram-сесії")

async def get_current_user(x_telegram_init_data: str = Header(...)) -> dict:
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = int(user.get("id"))

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", tg_id)

        if not row and tg_id == STAROSTA_TELEGRAM_ID and STAROSTA_TELEGRAM_ID != 0:
            name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or "Староста"
            await conn.execute(
                "INSERT INTO users (telegram_id, full_name, subgroup, role, elective_group) VALUES ($1, $2, 1, 'starosta', 0)",
                tg_id, name
            )
            row = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", tg_id)

        if not row:
            raise HTTPException(
                status_code=403,
                detail="Доступ обмежено. Вас немає в базі групи КІ-25-1. Зверніться до старости."
            )

        if tg_id == STAROSTA_TELEGRAM_ID and row["role"] != "starosta":
            await conn.execute("UPDATE users SET role = 'starosta' WHERE telegram_id = $1", tg_id)
            row = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", tg_id)

        return dict(row)

async def require_starosta(current_user: dict = Depends(get_current_user)) -> dict:
    if current_user.get("role") != "starosta":
        raise HTTPException(status_code=403, detail="Доступ дозволено лише старості")
    return current_user

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

def build_poll_keyboard(poll_id: int, options: list, allow_custom: bool = False):
    inline_keyboard = []
    for opt in options:
        btn_text = f"🔘 {opt['text']}"
        inline_keyboard.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"poll_vote_{poll_id}_{opt['id']}")
        ])
    if allow_custom:
        inline_keyboard.append([
            InlineKeyboardButton(text="✍️ Інше (запропонувати свій варіант)", callback_data=f"poll_vote_custom_{poll_id}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)

def parse_to_utc(dt_str: str) -> datetime:
    clean_str = dt_str.replace("Z", "")
    dt_naive = datetime.fromisoformat(clean_str)
    if dt_naive.tzinfo is None:
        dt_kyiv = dt_naive.replace(tzinfo=KYIV_TZ)
    else:
        dt_kyiv = dt_naive.astimezone(KYIV_TZ)
    return dt_kyiv.astimezone(timezone.utc)

def is_within_lesson_window(lessons: list, now: datetime, window_minutes: int = 10):
    for l in lessons:
        raw_start = l.get("time_start")
        if not raw_start or not isinstance(raw_start, str):
            continue
        try:
            parts = raw_start.strip().split(":")
            if len(parts) != 2:
                continue
            h, m = int(parts[0]), int(parts[1])
            lesson_start_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            window_delta = timedelta(minutes=window_minutes)
            
            if (lesson_start_dt - window_delta) <= now <= (lesson_start_dt + window_delta):
                return True, {
                    "lesson_number": l.get("lesson_number", "?"),
                    "time_start": raw_start.strip(),
                    "subject": l.get("subject", "Заняття")
                }
        except Exception:
            continue

    return False, None

# ---------------------------------------------------------------------------
# Профіль
# ---------------------------------------------------------------------------
@app.get("/api/user_data")
async def get_user_data(current_user: dict = Depends(get_current_user)):
    return current_user

class ProfileUpdateRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=120)
    subgroup: Literal[1, 2] = Field(..., description="Підгрупа повинна бути строго 1 або 2")

@app.post("/api/user/profile")
async def update_profile(data: ProfileUpdateRequest, current_user: dict = Depends(get_current_user)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users 
            SET full_name = $1, subgroup = $2 
            WHERE telegram_id = $3
        """, data.full_name.strip(), data.subgroup, current_user["telegram_id"])
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Розклад
# ---------------------------------------------------------------------------
@app.get("/api/schedule/meta")
async def get_schedule_meta(current_user: dict = Depends(get_current_user)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        subj_rows = await conn.fetch("""
            SELECT DISTINCT subject FROM schedule 
            WHERE subject IS NOT NULL AND TRIM(subject) != ''
            ORDER BY subject ASC
        """)
        teach_rows = await conn.fetch("""
            SELECT DISTINCT teacher FROM schedule 
            WHERE teacher IS NOT NULL AND TRIM(teacher) != ''
            ORDER BY teacher ASC
        """)
        return {
            "subjects": [r["subject"].strip() for r in subj_rows],
            "teachers": [r["teacher"].strip() for r in teach_rows]
        }

class CreateOverrideRequest(BaseModel):
    title_note: Optional[str] = "Об'єднана пара"
    replaces_ids: List[int]
    target_date: str
    lesson_number: int = Field(..., ge=1, le=8)
    subject: str = Field(..., min_length=1)
    lesson_type: Optional[str] = "Лаб"
    teacher: Optional[str] = ""
    room: Optional[str] = ""
    meet_url: Optional[str] = ""
    elective_num: Optional[int] = Field(default=0, ge=0, le=2)
    target_subgroup: int = Field(default=0, ge=0, le=2)

@app.post("/api/schedule/override/create")
async def create_schedule_override(data: CreateOverrideRequest, starosta: dict = Depends(require_starosta)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        try:
            day_dt = datetime.strptime(data.target_date[:10], "%d.%m.%Y")
            day_name = DAYS_UA[day_dt.weekday()]
        except Exception:
            day_name = ""

        t_start, t_end = LESSON_TIMES.get(data.lesson_number, ("08:00", "09:20"))

        await conn.execute("""
            INSERT INTO schedule_overrides (
                title_note, replaces_schedule_ids, date_str, day_of_week,
                lesson_number, time_start, time_end, subject, lesson_type,
                teacher, room, target_subgroup, meet_url, elective_num
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        """, data.title_note.strip(), data.replaces_ids, data.target_date[:10],
             day_name, data.lesson_number, t_start, t_end, data.subject.strip(),
             data.lesson_type, data.teacher.strip(), data.room.strip(), data.target_subgroup,
             data.meet_url.strip(), data.elective_num)

    return {"status": "ok"}

@app.post("/api/schedule/override/delete")
async def delete_schedule_override(override_id: int = Query(...), starosta: dict = Depends(require_starosta)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM schedule_overrides WHERE id = $1", override_id)
    return {"status": "ok"}

@app.get("/api/schedule")
async def get_schedule(
    sdate: Optional[str] = Query(None),
    edate: Optional[str] = Query(None),
    original: bool = Query(False),
    current_user: dict = Depends(get_current_user)
):
    role = current_user.get("role", "student")
    subgroup = current_user.get("subgroup", 1)
    user_elective = current_user.get("elective_group", 0)

    now_kyiv = datetime.now(KYIV_TZ)

    # За замовчуванням показуємо від понеділка поточного тижня на 14 днів вперед
    if sdate and edate and len(sdate.strip()) >= 10 and len(edate.strip()) >= 10:
        filter_start = datetime.strptime(sdate.strip()[:10], "%d.%m.%Y").date()
        filter_end = datetime.strptime(edate.strip()[:10], "%d.%m.%Y").date()
    else:
        monday_of_week = now_kyiv.date() - timedelta(days=now_kyiv.weekday())
        filter_start = monday_of_week
        filter_end = monday_of_week + timedelta(days=14)

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        if original:
            rows = await conn.fetch("SELECT * FROM schedule WHERE LENGTH(date_str) >= 10")
            filtered = []
            for r in rows:
                try:
                    d = datetime.strptime(r["date_str"][:10], "%d.%m.%Y").date()
                    if filter_start <= d <= filter_end:
                        filtered.append(dict(r))
                except Exception:
                    continue
            filtered.sort(key=lambda x: (datetime.strptime(x["date_str"][:10], "%d.%m.%Y"), x.get("lesson_number", 0)))
            return filtered

        base_rows = await conn.fetch("SELECT * FROM schedule WHERE LENGTH(date_str) >= 10")
        overrides_rows = await conn.fetch("SELECT * FROM schedule_overrides WHERE LENGTH(date_str) >= 10")

        active_base = []
        for r in base_rows:
            try:
                d = datetime.strptime(r["date_str"][:10], "%d.%m.%Y").date()
                if filter_start <= d <= filter_end:
                    if role != "starosta":
                        if r["target_subgroup"] not in (0, subgroup):
                            continue
                        el = r.get("elective_num", 0) or 0
                        if el != 0 and user_elective != 0 and el != user_elective:
                            continue
                    active_base.append(dict(r))
            except Exception:
                continue

        replaced_ids = set()
        active_overrides = []
        for ov in overrides_rows:
            try:
                d = datetime.strptime(ov["date_str"][:10], "%d.%m.%Y").date()
                if filter_start <= d <= filter_end:
                    ov_dict = dict(ov)
                    if role != "starosta":
                        if ov_dict["target_subgroup"] != 0 and ov_dict["target_subgroup"] != subgroup:
                            continue
                        ov_el = ov_dict.get("elective_num", 0) or 0
                        if ov_el != 0 and user_elective != 0 and ov_el != user_elective:
                            continue
                    if ov_dict.get("replaces_schedule_ids"):
                        replaced_ids.update(ov_dict["replaces_schedule_ids"])
                    ov_dict["is_custom_override"] = True
                    active_overrides.append(ov_dict)
            except Exception:
                continue

        final_lessons = [r for r in active_base if r["id"] not in replaced_ids]
        final_lessons.extend(active_overrides)
        final_lessons.sort(key=lambda x: (datetime.strptime(x["date_str"][:10], "%d.%m.%Y"), x.get("lesson_number", 0)))
        return final_lessons

class DateRangeRequest(BaseModel):
    sdate: str = Field(..., pattern=r"^\d{2}\.\d{2}\.\d{4}$")
    edate: str = Field(..., pattern=r"^\d{2}\.\d{2}\.\d{4}$")

@app.post("/api/schedule/fetch_range")
async def fetch_schedule_range(
    data: DateRangeRequest,
    x_telegram_init_data: str = Header(...),
    current_user: dict = Depends(get_current_user)
):
    await fetch_range_limiter.check(f"fetch_range_{current_user['telegram_id']}")
    await fetch_and_save_schedule(sdate=data.sdate.strip(), edate=data.edate.strip())
    return await get_schedule(
        sdate=data.sdate.strip(), 
        edate=data.edate.strip(), 
        original=False,
        current_user=current_user
    )

@app.post("/api/schedule/sync_month")
async def sync_month_schedule(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_cron_secret: Optional[str] = Header(default=None)
):
    cron_authenticated = False
    if x_cron_secret and CRON_SECRET:
        if hmac.compare_digest(x_cron_secret.strip(), CRON_SECRET.strip()):
            cron_authenticated = True

    caller_id = "cron"
    if not cron_authenticated:
        if not x_telegram_init_data:
            raise HTTPException(
                status_code=401,
                detail="Відсутні облікові дані: потрібен заголовок Telegram initData або X-Cron-Secret"
            )
        user = await get_current_user(x_telegram_init_data=x_telegram_init_data)
        caller_id = str(user["telegram_id"])

    # Дозволяємо старості запускати оновлення без блокування таймером
    if caller_id != str(STAROSTA_TELEGRAM_ID):
        await sync_month_limiter.check(f"sync_month_{caller_id}")

    try:
        now_dt = datetime.now(KYIV_TZ)
        sdate = (now_dt - timedelta(days=now_dt.weekday())).strftime("%d.%m.%Y")
        edate = (now_dt + timedelta(days=21)).strftime("%d.%m.%Y")
        
        success = await fetch_and_save_schedule(sdate=sdate, edate=edate)
        if not success:
            raise HTTPException(
                status_code=502, 
                detail="Сайт університету тимчасово не віддає пари або розклад на цей період порожній."
            )
            
        return {"status": "success", "synced_from": sdate, "synced_to": edate}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Помилка парсера: {str(e)}")

# ---------------------------------------------------------------------------
# Опитування
# ---------------------------------------------------------------------------
class PollCreateRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=300)
    options: List[str] = Field(..., min_length=2, max_length=10)
    is_multiple: bool = False
    allow_custom: bool = False
    deadline_at: Optional[str] = None

class PollVoteRequest(BaseModel):
    poll_id: int
    option_id: int
    custom_text: Optional[str] = Field(default="", max_length=200)

@app.get("/api/polls")
async def get_polls(current_user: dict = Depends(get_current_user)):
    tg_id = current_user["telegram_id"]
    is_starosta = current_user.get("role") == "starosta"

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        poll_rows = await conn.fetch("""
            SELECT id, question, options, is_multiple, is_active, allow_custom,
                   to_char(deadline_at AT TIME ZONE 'Europe/Kyiv', 'DD.MM.YYYY HH24:MI') as deadline_str,
                   to_char(created_at AT TIME ZONE 'Europe/Kyiv', 'DD.MM.YYYY HH24:MI') as created_str,
                   created_at,
                   deadline_at
            FROM polls
            ORDER BY id DESC LIMIT 15
        """)

        result = []
        for p in poll_rows:
            p_dict = dict(p)
            opts = json.loads(p_dict["options"]) if isinstance(p_dict["options"], str) else p_dict["options"]
            
            votes = await conn.fetch("SELECT option_id, telegram_id, custom_text FROM poll_votes WHERE poll_id = $1", p_dict["id"])
            total_voters = len(set([v["telegram_id"] for v in votes]))
            my_votes = [v["option_id"] for v in votes if v["telegram_id"] == tg_id]
            
            my_custom_vote = next((v["custom_text"] for v in votes if v["telegram_id"] == tg_id and v["option_id"] == 999), "")

            for opt in opts:
                count = sum(1 for v in votes if v["option_id"] == opt["id"])
                opt["votes_count"] = count
                opt["percent"] = round((count / total_voters * 100)) if total_voters > 0 else 0

            custom_votes_count = sum(1 for v in votes if v["option_id"] == 999)
            custom_percent = round((custom_votes_count / total_voters * 100)) if total_voters > 0 else 0

            p_dict["options"] = opts
            p_dict["custom_votes_count"] = custom_votes_count
            p_dict["custom_percent"] = custom_percent
            p_dict["total_voters"] = total_voters
            p_dict["my_votes"] = my_votes
            p_dict["my_custom_vote"] = my_custom_vote
            if p_dict.get("created_at"):
                p_dict["created_at_iso"] = p_dict["created_at"].isoformat()

            if is_starosta:
                voters_details = await conn.fetch("""
                    SELECT pv.option_id, pv.custom_text, u.full_name, u.subgroup 
                    FROM poll_votes pv
                    JOIN users u ON u.telegram_id = pv.telegram_id
                    WHERE pv.poll_id = $1
                    ORDER BY u.full_name ASC
                """, p_dict["id"])
                
                non_voters = await conn.fetch("""
                    SELECT u.full_name, u.subgroup 
                    FROM users u
                    WHERE u.telegram_id NOT IN (SELECT telegram_id FROM poll_votes WHERE poll_id = $1)
                    ORDER BY u.full_name ASC
                """, p_dict["id"])

                p_dict["voters_details"] = [dict(v) for v in voters_details]
                p_dict["non_voters"] = [dict(nv) for nv in non_voters]

            result.append(p_dict)

        return result

@app.post("/api/polls/create")
async def create_poll(data: PollCreateRequest, starosta: dict = Depends(require_starosta)):
    await poll_create_limiter.check(f"poll_create_{starosta['telegram_id']}")

    clean_options = [opt.strip() for opt in data.options if opt.strip()]
    if len(clean_options) < 2:
        raise HTTPException(status_code=400, detail="Потрібно мінімум 2 варіанти відповіді")

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        deadline_utc = parse_to_utc(data.deadline_at) if data.deadline_at else None
        options_json = [{"id": idx + 1, "text": opt} for idx, opt in enumerate(clean_options)]

        poll_row = await conn.fetchrow("""
            INSERT INTO polls (question, options, is_multiple, allow_custom, deadline_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """, data.question.strip(), json.dumps(options_json, ensure_ascii=False), data.is_multiple, data.allow_custom, deadline_utc)
        poll_id = poll_row["id"]
        students = await conn.fetch("SELECT telegram_id FROM users")

    type_str = "Кілька варіантів" if data.is_multiple else "Один варіант"
    custom_str = " · Можна свій варіант" if data.allow_custom else ""
    deadline_str = f"\n⏳ <b>До:</b> {deadline_utc.astimezone(KYIV_TZ).strftime('%d.%m.%Y %H:%M')}" if deadline_utc else ""
    
    text = (
        f"📊 <b>НОВЕ ОПИТУВАННЯ ВІД СТАРОСТИ</b>\n\n"
        f"❓ <b>{data.question.strip()}</b>\n"
        f"ℹ️ <i>Тип: {type_str}{custom_str}</i>{deadline_str}\n\n"
        f"<i>Натисніть кнопку під цим повідомленням, щоб проголосувати, або відкрийте вкладку «Новини» у додатку:</i>"
    )

    kb = build_poll_keyboard(poll_id, options_json, data.allow_custom)
    for s in students:
        try:
            await bot.send_message(chat_id=s["telegram_id"], text=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    return {"status": "ok", "poll_id": poll_id}

@app.post("/api/polls/vote")
async def vote_poll(data: PollVoteRequest, current_user: dict = Depends(get_current_user)):
    tg_id = current_user["telegram_id"]

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        poll = await conn.fetchrow("SELECT is_active, is_multiple, allow_custom FROM polls WHERE id = $1", data.poll_id)
        if not poll or not poll["is_active"]:
            raise HTTPException(status_code=400, detail="Опитування вже закрито")

        if data.option_id == 999 and not poll["allow_custom"]:
            raise HTTPException(status_code=400, detail="Власний варіант заборонено в цьому опитуванні")

        existing = await conn.fetchrow(
            "SELECT id FROM poll_votes WHERE poll_id = $1 AND telegram_id = $2 AND option_id = $3",
            data.poll_id, tg_id, data.option_id
        )

        if existing and data.option_id != 999:
            await conn.execute("DELETE FROM poll_votes WHERE id = $1", existing["id"])
            return {"status": "removed"}
        else:
            if not poll["is_multiple"]:
                await conn.execute("DELETE FROM poll_votes WHERE poll_id = $1 AND telegram_id = $2", data.poll_id, tg_id)
            
            await conn.execute("""
                INSERT INTO poll_votes (poll_id, telegram_id, option_id, custom_text)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (poll_id, telegram_id, option_id) 
                DO UPDATE SET custom_text = EXCLUDED.custom_text
            """, data.poll_id, tg_id, data.option_id, data.custom_text.strip())
            return {"status": "voted"}

@app.post("/api/polls/close")
async def close_poll(poll_id: int = Query(...), starosta: dict = Depends(require_starosta)):
    tg_id = starosta["telegram_id"]

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        poll = await conn.fetchrow("SELECT * FROM polls WHERE id = $1", poll_id)
        if not poll:
            raise HTTPException(status_code=404, detail="Опитування не знайдено")

        await conn.execute("UPDATE polls SET is_active = FALSE WHERE id = $1", poll_id)

        opts = json.loads(poll["options"]) if isinstance(poll["options"], str) else poll["options"]
        votes = await conn.fetch("SELECT option_id, telegram_id, custom_text FROM poll_votes WHERE poll_id = $1", poll_id)
        total_voters = len(set([v["telegram_id"] for v in votes]))

        report_lines = [f"📊 <b>ПІДСУМКИ ОПИТУВАННЯ ЗАВЕРШЕНО:</b>\n\n❓ <b>{poll['question']}</b>\n👥 Всього проголосувало: <b>{total_voters}</b>\n"]
        for opt in opts:
            count = sum(1 for v in votes if v["option_id"] == opt["id"])
            pct = round((count / total_voters * 100)) if total_voters > 0 else 0
            report_lines.append(f"• <b>{opt['text']}</b>: {count} голосів ({pct}%)")

        custom_votes = [v for v in votes if v["option_id"] == 999]
        if custom_votes:
            c_pct = round((len(custom_votes) / total_voters * 100)) if total_voters > 0 else 0
            report_lines.append(f"• <b>Інше (свої варіанти)</b>: {len(custom_votes)} голосів ({c_pct}%):")
            for cv in custom_votes:
                if cv["custom_text"]:
                    report_lines.append(f"  — «{cv['custom_text']}»")

        final_msg = "\n".join(report_lines)

    try:
        await bot.send_message(chat_id=tg_id, text=final_msg, parse_mode="HTML")
    except Exception:
        pass

    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Дедлайни
# ---------------------------------------------------------------------------
class ReminderCreateRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=200)
    description: Optional[str] = Field(default="", max_length=1000)
    start_at: Optional[str] = None
    deadline_at: str

@app.get("/api/reminders/active")
async def get_active_reminders(current_user: dict = Depends(get_current_user)):
    tg_id = current_user["telegram_id"]

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
            if d.get("deadline_at"):
                d["deadline_at"] = d["deadline_at"].isoformat()
            result.append(d)
        return result

@app.post("/api/reminders/create")
async def create_reminder(data: ReminderCreateRequest, starosta: dict = Depends(require_starosta)):
    await reminder_create_limiter.check(f"reminder_create_{starosta['telegram_id']}")

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        deadline_utc = parse_to_utc(data.deadline_at)
        start_utc = parse_to_utc(data.start_at) if data.start_at else datetime.now(timezone.utc)

        rem_row = await conn.fetchrow("""
            INSERT INTO reminders (title, description, start_at, deadline_at)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """, data.title.strip(), data.description.strip(), start_utc, deadline_utc)
        reminder_id = rem_row["id"]
        students = await conn.fetch("SELECT telegram_id FROM users")

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
async def toggle_complete_reminder(reminder_id: int = Query(...), current_user: dict = Depends(get_current_user)):
    tg_id = current_user["telegram_id"]

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
async def delete_reminder(reminder_id: int = Query(...), starosta: dict = Depends(require_starosta)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE reminders SET is_active = FALSE WHERE id = $1", reminder_id)
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Фоновий моніторинг (розумний скрапінг поточного дня)
# ---------------------------------------------------------------------------
async def background_reminder_notifier():
    while True:
        try:
            await asyncio.sleep(60)
            now_kyiv = datetime.now(KYIV_TZ)
            today_str = now_kyiv.strftime("%d.%m.%Y")

            pool = await get_db_pool()
            if not pool:
                continue

            if now_kyiv.weekday() < 5 and 8 <= now_kyiv.hour <= 18:
                try:
                    async with pool.acquire() as conn:
                        today_schedule = await conn.fetch("""
                            SELECT id, lesson_number, time_start, subject 
                            FROM schedule 
                            WHERE SUBSTRING(date_str, 1, 10) = $1
                        """, today_str)

                        today_overrides = await conn.fetch("""
                            SELECT id, lesson_number, time_start, subject, replaces_schedule_ids 
                            FROM schedule_overrides 
                            WHERE SUBSTRING(date_str, 1, 10) = $1
                        """, today_str)

                        replaced_ids = set()
                        for ov in today_overrides:
                            if ov["replaces_schedule_ids"]:
                                replaced_ids.update(ov["replaces_schedule_ids"])

                        active_today = [dict(s) for s in today_schedule if s["id"] not in replaced_ids]
                        active_today.extend([dict(ov) for ov in today_overrides])

                    if not active_today:
                        print(f"[{now_kyiv.strftime('%H:%M:%S')}] Скрапінг пропущено: на сьогодні ({today_str}) пар немає.")
                    else:
                        in_window, match = is_within_lesson_window(active_today, now_kyiv, window_minutes=10)
                        if in_window:
                            print(f"[{now_kyiv.strftime('%H:%M:%S')}] Скрапінг запущено: вікно {match['lesson_number']} пари ({match['time_start']}) — «{match['subject']}».")
                            await fetch_and_save_schedule(sdate=today_str, edate=today_str)
                        else:
                            print(f"[{now_kyiv.strftime('%H:%M:%S')}] Скрапінг пропущено: не час (немає пар у вікні ±10 хв).")
                except Exception as smart_parse_err:
                    print("Помилка розумного скрапінгу дня:", smart_parse_err)

            async with pool.acquire() as conn:
                expired_polls = await conn.fetch("SELECT id, question FROM polls WHERE is_active = TRUE AND deadline_at <= NOW()")
                for ep in expired_polls:
                    await conn.execute("UPDATE polls SET is_active = FALSE WHERE id = $1", ep["id"])

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

                    expired_msg_student = (
                        f"🛑 <b>ТЕРМІН ЗДАЧІ ВИЧЕРПАНО!</b>\n\n"
                        f"📌 Завдання: <b>{r['title']}</b>\n"
                        f"Час дедлайну минув: <b>{d_kyiv}</b>.\n\n"
                        f"⚠️ <i>Ви не відмітили завдання як виконане. Робота вважається простроченою!</i>"
                    )

                    for s in debtors:
                        try:
                            await bot.send_message(chat_id=s["telegram_id"], text=expired_msg_student, parse_mode="HTML")
                        except Exception:
                            pass

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
                            await bot.send_message(chat_id=STAROSTA_TELEGRAM_ID, text=report_starosta, parse_mode="HTML")
                        except Exception:
                            pass

                    await conn.execute("UPDATE reminders SET is_active = FALSE WHERE id = $1", rem_id)

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

# ---------------------------------------------------------------------------
# Керування групою
# ---------------------------------------------------------------------------
@app.get("/api/group/students")
async def get_all_group_students(starosta: dict = Depends(require_starosta)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT telegram_id, full_name, subgroup, role, COALESCE(elective_group, 0) as elective_group 
            FROM users 
            ORDER BY full_name ASC
        """)
        return [dict(r) for r in rows]

class ChangeSubgroupRequest(BaseModel):
    student_tg_id: int
    subgroup: Literal[1, 2]

@app.post("/api/group/change_subgroup")
async def change_student_subgroup(data: ChangeSubgroupRequest, starosta: dict = Depends(require_starosta)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET subgroup = $1 WHERE telegram_id = $2", data.subgroup, data.student_tg_id)
    return {"status": "ok"}

class ChangeElectiveRequest(BaseModel):
    student_tg_id: int
    elective_group: int = Field(..., ge=0, le=2)

@app.post("/api/group/change_elective")
async def change_student_elective(data: ChangeElectiveRequest, starosta: dict = Depends(require_starosta)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET elective_group = $1 WHERE telegram_id = $2", data.elective_group, data.student_tg_id)
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Журнал та відвідуваність
# ---------------------------------------------------------------------------
@app.get("/api/attendance/students")
async def get_students_for_lesson(
    schedule_id: int, 
    is_override: bool = Query(default=False),
    starosta: dict = Depends(require_starosta)
):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        if is_override:
            lesson = await conn.fetchrow(
                "SELECT target_subgroup, COALESCE(elective_num, 0) as elective_num FROM schedule_overrides WHERE id = $1", 
                schedule_id
            )
        else:
            lesson = await conn.fetchrow(
                "SELECT target_subgroup, COALESCE(elective_num, 0) as elective_num FROM schedule WHERE id = $1", 
                schedule_id
            )

        target_sg = lesson["target_subgroup"] if lesson else 0
        el_num = lesson["elective_num"] if lesson else 0

        if is_override:
            query = """
                SELECT u.telegram_id, u.full_name, u.subgroup, COALESCE(a.status, 'absent') as status
                FROM users u
                LEFT JOIN attendance a ON a.telegram_id = u.telegram_id AND a.override_id = $1
                WHERE ($2 = 0 OR u.subgroup = $3)
                  AND ($4 = 0 OR COALESCE(u.elective_group, 0) = 0 OR u.elective_group = $4)
                ORDER BY u.full_name ASC
            """
        else:
            query = """
                SELECT u.telegram_id, u.full_name, u.subgroup, COALESCE(a.status, 'absent') as status
                FROM users u
                LEFT JOIN attendance a ON a.telegram_id = u.telegram_id AND a.schedule_id = $1
                WHERE ($2 = 0 OR u.subgroup = $3)
                  AND ($4 = 0 OR COALESCE(u.elective_group, 0) = 0 OR u.elective_group = $4)
                ORDER BY u.full_name ASC
            """

        rows = await conn.fetch(query, schedule_id, target_sg, target_sg, el_num)
        return [dict(r) for r in rows]

class AttendanceSaveRequest(BaseModel):
    schedule_id: int
    is_override: bool = False
    records: Dict[int, Literal["present", "absent", "late", "excused"]]

@app.post("/api/attendance/save")
async def save_attendance(data: AttendanceSaveRequest, starosta: dict = Depends(require_starosta)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for tg_id_str, status in data.records.items():
                tg_id = int(tg_id_str)
                if data.is_override:
                    await conn.execute("""
                        DELETE FROM attendance 
                        WHERE override_id = $1 AND telegram_id = $2
                    """, data.schedule_id, tg_id)
                    await conn.execute("""
                        INSERT INTO attendance (override_id, telegram_id, status)
                        VALUES ($1, $2, $3)
                    """, data.schedule_id, tg_id, status)
                else:
                    await conn.execute("""
                        DELETE FROM attendance 
                        WHERE schedule_id = $1 AND telegram_id = $2
                    """, data.schedule_id, tg_id)
                    await conn.execute("""
                        INSERT INTO attendance (schedule_id, telegram_id, status)
                        VALUES ($1, $2, $3)
                    """, data.schedule_id, tg_id, status)
    return {"status": "ok"}

@app.get("/api/attendance/calendar_meta")
async def get_attendance_calendar_meta(
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    starosta: dict = Depends(require_starosta)
):
    target_year = str(year)
    target_month = f"{int(month):02d}"

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT SUBSTRING(date_str, 1, 2)::int as day_num
            FROM (
                SELECT s.date_str
                FROM attendance a
                JOIN schedule s ON s.id = a.schedule_id
                WHERE a.schedule_id IS NOT NULL
                  AND LENGTH(s.date_str) >= 10
                  AND SUBSTRING(s.date_str, 4, 2) = $1
                  AND SUBSTRING(s.date_str, 7, 4) = $2
                UNION
                SELECT ov.date_str
                FROM attendance a
                JOIN schedule_overrides ov ON ov.id = a.override_id
                WHERE a.override_id IS NOT NULL
                  AND LENGTH(ov.date_str) >= 10
                  AND SUBSTRING(ov.date_str, 4, 2) = $1
                  AND SUBSTRING(ov.date_str, 7, 4) = $2
            ) combined
        """, target_month, target_year)

        marked_days = [r["day_num"] for r in rows if r["day_num"] is not None]
        return {"year": year, "month": month, "marked_days": marked_days}

@app.get("/api/attendance/day_matrix")
async def get_day_matrix(
    date_str: str = Query(..., pattern=r"^\d{2}\.\d{2}\.\d{4}$"),
    starosta: dict = Depends(require_starosta)
):
    clean_date = date_str[:10]
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        students = await conn.fetch("SELECT telegram_id, full_name, subgroup, COALESCE(elective_group, 0) as elective_group FROM users ORDER BY full_name ASC")

        lessons = await conn.fetch("""
            SELECT id, lesson_number, time_start, time_end, subject, lesson_type, room, target_subgroup, meet_url, COALESCE(elective_num, 0) as elective_num, FALSE as is_override
            FROM schedule
            WHERE SUBSTRING(date_str, 1, 10) = $1
            ORDER BY lesson_number ASC, target_subgroup ASC
        """, clean_date)

        overrides = await conn.fetch("""
            SELECT id, lesson_number, time_start, time_end, subject, lesson_type, room, target_subgroup, meet_url, COALESCE(elective_num, 0) as elective_num, TRUE as is_override, replaces_schedule_ids
            FROM schedule_overrides
            WHERE SUBSTRING(date_str, 1, 10) = $1
            ORDER BY lesson_number ASC, target_subgroup ASC
        """, clean_date)

        replaces_ids = set()
        active_ovs = []
        for ov in overrides:
            ov_dict = dict(ov)
            if ov_dict.get("replaces_schedule_ids"):
                replaces_ids.update(ov_dict["replaces_schedule_ids"])
            active_ovs.append(ov_dict)

        combined_lessons = [dict(l) for l in lessons if l["id"] not in replaces_ids]
        combined_lessons.extend(active_ovs)
        combined_lessons.sort(key=lambda x: x["lesson_number"])

        att_rows = await conn.fetch("""
            SELECT a.schedule_id, a.override_id, a.telegram_id, a.status
            FROM attendance a
            LEFT JOIN schedule s ON s.id = a.schedule_id
            LEFT JOIN schedule_overrides ov ON ov.id = a.override_id
            WHERE (s.id IS NOT NULL AND SUBSTRING(s.date_str, 1, 10) = $1)
               OR (ov.id IS NOT NULL AND SUBSTRING(ov.date_str, 1, 10) = $1)
        """, clean_date)

        att_map = {}
        for r in att_rows:
            if r["override_id"]:
                key = f"ov_{r['override_id']}_{r['telegram_id']}"
            else:
                key = f"sc_{r['schedule_id']}_{r['telegram_id']}"
            att_map[key] = r["status"]

        return {
            "date_str": clean_date,
            "students": [dict(s) for s in students],
            "lessons": combined_lessons,
            "matrix": att_map
        }

class CellToggleRequest(BaseModel):
    schedule_id: int
    telegram_id: int
    is_override: bool = False

@app.post("/api/attendance/cell_toggle")
async def toggle_cell_attendance(data: CellToggleRequest, starosta: dict = Depends(require_starosta)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        if data.is_override:
            curr = await conn.fetchval("""
                SELECT status FROM attendance WHERE override_id = $1 AND telegram_id = $2
            """, data.schedule_id, data.telegram_id)
        else:
            curr = await conn.fetchval("""
                SELECT status FROM attendance WHERE schedule_id = $1 AND telegram_id = $2
            """, data.schedule_id, data.telegram_id)

        next_status_map = {
            "present": "absent",
            "absent": "late",
            "late": "excused",
            "excused": "present",
            None: "present"
        }
        new_status = next_status_map.get(curr, "present")

        if data.is_override:
            await conn.execute("""
                DELETE FROM attendance 
                WHERE override_id = $1 AND telegram_id = $2
            """, data.schedule_id, data.telegram_id)
            await conn.execute("""
                INSERT INTO attendance (override_id, telegram_id, status)
                VALUES ($1, $2, $3)
            """, data.schedule_id, data.telegram_id, new_status)
        else:
            await conn.execute("""
                DELETE FROM attendance 
                WHERE schedule_id = $1 AND telegram_id = $2
            """, data.schedule_id, data.telegram_id)
            await conn.execute("""
                INSERT INTO attendance (schedule_id, telegram_id, status)
                VALUES ($1, $2, $3)
            """, data.schedule_id, data.telegram_id, new_status)

        return {"status": "ok", "new_status": new_status}

def build_matrix_csv(students, lessons, att_dict) -> bytes:
    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';')

    header = ["№", "ПІБ Студента", "Підгр.", "Вибіркова"]
    for l in lessons:
        d_str = l.get('date_str', '')[:5]
        l_num = l.get('lesson_number', '')
        header.append(f"{d_str} ({l_num}п)")
    writer.writerow(header)

    status_chars = {
        "present": "✓",
        "absent": "Н",
        "late": "З",
        "excused": "П"
    }

    for idx, st in enumerate(students, start=1):
        el_label = "Не обрано" if st["elective_group"] == 0 else f"Вибір {st['elective_group']}"
        row = [idx, st["full_name"], f"{st['subgroup']} підгр.", el_label]
        for l in lessons:
            prefix = "ov_" if l.get("is_override") else "sc_"
            st_key = f"{prefix}{l['id']}_{st['telegram_id']}"
            val = att_dict.get(st_key, "")
            row.append(status_chars.get(val, ""))
        writer.writerow(row)

    return output.getvalue().encode('utf-8-sig')

@app.post("/api/attendance/send_to_telegram")
async def send_attendance_matrix_to_telegram(
    month: Optional[int] = Query(None, ge=1, le=12),
    year: Optional[int] = Query(None, ge=2020, le=2100),
    starosta: dict = Depends(require_starosta)
):
    tg_id = starosta["telegram_id"]
    now = datetime.now()
    target_year = str(year or now.year)
    target_month = f"{int(month or now.month):02d}"

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        students = await conn.fetch("""
            SELECT telegram_id, full_name, subgroup, COALESCE(elective_group, 0) as elective_group 
            FROM users 
            ORDER BY full_name ASC
        """)

        lessons = await conn.fetch("""
            SELECT id, date_str, lesson_number, subject, FALSE as is_override
            FROM schedule
            WHERE LENGTH(date_str) >= 10
              AND SUBSTRING(date_str, 4, 2) = $1
              AND SUBSTRING(date_str, 7, 4) = $2
            ORDER BY SUBSTRING(date_str, 1, 2) ASC, lesson_number ASC
        """, target_month, target_year)

        overrides = await conn.fetch("""
            SELECT id, date_str, lesson_number, subject, TRUE as is_override, replaces_schedule_ids
            FROM schedule_overrides
            WHERE LENGTH(date_str) >= 10
              AND SUBSTRING(date_str, 4, 2) = $1
              AND SUBSTRING(date_str, 7, 4) = $2
            ORDER BY SUBSTRING(date_str, 1, 2) ASC, lesson_number ASC
        """, target_month, target_year)

        replaces_ids = set()
        active_ovs = []
        for ov in overrides:
            d = dict(ov)
            if d.get("replaces_schedule_ids"):
                replaces_ids.update(d["replaces_schedule_ids"])
            active_ovs.append(d)

        combined_lessons = [dict(l) for l in lessons if l["id"] not in replaces_ids]
        combined_lessons.extend(active_ovs)

        def safe_sort_key(x):
            try:
                d = x.get("date_str", "")[:10]
                return (int(d[6:10]), int(d[3:5]), int(d[0:2]), int(x.get("lesson_number", 0)))
            except Exception:
                return (0, 0, 0, 0)

        combined_lessons.sort(key=safe_sort_key)

        att_records = await conn.fetch("""
            SELECT a.schedule_id, a.override_id, a.telegram_id, a.status
            FROM attendance a
            LEFT JOIN schedule s ON s.id = a.schedule_id
            LEFT JOIN schedule_overrides ov ON ov.id = a.override_id
            WHERE (s.id IS NOT NULL AND LENGTH(s.date_str) >= 10 AND SUBSTRING(s.date_str, 4, 2) = $1 AND SUBSTRING(s.date_str, 7, 4) = $2)
               OR (ov.id IS NOT NULL AND LENGTH(ov.date_str) >= 10 AND SUBSTRING(ov.date_str, 4, 2) = $1 AND SUBSTRING(ov.date_str, 7, 4) = $2)
        """, target_month, target_year)

        att_dict = {}
        for r in att_records:
            if r["override_id"]:
                att_dict[f"ov_{r['override_id']}_{r['telegram_id']}"] = r["status"]
            else:
                att_dict[f"sc_{r['schedule_id']}_{r['telegram_id']}"] = r["status"]

    csv_bytes = build_matrix_csv(students, combined_lessons, att_dict)
    filename = f"journal_KI251_{target_month}_{target_year}.csv"

    doc = BufferedInputFile(csv_bytes, filename=filename)
    try:
        await bot.send_document(
            chat_id=tg_id,
            document=doc,
            caption=f"📋 <b>Відомість відвідуваності групи КІ-25-1</b>\n📅 Період: <b>{target_month}.{target_year}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"Помилка відправки відомості ботом у чат {tg_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Помилка надсилання в Telegram: {str(e)}")

    return {"status": "ok", "filename": filename}

@app.get("/api/attendance/export_matrix")
async def export_attendance_matrix_report(
    month: Optional[int] = Query(None, ge=1, le=12),
    year: Optional[int] = Query(None, ge=2020, le=2100),
    starosta: dict = Depends(require_starosta)
):
    now = datetime.now()
    target_year = str(year or now.year)
    target_month = f"{int(month or now.month):02d}"

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        students = await conn.fetch("SELECT telegram_id, full_name, subgroup, COALESCE(elective_group, 0) as elective_group FROM users ORDER BY full_name ASC")

        lessons = await conn.fetch("""
            SELECT id, date_str, lesson_number, subject, FALSE as is_override
            FROM schedule
            WHERE LENGTH(date_str) >= 10
              AND SUBSTRING(date_str, 4, 2) = $1
              AND SUBSTRING(date_str, 7, 4) = $2
            ORDER BY SUBSTRING(date_str, 1, 2) ASC, lesson_number ASC
        """, target_month, target_year)

        overrides = await conn.fetch("""
            SELECT id, date_str, lesson_number, subject, TRUE as is_override, replaces_schedule_ids
            FROM schedule_overrides
            WHERE LENGTH(date_str) >= 10
              AND SUBSTRING(date_str, 4, 2) = $1
              AND SUBSTRING(date_str, 7, 4) = $2
            ORDER BY SUBSTRING(date_str, 1, 2) ASC, lesson_number ASC
        """, target_month, target_year)

        replaces_ids = set()
        active_ovs = []
        for ov in overrides:
            d = dict(ov)
            if d.get("replaces_schedule_ids"):
                replaces_ids.update(d["replaces_schedule_ids"])
            active_ovs.append(d)

        combined_lessons = [dict(l) for l in lessons if l["id"] not in replaces_ids]
        combined_lessons.extend(active_ovs)

        def safe_sort_key(x):
            try:
                d = x.get("date_str", "")[:10]
                return (int(d[6:10]), int(d[3:5]), int(d[0:2]), int(x.get("lesson_number", 0)))
            except Exception:
                return (0, 0, 0, 0)

        combined_lessons.sort(key=safe_sort_key)

        att_records = await conn.fetch("""
            SELECT a.schedule_id, a.override_id, a.telegram_id, a.status
            FROM attendance a
            LEFT JOIN schedule s ON s.id = a.schedule_id
            LEFT JOIN schedule_overrides ov ON ov.id = a.override_id
            WHERE (s.id IS NOT NULL AND LENGTH(s.date_str) >= 10 AND SUBSTRING(s.date_str, 4, 2) = $1 AND SUBSTRING(s.date_str, 7, 4) = $2)
               OR (ov.id IS NOT NULL AND LENGTH(ov.date_str) >= 10 AND SUBSTRING(ov.date_str, 4, 2) = $1 AND SUBSTRING(ov.date_str, 7, 4) = $2)
        """, target_month, target_year)

        att_dict = {}
        for r in att_records:
            if r["override_id"]:
                att_dict[f"ov_{r['override_id']}_{r['telegram_id']}"] = r["status"]
            else:
                att_dict[f"sc_{r['schedule_id']}_{r['telegram_id']}"] = r["status"]

    csv_bytes = build_matrix_csv(students, combined_lessons, att_dict)
    filename = f"journal_KI251_{target_month}_{target_year}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Access-Control-Expose-Headers": "Content-Disposition"
        }
    )

@app.get("/api/my_stats")
async def get_my_stats(current_user: dict = Depends(get_current_user)):
    tg_id = current_user["telegram_id"]

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
                COALESCE(s.subject, ov.subject) as subject,
                COUNT(a.id) as total_subject_lessons,
                SUM(CASE WHEN a.status = 'absent' THEN 1 ELSE 0 END) as subject_absent
            FROM attendance a
            LEFT JOIN schedule s ON s.id = a.schedule_id
            LEFT JOIN schedule_overrides ov ON ov.id = a.override_id
            WHERE a.telegram_id = $1
            GROUP BY COALESCE(s.subject, ov.subject)
        """, tg_id)
        by_subjects = [dict(r) for r in rows if r["subject"] is not None]

    return {"general": general, "subjects": by_subjects}

@app.get("/api/group/stats")
async def get_group_stats(starosta: dict = Depends(require_starosta)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
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
async def export_attendance_file(starosta: dict = Depends(require_starosta)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        query = """
            SELECT 
                COALESCE(s.date_str, ov.date_str) as date_str,
                COALESCE(s.day_of_week, ov.day_of_week) as day_of_week,
                COALESCE(s.lesson_number, ov.lesson_number) as lesson_number,
                COALESCE(s.subject, ov.subject) as subject,
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
            LEFT JOIN schedule s ON s.id = a.schedule_id
            LEFT JOIN schedule_overrides ov ON ov.id = a.override_id
            JOIN users u ON u.telegram_id = a.telegram_id
            ORDER BY date_str ASC, lesson_number ASC, u.full_name ASC
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
            "Content-Disposition": "attachment; filename=attendance_report_KI251.csv",
            "Access-Control-Expose-Headers": "Content-Disposition"
        }
    )

# ---------------------------------------------------------------------------
# Оголошення та розсилки
# ---------------------------------------------------------------------------
@app.get("/api/announcements")
async def get_announcements(current_user: dict = Depends(get_current_user)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, message, 
                   to_char(created_at AT TIME ZONE 'Europe/Kyiv', 'DD.MM.YYYY HH24:MI') as date_time,
                   created_at
            FROM announcements 
            ORDER BY id DESC LIMIT 30
        """)
        result = []
        for r in rows:
            d = dict(r)
            if d.get("created_at"):
                d["created_at_iso"] = d["created_at"].isoformat()
            result.append(d)
        return result

class BroadcastRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=200)
    message: str = Field(..., min_length=2, max_length=2000)

@app.post("/api/broadcast")
async def broadcast_announcement(data: BroadcastRequest, starosta: dict = Depends(require_starosta)):
    await broadcast_limiter.check(f"broadcast_{starosta['telegram_id']}")

    pool = await get_db_pool()
    async with pool.acquire() as conn:
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
