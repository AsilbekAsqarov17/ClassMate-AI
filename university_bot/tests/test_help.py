"""Tests for the /help command."""

from types import SimpleNamespace

import pytest

import app.bot.handlers.start as start_mod


class FakeMessage:
    def __init__(self, text="/help", tg_id=555):
        self.text = text
        self.from_user = SimpleNamespace(id=tg_id, username="helptest", first_name="Help")
        self.answers = []

    async def answer(self, text, **kw):
        msg = SimpleNamespace(text=text)
        self.answers.append(msg)
        return msg


@pytest.mark.asyncio
async def test_help_returns_successfully():
    msg = FakeMessage()
    await start_mod.cmd_help(msg)
    assert len(msg.answers) == 1
    assert msg.answers[0].text.strip()


@pytest.mark.asyncio
async def test_help_contains_supported_commands():
    msg = FakeMessage()
    await start_mod.cmd_help(msg)
    text = msg.answers[0].text
    for cmd in [
        "/start", "/today", "/tomorrow", "/week", "/next",
        "/assignments", "/quizzes", "/deadlines", "/scores",
        "/sync", "/settings", "/help",
    ]:
        assert cmd in text


@pytest.mark.asyncio
async def test_help_contains_password_and_session_security():
    msg = FakeMessage()
    await start_mod.cmd_help(msg)
    text = msg.answers[0].text
    assert "Password security" in text
    assert "not stored" in text
    assert "deleted" in text
    assert "encrypted" in text
    assert "Session" in text
    assert "30 minutes" in text
    assert "reconnect" in text


@pytest.mark.asyncio
async def test_help_does_not_require_authentication():
    """/help must answer even for a user with no account/session."""
    msg = FakeMessage(tg_id=999999999)  # unknown Telegram user
    await start_mod.cmd_help(msg)
    assert "help" in msg.answers[0].text.lower() or "ClassMate" in msg.answers[0].text
