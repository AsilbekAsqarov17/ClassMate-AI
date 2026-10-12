"""Single-instance guard + startup conflict detection.

These lock in the guarantee the bot depends on: exactly one process may poll a
given bot token, and a conflict is surfaced immediately with an actionable
message instead of being retried forever by aiogram's polling loop.
"""

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetUpdates
from aiogram.types import Update, User

from app.instance_lock import AlreadyRunning, SingleInstanceGuard, lock_path, pid_path
from app.main import WatchedSession, _wait_until_polling_healthy

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BOT_ID = 8616703813


# ---------- lock acquisition ----------


def test_first_acquire_succeeds_and_records_pid(tmp_path):
    guard = SingleInstanceGuard(tmp_path / "bot.lock")
    guard.acquire()
    try:
        assert pid_path(guard.path).read_text().strip() == str(os.getpid())
    finally:
        guard.release()


def test_second_guard_is_refused(tmp_path):
    path = tmp_path / "bot.lock"
    first = SingleInstanceGuard(path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning) as exc_info:
            SingleInstanceGuard(path).acquire()
        assert exc_info.value.pid == os.getpid()
    finally:
        first.release()


def test_lock_is_reusable_after_release(tmp_path):
    """A clean exit must not leave a lock that blocks the next start."""
    path = tmp_path / "bot.lock"
    first = SingleInstanceGuard(path)
    first.acquire()
    first.release()

    second = SingleInstanceGuard(path)
    second.acquire()
    second.release()


def test_release_is_idempotent(tmp_path):
    guard = SingleInstanceGuard(tmp_path / "bot.lock")
    guard.acquire()
    guard.release()
    guard.release()  # must not raise


def test_lock_is_visible_to_another_process(tmp_path):
    """The real failure mode: a second `python -m app.main` in another shell."""
    path = tmp_path / "bot.lock"
    guard = SingleInstanceGuard(path)
    guard.acquire()
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(PROJECT_ROOT)!r})
        from app.instance_lock import SingleInstanceGuard, AlreadyRunning
        try:
            SingleInstanceGuard({str(path)!r}).acquire()
            print("ACQUIRED")
        except AlreadyRunning as exc:
            print("REFUSED", exc.pid)
        """
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
        assert result.stdout.startswith("REFUSED"), result.stdout + result.stderr
        assert result.stdout.split()[1] == str(os.getpid())
    finally:
        guard.release()


def test_lock_is_released_when_owner_is_killed(tmp_path):
    """OS locks die with the process, so a crash cannot block the next start."""
    path = tmp_path / "bot.lock"
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(PROJECT_ROOT)!r})
        from app.instance_lock import SingleInstanceGuard
        guard = SingleInstanceGuard({str(path)!r})
        guard.acquire()
        print("HELD", flush=True)
        time.sleep(60)
        """
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout.readline().strip() == "HELD"

    with pytest.raises(AlreadyRunning):
        SingleInstanceGuard(path).acquire()

    holder.kill()  # hard kill: release() never runs
    holder.wait(timeout=30)

    recovered = SingleInstanceGuard(path)
    recovered.acquire()
    recovered.release()


# ---------- conflict detection ----------


def _conflict_response() -> str:
    return '{"ok": false, "error_code": 409, "description": "terminated by other getUpdates request"}'


def _ok_response() -> str:
    return '{"ok": true, "result": []}'


def test_session_flags_a_conflict():
    session = WatchedSession()
    with pytest.raises(TelegramConflictError):
        session.check_response(
            bot=None, method=GetUpdates(), status_code=409, content=_conflict_response()
        )
    assert session.conflict.is_set()
    assert not session.first_poll_ok.is_set()


def test_session_flags_a_healthy_poll():
    session = WatchedSession()
    session.check_response(
        bot=None, method=GetUpdates(), status_code=200, content=_ok_response()
    )
    assert session.first_poll_ok.is_set()
    assert not session.conflict.is_set()


def test_session_ignores_unrelated_methods_for_poll_health():
    """Only getUpdates proves the polling slot is ours."""
    session = WatchedSession()
    from aiogram.methods import GetMe

    session.check_response(
        bot=None,
        method=GetMe(),
        status_code=200,
        content='{"ok": true, "result": {"id": 1, "is_bot": true, "first_name": "B", "username": "b"}}',
    )
    assert not session.first_poll_ok.is_set()


async def test_healthy_wait_returns_once_a_poll_succeeds():
    session = WatchedSession()
    poller = asyncio.create_task(asyncio.sleep(60))  # never finishes on its own

    async def succeed_later():
        await asyncio.sleep(0.05)
        session.first_poll_ok.set()

    asyncio.create_task(succeed_later())
    try:
        await asyncio.wait_for(_wait_until_polling_healthy(poller, session), timeout=5)
    finally:
        poller.cancel()
    assert not session.conflict.is_set()


async def test_healthy_wait_returns_on_conflict():
    session = WatchedSession()
    poller = asyncio.create_task(asyncio.sleep(60))

    async def conflict_later():
        await asyncio.sleep(0.05)
        session.conflict.set()

    asyncio.create_task(conflict_later())
    try:
        await asyncio.wait_for(_wait_until_polling_healthy(poller, session), timeout=5)
    finally:
        poller.cancel()
    assert session.conflict.is_set()


async def test_healthy_wait_propagates_a_polling_crash():
    """A non-conflict failure must surface, not be mistaken for healthy."""

    async def boom():
        raise RuntimeError("network down")

    session = WatchedSession()
    poller = asyncio.create_task(boom())
    with pytest.raises(RuntimeError, match="network down"):
        await asyncio.wait_for(_wait_until_polling_healthy(poller, session), timeout=5)


# ---------- default lock location ----------


def test_default_lock_path_is_next_to_the_project():
    assert lock_path().parent == PROJECT_ROOT
    assert lock_path().name.endswith(".lock")