"""Regression tests for E-Class session handling in EClassWebClient."""

import httpx
import pytest

from app.eclass.client import EClassAuthError
from app.eclass.web_client import EClassWebClient


def _client_with_transport(handler) -> EClassWebClient:
    client = EClassWebClient(base_url="https://eclass.example")
    client._client = httpx.AsyncClient(
        base_url="https://eclass.example",
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_get_text_detects_login_php_redirect():
    """Expired Moodle sessions redirect to /login.php?errorcode=4 (not /login/index.php)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/local/ubion/user/":
            return httpx.Response(303, headers={"Location": "/login.php?errorcode=4"})
        return httpx.Response(200, text="<html>login form</html>")

    client = _client_with_transport(handler)
    with pytest.raises(EClassAuthError):
        await client._get_text("/local/ubion/user/")


@pytest.mark.asyncio
async def test_get_text_detects_login_index_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mod/quiz/index.php":
            return httpx.Response(303, headers={"Location": "/login/index.php"})
        return httpx.Response(200, text="<html>login form</html>")

    client = _client_with_transport(handler)
    with pytest.raises(EClassAuthError):
        await client._get_text("/mod/quiz/index.php")


@pytest.mark.asyncio
async def test_get_text_ok_when_authenticated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="course list html")

    client = _client_with_transport(handler)
    assert await client._get_text("/local/ubion/user/") == "course list html"


@pytest.mark.asyncio
async def test_login_clears_stale_cookies():
    """Re-login must drop stale session cookies so old/new cookies aren't both sent."""
    def handler(request: httpx.Request) -> httpx.Response:
        # successful login sets a fresh session cookie and redirects off the login page
        if request.url.path == "/login/index.php" and request.method == "POST":
            return httpx.Response(303, headers={
                "Location": "/my/",
                "set-cookie": "MoodleSession=fresh-value; path=/",
            })
        return httpx.Response(200, text="ok")

    client = _client_with_transport(handler)
    client._client.cookies.set("MoodleSession", "stale-value")
    await client.login("user", "pass")
    jar = [c for c in client._client.cookies.jar if c.name == "MoodleSession"]
    assert len(jar) == 1 and jar[0].value == "fresh-value"


@pytest.mark.asyncio
async def test_session_data_no_cookie_conflict():
    """Duplicate cookie names across domains must not crash session_data()."""
    client = _client_with_transport(lambda r: httpx.Response(200, text="ok"))
    client._client.cookies.set("MoodleSession", "a", domain="eclass.example")
    client._client.cookies.set("MoodleSession", "b", domain=".eclass.example")
    data = client.session_data()
    assert isinstance(data, str) and data
