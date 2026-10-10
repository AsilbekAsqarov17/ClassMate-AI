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
from app.eclass.models import AttendanceRecord, CourseAttendance
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


# Word -> canonical status. Ordered longest-alias-first at match time so that
# "unexcused absence" wins over "absence" and "excused absence" wins over
# "absence" (see _STATUS_RE). Note "attendance" is deliberately NOT an alias:
# it is the page/activity name, not a status, and treating it as "Present"
# misclassified any row that merely mentioned the word attendance.
_STATUS_ALIASES = {
    # Present
    "present": "Present", "attended": "Present", "출석": "Present",
    # Absent
    "absent": "Absent", "absence": "Absent", "absences": "Absent",
    "unexcused": "Absent", "unexcused absence": "Absent",
    "not present": "Absent", "no show": "Absent",
    "missed": "Absent", "missing": "Absent", "결석": "Absent",
    # Late
    "late": "Late", "tardy": "Late", "지각": "Late",
    # Excused
    "excused": "Excused", "excuse": "Excused", "justified": "Excused",
    "excused absence": "Excused", "공결": "Excused",
}

_DATE_PATTERNS = [
    re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"),          # 2026-10-07
    re.compile(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"),          # 10/7/2026
    re.compile(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s+\d{4})?", re.I),
    re.compile(r"(?:January|February|March|April|June|July|August|September|October|November|December)\s+\d{1,2}", re.I),
    re.compile(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)", re.I),
]


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _parse_date(text: str) -> str | None:
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0).strip()
    return None


def _build_status_regex() -> re.Pattern[str]:
    """Longest-alias-first alternation so the most specific phrase wins.

    Built from _STATUS_ALIASES so adding a label there is enough; the parser
    never hardcodes a status word separately.
    """
    aliases = sorted(_STATUS_ALIASES, key=len, reverse=True)
    parts = [
        r"\b" + r"\s+".join(re.escape(w) for w in a.split()) + r"\b" if a.isascii() else re.escape(a)
        for a in aliases
    ]
    return re.compile("|".join(parts), re.I)


_STATUS_RE = _build_status_regex()


def _parse_grade(raw: str | None) -> tuple[str | None, str | None]:
    """Split one E-Class grade cell into (score, max_score).

    E-Class renders the grade column in several shapes; only unambiguous ones
    are accepted, so a garbled cell yields (None, None) rather than a
    fabricated mark:
        "8.00/10.00"          -> ("8.00", "10.00")
        "8.00 out of 10.00"   -> ("8.00", "10.00")
        "8.00"                -> ("8.00", None)     # max not published
        "-" / "" / "None"     -> (None, None)       # not graded
    Anything with more than one unpaired number ("8.00 (60%)") is treated as
    unparseable: guessing which number is the mark would risk notifying with
    the wrong value.
    """
    if raw is None:
        return None, None
    text = unescape(_strip_tags(raw)).strip()
    if not text or text in {"-", "--", "–", "N/A", "None", "null"}:
        return None, None

    m = re.match(r"(-?\d+(?:[.,]\d+)?)\s*(?:/|\s+out\s+of\s+)\s*(-?\d+(?:[.,]\d+)?)\s*$", text, re.I)
    if m:
        return m.group(1), m.group(2)

    m = re.fullmatch(r"(-?\d+(?:[.,]\d+)?)\s*%?", text)
    if m:
        return m.group(1), None
    return None, None


def _parse_attempt_mark(detail: str) -> tuple[str | None, str | None]:
    """Recover a quiz mark from the quiz attempt summary page.

    The quiz index shows "-" for a closed attempt, so the mark has to come
    from here. Formats are tried most specific first because several appear
    together on the page.
    """
    # Scalar forms first — unambiguous and independent of table layout.
    for pat in (
        # "Marks: 8.00/10.00"
        r"Marks?\s*:?\s*(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)",
        # "Raw score 8.00/10.00."  /  "Score: 8.00 out of 10.00"
        r"(?:Raw\s+)?[Ss]core\s*:?\s*(\d+(?:[.,]\d+)?)\s*(?:/|\s+out\s+of\s+)\s*(\d+(?:[.,]\d+)?)",
    ):
        m = re.search(pat, detail, re.S | re.I)
        if m:
            return m.group(1), m.group(2)

    # Attempts summary table: locate the "Marked out of" column by its header
    # rather than counting <td>s. The table also has a "Raw" column holding a
    # second copy of the mark, so positional matching returns (8.00, 8.00).
    return _parse_attempts_table(detail)


