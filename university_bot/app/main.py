"""Bot entry point. Run with: python -m app.main (from university_bot/)."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import academic, attendance, schedule, start
from app.bot.handlers import settings as settings_handler
from app.config.settings import get_settings
from app.database.database import create_tables, dispose_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("classmate_ai")


async def main() -> None:
    config = get_settings()
    await create_tables()

    bot = Bot(token=config.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(start.router)
    dp.include_router(schedule.router)
    dp.include_router(academic.router)
    dp.include_router(attendance.router)
    dp.include_router(settings_handler.router)

    await bot.delete_webhook(drop_pending_updates=True)

    from app.scheduler.jobs import build_scheduler

    scheduler = build_scheduler(bot)
    scheduler.start()
    log.info("ClassMate_AI started (polling + scheduler).")

    try:
        await dp.start_polling(bot)
    finally:
        # Graceful shutdown in reverse startup order. aiogram's start_polling
        # returns normally when it handles SIGINT/SIGTERM itself; on Windows a
        # Ctrl+C cancels the polling task, which also lands here.
        log.info("Shutting down…")
        if scheduler.running:
            scheduler.shutdown(wait=False)  # stop jobs before anything else
        await dp.storage.close()
        await bot.session.close()           # Telegram HTTP session
        await dispose_engine()              # PostgreSQL connection pool
        log.info("Resources released.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Normal manual shutdown (Ctrl+C): cleanup already ran inside main()'s
        # finally; no traceback for this. Real exceptions propagate untouched.
        pass
    log.info("ClassMate_AI stopped gracefully.")
