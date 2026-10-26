import os
import json
import hmac
import hashlib
from urllib.parse import parse_qsl
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import aiosqlite
from database import DB_NAME

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def verify_telegram_data(init_data: str) -> dict:
    """Перевіряє автентичність запиту з Telegram Mini App."""
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing init data")
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_received = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash != hash_received:
            raise HTTPException(status_code=403, detail="Invalid data signature")
            
        return json.loads(parsed_data.get("user", "{}"))
    except Exception:
        raise HTTPException(status_code=403, detail="Authentication failed")

@app.get("/api/user_data")
async def get_user_data(x_telegram_init_data: str = Header(...)):
    user = verify_telegram_data(x_telegram_init_data)
    tg_id = user.get("id")
    
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                # Першого користувача можна зробити старостою, або призначити роль вручну
                await db.execute(
                    "INSERT INTO users (telegram_id, full_name, subgroup, role) VALUES (?, ?, 1, 'student')",
                    (tg_id, name)
                )
                await db.commit()
                return {"telegram_id": tg_id, "full_name": name, "subgroup": 1, "role": "student"}
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
            
        # Сортуємо за id ASC — це збереже точний хронологічний порядок додавання парсером
        async with db.execute("""
            SELECT * FROM schedule 
            WHERE target_subgroup IN (0, ?) 
            ORDER BY id ASC
        """, (subgroup,)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

# Роздача файлів папки webapp за адресою /app/
app.mount("/app", StaticFiles(directory="webapp", html=True), name="webapp")
