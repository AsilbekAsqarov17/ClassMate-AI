"""One-time password / session-based authentication lifecycle tests.

Covers: no password column in the model, session reuse across syncs,
legitimate sync requests as keep-alive, genuine expiry detection (vs.
transient network errors), and that expired sessions never trigger any
password fallback.
"""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.services.sync_service as sync_mod
from app.database.models.base import Base
from app.database.models.eclass_account import EClassAccount
from app.database.models.group import Group
from app.database.models.user import User
from app.eclass.client import EClassAuthError, EClassUnavailableError
from app.eclass.models import Assignment as EAssignment
from app.eclass.models import Course as ECourse
from app.services.eclass_session import SessionExpiredError, client_for_account
from app.services.sync_service import sync_user


def test_model_has_no_password_field():
    cols = EClassAccount.__table__.columns
    assert "encrypted_password" not in cols
    assert all("password" not in c.name.lower() for c in cols)
    # session storage is what remains
    assert "session_data" in cols


async def _factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class FakeWebClient:
    """Stands in for EClassWebClient in client_for_account."""

    behavior = "ok"  # ok | expired | network
    instances: list["FakeWebClient"] = []

    def __init__(self):
        self.loaded_sessions: list[str] = []
        self.probes = 0
        FakeWebClient.instances.append(self)

    async def _get_client(self):
        return object()

    def load_session_data(self, encrypted: str) -> None:
        self.loaded_sessions.append(encrypted)

    async def get_courses(self):
        self.probes += 1
        if FakeWebClient.behavior == "expired":
            raise EClassAuthError("Session expired")
        if FakeWebClient.behavior == "network":
            raise httpx.ConnectError("connection refused")
        return []


@pytest.mark.asyncio
async def test_valid_session_is_returned_and_reusable(monkeypatch):
    monkeypatch.setattr("app.services.eclass_session.EClassWebClient", FakeWebClient)
    FakeWebClient.behavior = "ok"
    engine, factory = await _factory()
    async with factory() as s:
        acc = EClassAccount(user_id=1, username="u2410037", session_data="enc-cookies")
        # multiple consecutive uses (e.g. several scheduled syncs)
        c1 = await client_for_account(s, acc)
        c2 = await client_for_account(s, acc)
        assert c1.loaded_sessions == ["enc-cookies"]
        assert c2.loaded_sessions == ["enc-cookies"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_session_raises_without_password_fallback(monkeypatch):
    monkeypatch.setattr("app.services.eclass_session.EClassWebClient", FakeWebClient)
    FakeWebClient.behavior = "expired"
    engine, factory = await _factory()
    async with factory() as s:
        acc = EClassAccount(user_id=1, username="u2410037", session_data="enc-cookies")
        with pytest.raises(SessionExpiredError):
            await client_for_account(s, acc)
        # there is no password anywhere to fall back to
        assert not hasattr(acc, "encrypted_password")
    await engine.dispose()


@pytest.mark.asyncio
async def test_missing_session_raises_expired():
    engine, factory = await _factory()
    async with factory() as s:
        acc = EClassAccount(user_id=1, username="u2410037", session_data=None)
        with pytest.raises(SessionExpiredError):
            await client_for_account(s, acc)
    await engine.dispose()


@pytest.mark.asyncio
async def test_transient_network_error_is_not_expiry(monkeypatch):
    monkeypatch.setattr("app.services.eclass_session.EClassWebClient", FakeWebClient)
    FakeWebClient.behavior = "network"
    engine, factory = await _factory()
    async with factory() as s:
        acc = EClassAccount(user_id=1, username="u2410037", session_data="enc-cookies")
        with pytest.raises(EClassUnavailableError):
            await client_for_account(s, acc)
    await engine.dispose()


class SpySyncClient:
    """Records the legitimate requests sync_user makes with the session."""

    def __init__(self):
        self.calls: list[str] = []

    async def get_courses(self):
        self.calls.append("get_courses")
        return [ECourse(external_id="1", name="OS", professor="Kim")]

    async def get_assignments(self):
        self.calls.append("get_assignments")
        return []

    async def get_quizzes(self):
        self.calls.append("get_quizzes")
        return [
            EAssignment(external_id="q1", course_external_id="1", course_name="OS",
                        title="Quiz 1", kind="quiz")
        ]

    async def aclose(self):
        self.calls.append("aclose")


class FakeEduPage:
    async def get_timetable(self, group_name, start, end):
        return []


@pytest.mark.asyncio
async def _make_account(factory):
    async with factory() as s:
        g = Group(name="ICE-24-1")
        s.add(g)
        await s.flush()
        u = User(telegram_id=999, username="sess", timezone="Asia/Tashkent", group_id=g.id)
        s.add(u)
        await s.flush()
        acc = EClassAccount(user_id=u.id, username="u2410037", session_data="enc-cookies")
        s.add(acc)
        await s.commit()
        return acc.id


@pytest.mark.asyncio
async def test_sync_uses_legitimate_requests_on_stored_session(monkeypatch):
    engine, factory = await _factory()
    acc_id = await _make_account(factory)
    spy = SpySyncClient()

    async def fake_client_for_account(session, account):
        return spy

    monkeypatch.setattr(sync_mod, "client_for_account", fake_client_for_account)
    monkeypatch.setattr(sync_mod, "EduPageWebClient", lambda: FakeEduPage())

    async with factory() as s:
        acc = (await s.execute(select(EClassAccount).where(EClassAccount.id == acc_id))).scalar_one()
        outcome = await sync_user(s, acc)
        assert outcome.courses_ok and outcome.quizzes_ok and outcome.timetable_ok
        assert outcome.session_expired is False
        # two consecutive syncs reuse the same stored session — no re-login
        outcome2 = await sync_user(s, acc)
        assert outcome2.courses_ok
        assert acc.is_active is True
        assert acc.last_sync is not None

    assert spy.calls == ["get_courses", "get_assignments", "get_quizzes", "aclose"] * 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_marks_account_expired_and_stops(monkeypatch):
    engine, factory = await _factory()
    acc_id = await _make_account(factory)

    async def expired_client(session, account):
        raise SessionExpiredError("E-Class session expired — please reconnect")

    monkeypatch.setattr(sync_mod, "client_for_account", expired_client)

    async with factory() as s:
        acc = (await s.execute(select(EClassAccount).where(EClassAccount.id == acc_id))).scalar_one()
        outcome = await sync_user(s, acc)
        assert outcome.session_expired is True
        assert outcome.courses_ok is False
        assert acc.is_active is False  # scheduler only iterates active accounts
        assert not hasattr(acc, "encrypted_password")  # no silent re-login possible
    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_transient_error_keeps_account_active(monkeypatch):
    engine, factory = await _factory()
    acc_id = await _make_account(factory)

    async def flaky_client(session, account):
        raise EClassUnavailableError("E-Class unreachable: timeout")

    monkeypatch.setattr(sync_mod, "client_for_account", flaky_client)

    async with factory() as s:
        acc = (await s.execute(select(EClassAccount).where(EClassAccount.id == acc_id))).scalar_one()
        outcome = await sync_user(s, acc)
        # transient failure: not treated as expiry, user NOT asked to reconnect
        assert outcome.session_expired is False
        assert acc.is_active is True
    await engine.dispose()