_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)


def _parse_attempts_table(detail: str) -> tuple[str | None, str | None]:
    """Read Marks and 'Marked out of' from a mod_quiz attempts summary table."""
    for table in _TABLE_RE.findall(detail):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I)
        headers: list[str] | None = None
        marks_idx = max_idx = None
        for row in rows:
            cells = [unescape(_strip_tags(c)).strip() for c in re.findall(
                r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
            if not cells:
                continue
            lowered = [c.lower() for c in cells]
            if any("marked out of" in c for c in lowered):
                headers = lowered
                for i, c in enumerate(lowered):
                    if c == "marks" or c.startswith("marks "):
                        marks_idx = i
                    if "marked out of" in c:
                        max_idx = i
                break
        if headers is None or marks_idx is None or max_idx is None:
            continue
        # first data row whose state is a finished state
        for row in rows:
            cells = [unescape(_strip_tags(c)).strip() for c in re.findall(
                r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
            if len(cells) <= max(marks_idx, max_idx):
                continue
            if not any(c.lower() in {"finished", "submitted"} for c in cells):
                continue
            score = _parse_grade(cells[marks_idx])
            maximum = _parse_grade(cells[max_idx])
            if score[0] is not None:
                return score[0], maximum[0]
    return None, None


def _parse_status(text: str) -> str | None:
    """Canonical status named by a fragment, or None.

    Uses a single longest-match alternation instead of iterating the alias
    dict, so "unexcused absence" resolves to Absent rather than to whatever
    shorter alias happens to come first.
    """
    m = _STATUS_RE.search(text)
    return _STATUS_ALIASES[m.group(0).lower()] if m else None


_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_NUM_RE = re.compile(r"\d{1,4}")
# Maximum characters allowed between a status label and its count. Wide
# enough for "Absent: 5" / "Absent (5)" / "Absent - 5", narrow enough that a
# label cannot reach across a sentence into an unrelated number.
_MAX_GAP = 8


def _cells(row_html: str) -> list[str]:
    return [unescape(_strip_tags(c)).strip() for c in _CELL_RE.findall(row_html)]


def _is_standalone_number(text: str, start: int, end: int) -> bool:
    """True when text[start:end] is a count, not part of a date or a time.

    Guards against "10:00 Present" binding the clock's minutes to Present and
    "2026-10-07 Absent" binding the day to Absent.
    """
    if start == end:
        return False
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    # A digit or letter on either side means this is part of a larger token.
    if before.isalnum() or after.isalnum():
        return False
    # A separator here is only date/time punctuation when a digit follows it
    # through it: 2026-10-07, 10:00. A bare "Absent: 0" colon is fine.
    # NB: test for a non-empty neighbour first — "" is a substring of every
    # string in Python, so an unguarded `before in "./-:"` always fires.
    if before and before != ":" and before in "./-:":
        return False
    if after and after in "./-":
        return False
    if before == ":" and start >= 2 and text[start - 2].isdigit():
        return False
    if after == ":" and end + 1 < len(text) and text[end + 1].isdigit():
        return False
    return True


def _inline_counts(text: str) -> dict[str, int]:
    """Pair a status label with a count in running prose.

    Used for summary lines that are not table rows, e.g.
        "Summary — Present: 18  Late: 1  Absent: 5  Excused: 0"

    A label only binds to a number when the text between them is pure
    separator ("", ":", "(", "-", ...). That restriction is what the previous
    implementation got wrong: it allowed up to five arbitrary non-digit
    characters between label and number, so a label could reach across a word
    into an unrelated number, and any label separated from its count by markup
    longer than that window never matched — which is why Absent came back
    missing while a neighbouring Late label still resolved.
    """
    out: dict[str, int] = {}
    matches = list(_STATUS_RE.finditer(text))
    claimed: set[int] = set()

    for idx, m in enumerate(matches):
        canon = _STATUS_ALIASES[m.group(0).lower()]
        # The window stops at the next status label so one label's count can
        # never be taken from the following label's field.
        limit = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        after = text[m.end():min(limit, m.end() + _MAX_GAP)]
        n = _NUM_RE.search(after)
        if (n and not re.search(r"[A-Za-z]", after[:n.start()])
                and _is_standalone_number(text, m.end() + n.start(), m.end() + n.end())):
            out[canon] = max(out.get(canon, 0), int(n.group(0)))
            claimed.add(m.end() + n.start())
            continue
        # "N Label" — number immediately before the label, if unclaimed.
        abs_start = max(m.start() - _MAX_GAP, 0)
        before = text[abs_start:m.start()]
        nums = list(_NUM_RE.finditer(before))
        if nums:
            n = nums[-1]
            abs_pos = abs_start + n.start()
            abs_end = abs_start + n.end()
            if (abs_pos not in claimed
                    # No letters anywhere in the window: "Week 7 Present" must
                    # not bind Present to the week number.
                    and not re.search(r"[A-Za-z]", before)
                    and _is_standalone_number(text, abs_pos, abs_end)):
                out.setdefault(canon, int(n.group(0)))
                claimed.add(abs_pos)
    return out


def _summary_counts_from_row(cells: list[str]) -> dict[str, int]:
    """Pull 'Status: N' / 'Status N' / 'N Status' pairs out of one table row.

    Handles both layouts E-Class uses:
      * summary row  — <td>Present</td><td>18</td><td>Late</td><td>1</td>...
      * label/value  — <td>Absent</td><td>5</td> inside a per-status row
    A status word only pairs with a number when the number is the adjacent
    cell, which keeps Late from swallowing an unrelated number further along.
    """
    out: dict[str, int] = {}
    for i, cell in enumerate(cells):
        status = _parse_status(cell)
        if not status:
            continue
        # Count in the adjacent cell: <td>Absent</td><td>5</td>
        if i + 1 < len(cells):
            nxt = cells[i + 1]
            n = _NUM_RE.search(nxt)
            if n and not _STATUS_RE.search(nxt) and _is_standalone_number(nxt, n.start(), n.end()):
                out[status] = max(out.get(status, 0), int(n.group(0)))
                continue
        # Count in the same cell: "Absent (5)"
        n = _NUM_RE.search(_STATUS_RE.sub("", cell))
        if n and _is_standalone_number(cell, n.start(), n.end()):
            out[status] = max(out.get(status, 0), int(n.group(0)))
    # "5 Absent" (count before the label) — last cell of the row only, to
    # avoid pairing a stray leading number with an unrelated status label.
    if cells and not _STATUS_RE.search(cells[0]):
        n = _NUM_RE.search(cells[0])
        if n and len(cells) > 1:
            for cell in cells[1:]:
                status = _parse_status(cell)
                if status and _NUM_RE.search(cell):
                    out.setdefault(status, int(n.group(0)))
                break
    return out


def parse_attendance_html(html: str) -> tuple[dict[str, int] | None, list[AttendanceRecord]]:
    """Parse a Moodle mod_attendance report page.

    Returns (counts, records):
      counts=None  → no parseable attendance data anywhere on the page
                     (blank / unrecorded). Callers must treat this as
                     "attendance not available", NEVER as 0 absences.
      counts=dict  → per-status totals. A status key is present ONLY when
                     E-Class actually reported that status on the page, so a
                     missing "Absent" key means "not reported", not zero.

    Absence extraction notes: absences come from the report's own summary
    row/cells ("Absent: 5") or, when the report has no summary, from counting
    the per-session rows whose status cell is an absence label. Absences are
    never inferred from class counts or from other unrelated numbers.
    """
    rows = _ROW_RE.findall(html)
    records: list[AttendanceRecord] = []
    counts: dict[str, int] = {}

    for row in rows:
        cells = _cells(row)
        row_text = " ".join(cells) if cells else unescape(_strip_tags(row)).strip()
        status = _parse_status(row_text)
        day = _parse_date(row_text)
        is_session_row = bool(status and day)
        if is_session_row:
            records.append(AttendanceRecord(date=day, status=status))
            # A dated session row carries a status, not a count. Running the
            # count extractors over it would read the time/date digits as
            # counts, so only summary rows are scanned for numbers.
            continue
        counts.update(_summary_counts_from_row(cells))
        counts.update(_inline_counts(row_text))

    # Summary text that lives outside any table row.
    plain = unescape(_strip_tags(html))
    if not counts:
        counts.update(_inline_counts(plain))

    if not counts and not records:
        return None, []

    if records:
        # No explicit summary on the page (or summary missing a status):
        # derive the counts from the per-session rows themselves.
        derived: dict[str, int] = {}
        for r in records:
            derived[r.status] = derived.get(r.status, 0) + 1
        for status, n in derived.items():
            counts.setdefault(status, n)

    # A status absent from the page stays absent from the dict on purpose —
    # the caller renders "unavailable" rather than inventing a zero.
    return counts, records


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
                score, max_score = _parse_grade(m.group(5))
                out.append(Assignment(
                    external_id=m.group(1), course_external_id=course.external_id,
                    course_name=course.name, title=unescape(m.group(2)).strip(),
                    kind="homework", deadline=deadline, submission_status=unescape(m.group(4)).strip(),
                    score=score, max_score=max_score,
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
                qid, title, closes = m.group(1), m.group(2), m.group(3)
                score, max_score = _parse_grade(m.group(4))
                out.append(Assignment(
                    external_id=qid, course_external_id=course.external_id,
                    course_name=course.name, title=unescape(title).strip(),
                    kind="quiz", deadline=_parse_mdy(closes),
                    submission_status=None, score=score, max_score=max_score, description=None,
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
                # The quiz index prints "-" in the Grade column once the
                # attempt is closed, so the mark must be read from the attempt
                # page. Only fills a gap: an index-published grade wins.
                if q.score is None:
                    mark, mark_max = _parse_attempt_mark(detail)
                    if mark is not None:
                        q.score = mark
                        if mark_max is not None:
                            q.max_score = mark_max
            except Exception:
                pass
        return out

    async def get_schedule(self, start: date, end: date) -> list[Lesson]:
        # Timetable lives on EduPage, not E-Class. Kept for interface compliance.
        return []

    # ---------- attendance ----------

    async def get_attendance(self) -> list[CourseAttendance]:
        out: list[CourseAttendance] = []
        for course in await self.get_courses():
            out.append(await self.get_course_attendance(course))
        return out

    async def get_course_attendance(self, course: Course) -> CourseAttendance:
        try:
            html = await self._get_text(f"/course/view.php?id={course.external_id}")
        except EClassAuthError:
            raise  # session problem — never convert this to "unavailable"
        links = re.findall(r'href="([^"]*attendance[^"]*)"', html, re.IGNORECASE)
        if not links:
            # Professor does not track attendance through E-Class at all.
            return CourseAttendance(course_external_id=course.external_id,
                                    course_name=course.name, available=False)
        from urllib.parse import urlparse

        target = links[0]
        parsed = urlparse(target)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        try:
            att_html = await self._get_text(path)
        except (EClassAuthError, EClassUnavailableError):
            raise  # session/network problems are errors, not "unavailable"
        except Exception:
            # Attendance activity link exists but the page is unreadable in an
            # unexpected way: treat as unavailable rather than inventing zeros.
            return CourseAttendance(course_external_id=course.external_id,
                                    course_name=course.name, available=False)
        counts, records = parse_attendance_html(att_html)
        if counts is None:
            return CourseAttendance(course_external_id=course.external_id,
                                    course_name=course.name, available=False)
        # counts.get(...) → None when E-Class did not report that status at
        # all, which is deliberately distinct from a reported 0.
        return CourseAttendance(
            course_external_id=course.external_id, course_name=course.name,
            available=True,
            absences=counts.get("Absent"), late=counts.get("Late"),
            present=counts.get("Present"), excused=counts.get("Excused"),
            records=records,
        )

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
