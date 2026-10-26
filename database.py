import os
import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pool: asyncpg.Pool = None

async def init_db():
    global pool
    if not DATABASE_URL:
        print("ПОМИЛКА: Змінна оточення DATABASE_URL не встановлена на Render!")
        return

    # Ініціалізуємо пул підключень до PostgreSQL у Supabase
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=60
    )
    print("Успішно підключено до бази Supabase PostgreSQL!")

async def get_db_pool() -> asyncpg.Pool:
    global pool
    if pool is None:
        await init_db()
    return pool
