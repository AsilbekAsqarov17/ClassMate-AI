"""Single-instance guard for the bot process.

Telegram permits exactly one ``getUpdates`` consumer per bot token. A second
process does not queue politely: it terminates the previous long poll, and the
two instances then fight over the update stream forever
(``TelegramConflictError``). Neither ever recovers on its own, which looks
like "the bot does not start".

This module holds an OS-level advisory lock so a second copy on the same
machine fails immediately and names the PID that is already polling. OS locks
(unlike a hand-rolled PID file) are dropped by the kernel when the process
dies for *any* reason, so a crash or a ``kill -9`` can never leave a stale
lock that blocks the next start.

The lock file holds no data: on Windows an exclusive byte-range lock also
denies *reads* of the locked range, which would make the owner's PID
unreadable. The PID therefore lives in a sibling ``*.pid`` file that is only
ever written by the lock holder and is never locked.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from types import TracebackType

__all__ = ["AlreadyRunning", "SingleInstanceGuard", "lock_path"]


def lock_path() -> Path:
    """Lock file location: ``CLASSMATE_LOCK_FILE`` or ``<project>/.classmate_ai.lock``."""
    override = os.environ.get("CLASSMATE_LOCK_FILE")
    if override:
        return Path(override).expanduser()
    # app/instance_lock.py -> app/ -> project root
    return Path(__file__).resolve().parent.parent / ".classmate_ai.lock"


def pid_path(lock: Path) -> Path:
    """Companion file holding the PID of the current lock holder."""
    return lock.with_name(lock.name + ".pid")


class AlreadyRunning(RuntimeError):
    """Raised when another live process already holds the instance lock."""

    def __init__(self, path: Path, pid: int | None) -> None:
        self.path = path
        self.pid = pid
        holder = f" (PID {pid})" if pid else ""
        super().__init__(
            f"Another ClassMate_AI instance is already running{holder}.\n"
            f"Lock file: {path}\n\n"
            "Only one instance may poll a given bot token. Stop the running\n"
            "instance first. If no process is actually alive, just delete the\n"
            "lock file - it is recreated automatically on the next start."
        )


def _holder_pid(lock: Path) -> int | None:
    """PID of the current lock holder, or None if unreadable."""
    try:
        text = pid_path(lock).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


class SingleInstanceGuard:
    """Exclusive, crash-safe lock guaranteeing a single polling process."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else lock_path()
        self._fd: int | None = None

    # ---------- platform primitives ----------

    @staticmethod
    def _try_lock(fd: int) -> None:
        """Take a non-blocking exclusive lock; raise ``OSError`` if already held."""
        if os.name == "nt":
            import msvcrt

            # msvcrt locks a byte range starting at the file position and
            # refuses to lock past EOF, so make sure byte 0 exists first.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b" ")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)

    # ---------- lifecycle ----------

    def acquire(self) -> None:
        """Take the lock or raise :class:`AlreadyRunning`."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self._try_lock(fd)
        except OSError:
            pid = _holder_pid(self.path)
            os.close(fd)
            raise AlreadyRunning(self.path, pid) from None

        # We own the lock - record our PID for whoever tries next. Writing to
        # the companion file can never race, because only the holder reaches
        # this point and only the holder ever writes it.
        with suppress(OSError):
            pid_path(self.path).write_text(f"{os.getpid()}\n", encoding="utf-8")
        self._fd = fd

    def release(self) -> None:
        """Release the lock. Idempotent; safe to call from ``finally``.

        The PID file is deliberately left in place: deleting it after unlocking
        could wipe the PID of a new holder that acquired the lock in between.
        It is only ever read while the lock is genuinely held, and the holder
        always overwrites it, so a leftover value is never shown in error
        messages.
        """
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            self._unlock(fd)
        except OSError:
            pass  # closing the fd drops the lock regardless
        os.close(fd)

    def __enter__(self) -> SingleInstanceGuard:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()