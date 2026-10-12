"""Bot entry point. Run with: python -m app.main (from university_bot/)."""

import asyncio
import logging
from contextlib import suppress
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramConflictError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetUpdates

from app.bot.handlers import academic, attendance, schedule, start
from app.bot.handlers import settings as settings_handler
from app.config.settings import get_settings
from app.database.database import create_tables, dispose_engine
from app.instance_lock import AlreadyRunning, SingleInstanceGuard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("classmate_ai")

CONFLICT_HELP = (
    "Cannot start: another instance is already calling getUpdates with this\n"
    "bot token. Telegram allows a single polling consumer per token, so two\n"
    "instances can only steal updates from each other.\n\n"
    "Stop the other instance first - another terminal or editor run, a stray\n"
    "background process, or a deployment using the same BOT_TOKEN - then start\n"
    "this one again.\n"
    "The single-instance lock already blocks a second local copy; this check\n"
    "catches an instance running somewhere else."
)


class WatchedSession(AiohttpSession):
    """A session that surfaces polling health instead of letting it disappear.

    ``Dispatcher._listen_updates`` catches *every* exception and retries it
    forever with a short backoff. That is correct for a network blip, but a
    competing bot instance is a permanent condition: the loop turns it into an
    endless wall of identical "terminated by other getUpdates request" lines
    while the bot silently never works. ``check_response`` is the last place
    that condition is still observable, so record it here.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.first_poll_ok = asyncio.Event()
        self.conflict = asyncio.Event()

    def check_response(self, bot, method, status_code, content):
        try:
            response = super().check_response(
                bot=bot, method=method, status_code=status_code, content=content
            )
        except TelegramConflictError:
            self.conflict.set()
            raise
        if isinstance(method, GetUpdates):
            self.first_poll_ok.set()
        return response


async def _await_any(*events: asyncio.Event) -> None:
    """Complete as soon as any of the events is set."""
    waiters = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def _wait_until_polling_healthy(poller: asyncio.Task, session: WatchedSession) -> None:
    """Return once polling is confirmed working, a conflict is seen, or it dies."""
    health = asyncio.create_task(_await_any(session.first_poll_ok, session.conflict))
    try:
        await asyncio.wait({poller, health}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        health.cancel()
        with suppress(asyncio.CancelledError):
            await health
    if poller.done():
        await poller  # died for a non-conflict reason: let it propagate


async def main() -> None:
    config = get_settings()
    await create_tables()

    session = WatchedSession()
    bot = Bot(token=config.bot_token, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(start.router)
    dp.include_router(schedule.router)
    dp.include_router(academic.router)
    dp.include_router(attendance.router)
    dp.include_router(settings_handler.router)

    from app.scheduler.jobs import build_scheduler

    scheduler = build_scheduler(bot)

    try:
        await bot.delete_webhook(drop_pending_updates=True)

        # close_bot_session=False: the finally block below owns session cleanup.
        poller = asyncio.create_task(dp.start_polling(bot, close_bot_session=False))
        try:
            # Wait for a confirmed-good poll before starting the scheduler, so a
            # losing instance can never send reminders that the winning one is
            # already sending.
            await _wait_until_polling_healthy(poller, session)
            if session.conflict.is_set():
                log.error(CONFLICT_HELP)
                raise SystemExit(1)

            scheduler.start()
            log.info("ClassMate_AI started (polling + scheduler).")

            await poller
        finally:
            # Dispatcher.stop_polling() would hang here: _polling consumes an
            # endless update generator and never re-checks the stop signal, so
            # cancelling the task is what actually breaks the loop.
            if not poller.done():
                poller.cancel()
                with suppress(asyncio.CancelledError):
                    await poller
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


def run() -> None:
    """Console entry point; refuses to start when another instance already runs."""
    try:
        with SingleInstanceGuard():
            asyncio.run(main())
    except AlreadyRunning as exc:
        log.error("%s", exc)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        # Normal manual shutdown (Ctrl+C): cleanup already ran inside main()'s
        # finally; no traceback for this. Real exceptions propagate untouched.
        pass
    log.info("ClassMate_AI stopped gracefully.")


if __name__ == "__main__":
    run()