import os
import sys
import asyncio
import uvicorn
from database import init_db
from parser import fetch_and_save_schedule
from bot import bot, dp
from server import app

async def start_bot():
    try:
        print("[BOT] Telegram-бот починає опитування...", flush=True)
        # handle_signals=False критично необхідний, щоб не конфліктувати з Uvicorn
        await dp.start_polling(bot, handle_signals=False)
    except Exception as e:
        print(f"[BOT] Помилка роботи бота: {e}", flush=True)

async def initial_sync():
    # Даємо Uvicorn 2 секунди спокійно підняти порт
    await asyncio.sleep(2)
    try:
        print("[STARTUP] Спроба первинної синхронізації розкладу...", flush=True)
        success = await fetch_and_save_schedule()
        if success:
            print("[STARTUP] Початковий розклад успішно завантажено.", flush=True)
        else:
            print("[STARTUP] Розклад не оновлено під час старту (буде доступний за запитом).", flush=True)
    except Exception as e:
        print(f"[STARTUP] Помилка початкового парсингу: {e}", flush=True)

async def main():
    print("[MAIN] Запуск додатку...", flush=True)
    
    # 1. Ініціалізація пулу БД з перехопленням помилок
    try:
        await init_db()
    except Exception as e:
        print(f"[DATABASE] Помилка підключення до Supabase: {e}", flush=True)

    # 2. Запускаємо бота та первинний парсинг у ФОНІ, не блокуючи запуск порту
    asyncio.create_task(start_bot())
    asyncio.create_task(initial_sync())

    # 3. Негайно відкриваємо порт для Render
    port = int(os.environ.get("PORT", 8000))
    print(f"[UVICORN] Запуск сервера на порту {port}...", flush=True)
    
    config = uvicorn.Config(
        app, 
        host="0.0.0.0", 
        port=port, 
        log_level="info",
        loop="asyncio"
    )
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
