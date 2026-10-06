"""Moodle-based E-Class web client (session-cookie auth + HTML parsing)."""

import json
import re
from datetime import date, datetime
from html import unescape
from zoneinfo import ZoneInfo

import httpx

from app.config.settings import get_settings
from app.eclass.client import EClassAuthError, EClassClient, EClassUnavailableError
from app.eclass.models import Assignment, Course, Lesson
from app.services.crypto import decrypt, encrypt

SESSION_COOKIE_PREFIX = "MoodleSession"


def _parse_mdy(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        local = datetime.strptime(value, "%Y-%m-%d %H:%M")
        tz = ZoneInfo(get_settings().default_timezone)
        # E-Class displays deadlines in the school's local timezone; convert the
        # displayed wall-time to an aware absolute instant (UTC) for storage.
        return local.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))
    except ValueError:
        return None


class EClassWebClient(EClassClient):
    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or get_settings().eclass_base_url).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                follow_redirects=True,
                timeout=httpx.Timeout(20.0),
                headers={"User-Agent": "UniAssistantBot/1.0"},
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client (connection pool, sockets)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- auth ----------

    async def login(self, username: str, password: str) -> bool:
        try:
            client = await self._get_client()
            client.cookies.clear()  # drop stale session cookies before re-auth
            await client.get("/login/index.php")
            resp = await client.post(
                "/login/index.php",
                data={
                    "username": username,
                    "password": password,
                    "loginbutton": "Log in",
                },
            )
        except httpx.HTTPError as exc:
            raise EClassUnavailableError(f"E-Class unreachable: {exc}") from exc

        if not self._has_session_cookie():
            raise EClassAuthError("Login failed — no session cookie received")
        if "Invalid login" in resp.text or "invalidlogin" in resp.text:
            raise EClassAuthError("Invalid username or password")
        if "/login/index.php" in str(resp.url):
            raise EClassAuthError("Login failed — check credentials")
        # A MoodleSession cookie alone is NOT proof of authentication: Moodle
        # issues one to guests as well. Confirm by loading an authenticated
        # page; _get_text raises EClassAuthError if it redirects to login.
        try:
            await self._get_text("/my/")
        except EClassAuthError as exc:
            raise EClassAuthError("Login failed — session not authenticated") from exc
        return True

    async def refresh_session(self, username: str) -> bool:
        """E-Class (Moodle) provides no token refresh endpoint: the session is
        a server-side Moodle session extended by activity. Normal sync requests
        are the keep-alive; when the session dies the student re-authenticates
        once. Nothing to do here."""
        return self._has_session_cookie() if self._client is not None else False

    def _has_session_cookie(self) -> bool:
        assert self._client is not None
        return any(name.startswith(SESSION_COOKIE_PREFIX) for name in self._client.cookies.keys())

    def session_data(self) -> str:
        assert self._client is not None
        # Iterate the raw jar: Cookies.items() raises CookieConflict when the
        # same name exists for multiple domains/paths. Last write wins.
        cookies = {c.name: c.value for c in self._client.cookies.jar}
        return encrypt(json.dumps(cookies))

    def load_session_data(self, encrypted: str) -> None:
        assert self._client is not None
        for name, value in json.loads(decrypt(encrypted)).items():
            self._client.cookies.set(name, value)

    # ---------- data ----------

    async def get_courses(self) -> list[Course]:
        html = await self._get_text("/local/ubion/user/")
        courses: list[Course] = []
        for m in re.finditer(
            r'course/view\.php\?id=(\d+)"[^>]*class="coursefullname">([^<]+)</a>.*?<td class="text-center">([^<]+)</td>',
            html, re.S,
        ):
            name = unescape(m.group(2)).strip()
            professor = unescape(m.group(3)).strip()
            kind_m = re.search(r'label-course">(ONLINE|OFFLINE)<', html[max(0, m.start()-200):m.start()])
            courses.append(Course(external_id=m.group(1), name=name, professor=professor,
                                  kind=kind_m.group(1) if kind_m else None))
        return courses

    async def get_assignments(self) -> list[Assignment]:
        out: list[Assignment] = []
        for course in await self.get_courses():
            html = await self._get_text(f"/mod/assign/index.php?id={course.external_id}")
            for m in re.finditer(
                r'mod/assign/view\.php\?id=(\d+)">([^<]+)</a></td>\s*'
                r'<td class="cell c2"[^>]*>([^<]*)</td>\s*'
                r'<td class="cell c3"[^>]*>([^<]*)</td>\s*'
                r'<td class="cell c4[^>]*>([^<]*)</td>',
                html,
            ):
                deadline = _parse_mdy(m.group(3))
                grade = m.group(5).strip()
                score, max_score = (grade.split("/") + [None])[:2] if grade and grade != "-" else (None, None)
                out.append(Assignment(
                    external_id=m.group(1), course_external_id=course.external_id,
                    course_name=course.name, title=unescape(m.group(2)).strip(),
                    kind="homework", deadline=deadline, submission_status=unescape(m.group(4)).strip(),
                    score=score.strip() if score else None, max_score=max_score.strip() if max_score else None,
                    description=None, submission_url=f"{self._base_url}/mod/assign/view.php?id={m.group(1)}",
                ))
        return out

    async def get_quizzes(self) -> list[Assignment]:
        out: list[Assignment] = []
        for course in await self.get_courses():
            html = await self._get_text(f"/mod/quiz/index.php?id={course.external_id}")
            for m in re.finditer(
                r'<a href="view\.php\?id=(\d+)">([^<]+)</a></td>\s*'
                r'<td class="cell c2[^"]*"[^>]*>([^<]*)</td>\s*'
                r'<td class="cell c3[^"]*"[^>]*>([^<]*)</td>',
                html,
            ):
                qid, title, closes, grade = m.group(1), m.group(2), m.group(3), m.group(4).strip()
                score, max_score = (grade.split("/") + [None])[:2] if grade and grade != "-" else (None, None)
                out.append(Assignment(
                    external_id=qid, course_external_id=course.external_id,
                    course_name=course.name, title=unescape(title).strip(),
                    kind="quiz", deadline=_parse_mdy(closes),
                    submission_status=None, score=score.strip() if score else None,
                    max_score=max_score.strip() if max_score else None, description=None,
                    submission_url=f"{self._base_url}/mod/quiz/view.php?id={qid}",
                ))
        for q in out:
            try:
                detail = await self._get_text(f"/mod/quiz/view.php?id={q.external_id}")
                m = re.search(r"This quiz opened at ([^<]+)", detail)
                if m:
                    q.open_time = _parse_mdy(m.group(1))
                else:
                    # Not opened yet: "The quiz will not be available until <date>"
                    m = re.search(r"The quiz will not be available until ([^<]+)", detail)
                    if m:
                        q.open_time = _parse_mdy(m.group(1))
                m = re.search(r"Time limit: ([^<]+)", detail)
                if m:
                    q.time_limit = m.group(1).strip()
                m = re.search(r"Attempts allowed: ([^<]+)", detail)
                if m:
                    q.attempts_allowed = m.group(1).strip()
                # Attempt state is shown on the quiz view page ("Summary of your
                # previous attempts" with State: Finished / Submitted ...).
                if "Summary of your previous attempts" in detail:
                    q.submission_status = "Completed"
            except Exception:
                pass
        return out

    async def get_schedule(self, start: date, end: date) -> list[Lesson]:
        # Timetable lives on EduPage, not E-Class. Kept for interface compliance.
        return []

    async def _get_text(self, path: str) -> str:
        from urllib.parse import urlparse

        client = await self._get_client()
        resp = await client.get(path)
        login_path = urlparse(str(resp.url)).path.rstrip("/")
        if (
            resp.status_code in (401, 403)
            or login_path.endswith("/login")
            or login_path.endswith("/login/index.php")
            or login_path.endswith("/login.php")
        ):
            raise EClassAuthError("Session expired")
        return resp.text
