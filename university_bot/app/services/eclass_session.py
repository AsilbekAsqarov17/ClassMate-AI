"""Build an authenticated EClassWebClient for a user from the stored session.

One-time password design: the password is NEVER stored. The stored encrypted
session cookies are the only ongoing authentication mechanism. When they stop
working, the student must reconnect (enter the password once again).
"""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.eclass_account import EClassAccount
from app.eclass.client import EClassAuthError, EClassError, EClassUnavailableError
from app.eclass.web_client import EClassWebClient


class SessionExpiredError(EClassError):
    """The stored session is genuinely dead — the student must reconnect."""


async def client_for_account(session: AsyncSession, account: EClassAccount) -> EClassWebClient:
    """Reattach the stored authenticated session and verify it still works.

    Raises SessionExpiredError when E-Class rejects the session (redirect to
    login), EClassUnavailableError on transient network problems (account
    stays active; no password fallback exists by design).
    """
    if not account.session_data:
        raise SessionExpiredError("E-Class session missing — please reconnect")
    client = EClassWebClient()
    try:
        await client._get_client()  # ensure the httpx client exists before loading cookies
        client.load_session_data(account.session_data)
        await client.get_courses()  # probe a real authenticated page
        return client
    except EClassAuthError as exc:
        raise SessionExpiredError("E-Class session expired — please reconnect") from exc
    except (EClassError, httpx.HTTPError) as exc:
        raise EClassUnavailableError(f"E-Class unreachable: {exc}") from exc
