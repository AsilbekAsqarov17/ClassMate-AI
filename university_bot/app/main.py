"""Bot entry point. Run with: python -m app.main (from university_bot/)."""

import asyncio
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

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


# 1. Tiny HTTP server to satisfy Render's port binding requirement on free web services
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ClassMate AI Bot is running!")
    
    def log_message(self, format, *args):
        # Suppress request log spam from health checks
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    log.info(f"Health check server listening on port {port}")
    server.serve_forever()


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
        log.info("Shutting down…")
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await dp.storage.close()
        await bot.session.close()
        await dispose_engine()
        log.info("Resources released.")


if __name__ == "__main__":
    # 2. Start the health-check server in a background thread before running asyncio
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    log.info("ClassMate_AI stopped gracefully.")