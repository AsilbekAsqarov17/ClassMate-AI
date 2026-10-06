"""Tests for Student ID validation and verified E-Class authentication flow.

Covers:
- normalize_student_id format rules
- EClassWebClient.login() real-verification semantics (no guest-cookie false positives)
- /start handler DB behavior: nothing stored on failed login, encrypted on success
- /settings password change: old credentials preserved on failure
"""

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.settings as settings_mod
import app.bot.handlers.start as start_mod
from app.bot.states.login import LoginStates
from app.database.models.base import Base
from app.database.models.eclass_account import EClassAccount
from app.database.models.user import User
from app.eclass.client import EClassAuthError
from app.eclass.web_client import EClassWebClient
from app.services.validation import normalize_student_id

# ---------------- Student ID format ----------------


@pytest.mark.parametrize("value", ["u2410037", "U2410037", "u2123456", "U2123456", "u2000000"])
def test_student_id_valid(value):
    assert normalize_student_id(value) == value.lower()


@pytest.mark.parametrize(
    "value",
    ["u241037", "u24100378", "u3410037", "x2410037", "u2abcdef", "u2-410037", "2410037", "", "  ", None],
)
def test_student_id_invalid(value):
    assert normalize_student_id(value) is None


def test_student_id_normalized_to_lowercase():
    assert normalize_student_id("U2410037") == "u2410037"
    assert normalize_student_id("  U2410037  ") == "u2410037"


# ---------------- login() verification semantics ----------------


def _client(handler) -> EClassWebClient:
    client = EClassWebClient(base_url="https://eclass.example")
    client._client = httpx.AsyncClient(
        base_url="https://eclass.example",
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_login_success_verified_by_authenticated_page():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/index.php" and request.method == "POST":
            # real Moodle redirects away from the login page on success
            return httpx.Response(303, headers={
                "Location": "/my/",
                "set-cookie": "MoodleSession=real; path=/",
            })
        if request.url.path == "/my/":
            return httpx.Response(200, text="<html>my courses</html>")
        return httpx.Response(200, text="ok")

    client = _client(handler)
    assert await client.login("u2410037", "whatever") is True


@pytest.mark.asyncio
async def test_login_rejected_on_invalid_login_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Invalid login, please try again</html>",
                              headers={"set-cookie": "MoodleSession=guest; path=/"})

    client = _client(handler)
    with pytest.raises(EClassAuthError):
        await client.login("u2410037", "wrong")


@pytest.mark.asyncio
async def test_login_rejected_when_redirected_back_to_login_page():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/index.php" and request.method == "POST":
            return httpx.Response(303, headers={"Location": "/login/index.php"})
        return httpx.Response(200, text="<html>login form</html>",
                              headers={"set-cookie": "MoodleSession=guest; path=/"})

    client = _client(handler)
    with pytest.raises(EClassAuthError):
        await client.login("u2410037", "wrong")


