"""Abstract E-Class client. Everything outside app.eclass depends only on this."""

from abc import ABC, abstractmethod
from datetime import date

from app.eclass.models import Assignment, Course, Lesson


class EClassError(Exception):
    pass


class EClassAuthError(EClassError):
    pass


class EClassUnavailableError(EClassError):
    pass


class EClassClient(ABC):
    @abstractmethod
    async def login(self, username: str, password: str) -> bool:
        """Authenticate and persist session state. Raise EClassAuthError on bad credentials."""

    @abstractmethod
    async def refresh_session(self, username: str) -> bool:
        """Re-authenticate/renew the session WITHOUT a stored password.

        There is no legitimate E-Class refresh mechanism, so implementations
        must not silently fall back to a saved password (none exists)."""

    @abstractmethod
    async def get_courses(self) -> list[Course]:
        ...

    @abstractmethod
    async def get_schedule(self, start: date, end: date) -> list[Lesson]:
        ...

    @abstractmethod
    async def get_assignments(self) -> list[Assignment]:
        ...

    @abstractmethod
    async def get_quizzes(self) -> list[Assignment]:
        ...

    @abstractmethod
    def session_data(self) -> str:
        """Serialized session state (e.g. encrypted cookies) for DB storage."""
