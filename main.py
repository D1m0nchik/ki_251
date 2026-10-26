import os
import asyncio
import uvicorn
from database import init_db
from parser import fetch_and_save_schedule  # Імпортуємо правильну назву функції
from bot import bot, dp
from server import app

async def start_bot():
    print("Telegram-бот починає опитування...")
    await dp.start_polling(bot)

async def main():
    # 1. Створюємо таблиці бази даних
    await init_db()
    
    # 2. Одноразово завантажуємо розклад під час запуску (без дат завантажує стандартний тиждень)
    try:
        await fetch_and_save_schedule()
        print("Початковий розклад успішно завантажено.")
    except Exception as e:
        print(f"Помилка початкового парсингу: {e}")

    # 3. Запускаємо бота у фоні
    asyncio.create_task(start_bot())

    # 4. Запускаємо сервер FastAPI
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
