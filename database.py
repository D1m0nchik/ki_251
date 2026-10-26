import aiosqlite

DB_NAME = "schedule.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблиця студентів
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                subgroup INTEGER DEFAULT 1,
                role TEXT DEFAULT 'student'
            )
        """)
        
        # Таблиця розкладу
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_str TEXT NOT NULL,
                day_of_week TEXT NOT NULL,
                lesson_number INTEGER NOT NULL,
                time_start TEXT NOT NULL,
                time_end TEXT NOT NULL,
                subject TEXT NOT NULL,
                lesson_type TEXT,
                teacher TEXT,
                room TEXT,
                target_subgroup INTEGER DEFAULT 0,
                is_replacement BOOLEAN DEFAULT 0,
                replacement_note TEXT
            )
        """)

        # Таблиця відвідуваності
        await db.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                status TEXT DEFAULT 'present',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(schedule_id, telegram_id)
            )
        """)

        # Таблиця для збереження історії оголошень
        await db.execute("""
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()