@pytest.mark.asyncio
async def test_login_rejected_when_session_cookie_is_guest_only():
    """Moodle hands out MoodleSession cookies to guests: a cookie plus a 200
    response that is NOT the login page must still fail if an authenticated
    page (/my/) bounces back to login."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/index.php" and request.method == "POST":
            return httpx.Response(200, text="<html>welcome</html>",
                                  headers={"set-cookie": "MoodleSession=guest; path=/"})
        if request.url.path == "/my/":
            return httpx.Response(303, headers={"Location": "/login/index.php"})
        return httpx.Response(200, text="<html>login form</html>")

    client = _client(handler)
    with pytest.raises(EClassAuthError):
        await client.login("u2410037", "wrong")


@pytest.mark.asyncio
async def test_login_empty_password_fails_via_real_attempt():
    post_seen = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_seen
        if request.method == "POST" and request.url.path == "/login/index.php":
            post_seen = True  # a real login attempt must happen
        return httpx.Response(200, text="<html>Invalid login, please try again</html>",
                              headers={"set-cookie": "MoodleSession=guest; path=/"})

    client = _client(handler)
    with pytest.raises(EClassAuthError):
        await client.login("u2410037", "")
    assert post_seen


# ---------------- handler-level DB behavior ----------------


class FakeState:
    def __init__(self, data=None):
        self.data = data or {}
        self.state = None

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return self.data

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.data = {}
        self.state = None


class FakeMessage:
    def __init__(self, text, tg_id=555, username="flowtest"):
        self.text = text
        self.from_user = SimpleNamespace(id=tg_id, username=username, first_name="Flow")
        self.deleted = False
        self.answers = []

    async def delete(self):
        self.deleted = True

    async def answer(self, text, **kw):
        msg = SimpleNamespace(text=text, edits=[])

        async def edit_text(t, **k):
            msg.edits.append(t)

        msg.edit_text = edit_text
        self.answers.append(msg)
        return msg


class FakeEClassClient:
    """Stands in for EClassWebClient: 'login' succeeds only for password == 'correct'."""

    def __init__(self):
        self.logins: list[str] = []

    async def login(self, username: str, password: str) -> bool:
        self.logins.append(username)
        if password == "correct":
            return True
        raise EClassAuthError("Invalid username or password")

    def session_data(self) -> str:
        return "encrypted-session-blob"


async def _factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_invalid_student_id_rejected_before_login(monkeypatch):
    engine, factory = await _factory()
    monkeypatch.setattr(start_mod, "get_session_factory", lambda: factory)
    client = FakeEClassClient()
    monkeypatch.setattr(start_mod, "EClassWebClient", lambda: client)

    state = FakeState()
    await state.set_state(LoginStates.waiting_student_id)
    msg = FakeMessage("u241037")  # 7 chars — invalid

    await start_mod.got_student_id(msg, state)

    assert state.state == LoginStates.waiting_student_id  # did NOT advance
    assert "student_id" not in state.data
    assert "Invalid Student ID" in msg.answers[-1].text
    async with factory() as s:
        assert (await s.execute(select(EClassAccount))).scalars().all() == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_valid_student_id_normalized_and_advanced(monkeypatch):
    engine, factory = await _factory()
    monkeypatch.setattr(start_mod, "get_session_factory", lambda: factory)

    state = FakeState()
    msg = FakeMessage("U2410037")
    await start_mod.got_student_id(msg, state)

    assert state.state == LoginStates.waiting_password
    assert state.data["student_id"] == "u2410037"
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_login_stores_nothing(monkeypatch):
    engine, factory = await _factory()
    monkeypatch.setattr(start_mod, "get_session_factory", lambda: factory)
    client = FakeEClassClient()
    monkeypatch.setattr(start_mod, "EClassWebClient", lambda: client)

    state = FakeState({"student_id": "u2410037"})
    msg = FakeMessage("definitely-wrong")
    await start_mod.got_password(msg, state)

    assert msg.deleted is True  # password message removed
    assert "authentication failed" in msg.answers[-1].edits[-1]
    async with factory() as s:
        assert (await s.execute(select(EClassAccount))).scalars().all() == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_successful_login_stores_session_only_never_password(monkeypatch):
    engine, factory = await _factory()
    monkeypatch.setattr(start_mod, "get_session_factory", lambda: factory)
    client = FakeEClassClient()
    monkeypatch.setattr(start_mod, "EClassWebClient", lambda: client)

    state = FakeState({"student_id": "u2410037"})
    msg = FakeMessage("correct")
    await start_mod.got_password(msg, state)

    assert state.state == LoginStates.waiting_group
    assert "Account created successfully" in msg.answers[-1].edits[-1]
    async with factory() as s:
        user = (await s.execute(select(User).where(User.telegram_id == 555))).scalar_one()
        acc = (await s.execute(select(EClassAccount).where(EClassAccount.user_id == user.id))).scalar_one()
        assert acc.username == "u2410037"
        assert acc.session_data == "encrypted-session-blob"
        assert acc.is_active is True
        # ONE-TIME PASSWORD: no password material anywhere on the model
        assert not hasattr(acc, "encrypted_password")
        assert all("password" not in c.name for c in acc.__table__.columns)
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_reconnect_preserves_old_session(monkeypatch):
    engine, factory = await _factory()
    monkeypatch.setattr(settings_mod, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_mod, "EClassWebClient", lambda: FakeEClassClient())

    async with factory() as s:
        u = User(telegram_id=555, username="flowtest", timezone="Asia/Tashkent")
        s.add(u)
        await s.flush()
        s.add(EClassAccount(user_id=u.id, username="u2410037", session_data="old-session"))
        await s.commit()

    msg = FakeMessage("wrong-new-password")
    await settings_mod.got_reconnect_password(msg, FakeState(), None)

    assert msg.deleted is True
    assert "session was kept" in msg.answers[-1].edits[-1]
    async with factory() as s:
        acc = (await s.execute(select(EClassAccount))).scalar_one()
        assert acc.session_data == "old-session"  # untouched
    await engine.dispose()


@pytest.mark.asyncio
async def test_successful_reconnect_replaces_session(monkeypatch):
    engine, factory = await _factory()
    monkeypatch.setattr(settings_mod, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_mod, "EClassWebClient", lambda: FakeEClassClient())

    async with factory() as s:
        u = User(telegram_id=555, username="flowtest", timezone="Asia/Tashkent")
        s.add(u)
        await s.flush()
        s.add(EClassAccount(user_id=u.id, username="u2410037", session_data="old-session", is_active=False))
        await s.commit()

    msg = FakeMessage("correct")
    await settings_mod.got_reconnect_password(msg, FakeState(), None)

    assert "reconnected successfully" in msg.answers[-1].edits[-1]
    async with factory() as s:
        acc = (await s.execute(select(EClassAccount))).scalar_one()
        assert acc.session_data == "encrypted-session-blob"
        assert acc.is_active is True
        assert not hasattr(acc, "encrypted_password")
    await engine.dispose()


@pytest.mark.asyncio
async def test_settings_invalid_student_id_rejected_before_login(monkeypatch):
    engine, factory = await _factory()
    monkeypatch.setattr(settings_mod, "get_session_factory", lambda: factory)

    def _boom():
        raise AssertionError("E-Class login must not be attempted for invalid ID")

    monkeypatch.setattr(settings_mod, "EClassWebClient", _boom)

    async with factory() as s:
        u = User(telegram_id=555, username="flowtest", timezone="Asia/Tashkent")
        s.add(u)
        await s.flush()
        s.add(EClassAccount(user_id=u.id, username="u2410037", session_data="sess"))
        await s.commit()

    state = FakeState()
    msg = FakeMessage("x3410037")
    await settings_mod.got_new_student_id(msg, state)

    assert "Invalid Student ID" in msg.answers[-1].text
    async with factory() as s:
        acc = (await s.execute(select(EClassAccount))).scalar_one()
        assert acc.username == "u2410037"  # unchanged
    await engine.dispose()


@pytest.mark.asyncio
async def test_change_student_id_requires_one_time_password(monkeypatch):
    """New Student ID is only persisted after a real login for that ID."""
    engine, factory = await _factory()
    monkeypatch.setattr(settings_mod, "get_session_factory", lambda: factory)
    client = FakeEClassClient()
    monkeypatch.setattr(settings_mod, "EClassWebClient", lambda: client)

    async with factory() as s:
        u = User(telegram_id=555, username="flowtest", timezone="Asia/Tashkent")
        s.add(u)
        await s.flush()
        s.add(EClassAccount(user_id=u.id, username="u2410037", session_data="old-session"))
        await s.commit()

    # step 1: enter the new ID -> bot asks for a one-time password
    state = FakeState()
    await settings_mod.got_new_student_id(FakeMessage("U2499999"), state)
    assert state.data["new_student_id"] == "u2499999"

    # step 2: wrong password -> nothing changes
    msg = FakeMessage("nope")
    await settings_mod.got_id_password(msg, state)
    async with factory() as s:
        acc = (await s.execute(select(EClassAccount))).scalar_one()
        assert acc.username == "u2410037"
        assert acc.session_data == "old-session"

    # step 3: correct password -> ID + session replaced, password never stored
    state2 = FakeState({"new_student_id": "u2499999"})
    msg2 = FakeMessage("correct")
    await settings_mod.got_id_password(msg2, state2)
    async with factory() as s:
        acc = (await s.execute(select(EClassAccount))).scalar_one()
        assert acc.username == "u2499999"
        assert acc.session_data == "encrypted-session-blob"
        assert not hasattr(acc, "encrypted_password")
    await engine.dispose()
