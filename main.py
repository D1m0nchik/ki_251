import asyncio
import os
import uvicorn
from server import app
from bot import bot, dp
from database import init_db

async def run_bot():
    await init_db()
    print("Telegram Bot запущено...")
    await dp.start_polling(bot)

async def main():
    asyncio.create_task(run_bot())

    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
