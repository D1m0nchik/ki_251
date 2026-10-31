import os
import re
import asyncpg

db_pool = None

def parse_db_url(url: str):
    """
    Надійно витягує компоненти підключення навіть якщо в паролі 
    присутні спецсимволи (+, ?, &, #, %), оминаючи баги urllib.parse.
    Формат Supabase: postgresql://USER:PASSWORD@HOST:PORT/DB
    """
    pattern = r"^postgres(?:ql)?://([^:]+):(.*)@([^:/]+)(?::(\d+))?/(.+)$"
    match = re.match(pattern, url.strip())
    if not match:
        return None
    
    user, password, host, port, database = match.groups()
    
    # Відсікаємо можливі query-параметри в кінці назви БД (наприклад, ?sslmode=require)
    if "?" in database:
        database = database.split("?")[0]
        
    return {
        "user": user,
        "password": password,
        "host": host,
        "port": int(port) if port else 5432,
        "database": database
    }

async def init_db():
    global db_pool
    raw_url = os.environ.get("DATABASE_URL", "")
    conn_params = parse_db_url(raw_url)
    
    if conn_params:
        db_pool = await asyncpg.create_pool(
            user=conn_params["user"],
            password=conn_params["password"],
            host=conn_params["host"],
            port=conn_params["port"],
            database=conn_params["database"],
            min_size=1,
            max_size=10,
            ssl="require"
        )
    else:
        # Резервний варіант через dsn, якщо рядок нестандартного формату
        db_pool = await asyncpg.create_pool(
            dsn=raw_url,
            min_size=1,
            max_size=10,
            ssl="require"
        )
    return db_pool

async def get_db_pool():
    global db_pool
    if db_pool is None:
        await init_db()
    return db_pool
