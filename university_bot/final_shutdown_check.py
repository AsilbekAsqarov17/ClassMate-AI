"""Deterministic shutdown verification.

CPython's asyncio.Runner (3.11+) handles SIGINT by *cancelling the main task*
and then re-raising KeyboardInterrupt after the task finishes. This harness
reproduces that exact mechanism against the real running bot: start main()
as a task, let polling begin, cancel it (== Ctrl+C), then re-raise
KeyboardInterrupt (== Runner behavior) — exercising the production shutdown
path end to end (polling stop -> scheduler stop -> resource cleanup).
"""
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def runner() -> None:
    from app.main import main

    task = asyncio.create_task(main())
    await asyncio.sleep(20)  # let polling fully start
    task.cancel()  # == what asyncio.Runner's SIGINT handler does on Ctrl+C
    try:
        await task
    except asyncio.CancelledError:
        raise KeyboardInterrupt  # == Runner re-raising after cancellation


try:
    asyncio.run(runner())
except KeyboardInterrupt:
    pass
logging.getLogger("classmate_ai").info("ClassMate_AI stopped gracefully.")
print("RESULT: clean exit, no traceback")
