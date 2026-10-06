"""EduPage timetable adapter.

Public flow (reverse-engineered from aSC EduPage `ttviewer`):

  1. GET  /timetable/view.php?class=<GROUP>
     → server attaches the user context "Trieda<GROUP>".
  2. POST /timetable/server/ttviewer.js?__func=getTTViewerData
     {"__args":[null,<year>],"__gsh":"00000000"}
     → returns the default timetable serial (`regular.default_num`).
  3. POST /timetable/server/regulartt.js?__func=regularttGetData
     {"__args":[null,<tt_num>],"__gsh":"00000000"}
     → timetable document (tables of rows: classes, lessons, cards,
       periods, classrooms, teachers, subjects).
  4. (needed for row data) POST /rpr/server/maindbi.js?__func=mainDBIAccessor

NOTE: anonymous calls DO return the row payloads, but under the
`data_rows` key (not `rows`) in each table object. Student-group
timetables render fine anonymously; only personal items need a login.
"""

import re
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from app.config.settings import get_settings
from app.eclass.models import Lesson
from app.edupage.client import EduPageError

BASE = "https://iut.edupage.org"
GSH_PUBLIC = "00000000"


class EduPageWebClient:
    def __init__(self, base_url: str | None = None) -> None:
        base = (base_url or get_settings().edupage_timetable_url or BASE).rstrip("/")
        if base.endswith("/timetable"):
            base = base[: -len("/timetable")]
        self._base_url = base
        self._tt_num: str | None = None

    def _client(self) -> httpx.AsyncClient:
        headers = {}
        cookie = get_settings().edupage_cookie
        if cookie:
            headers["Cookie"] = cookie
        return httpx.AsyncClient(base_url=self._base_url, timeout=httpx.Timeout(20.0), follow_redirects=True, headers=headers)

    async def _get_gsh(self, client: httpx.AsyncClient, group: str) -> str:
        r = await client.get(f"/timetable/view.php?class={group}")
        m = re.search(r'ASC\.gsechash\s*=\s*"([0-9a-fA-F]+)"', r.text)
        return m.group(1) if m else GSH_PUBLIC

    async def resolve_group_name(self, group: str) -> str | None:
        """Return the official EduPage class name matching the user's input, or None."""
        try:
            async with self._client() as c:
                gsh = await self._get_gsh(c, group)
                r1 = await c.post(
                    "/timetable/server/ttviewer.js?__func=getTTViewerData",
                    json={"__args": [None, date.today().year], "__gsh": gsh},
                )
                if r1.status_code != 200 or not r1.text.strip().startswith("{"):
                    return None
                tt_num = str(r1.json()["r"]["regular"]["default_num"])
                r2 = await c.post(
                    "/timetable/server/regulartt.js?__func=regularttGetData",
                    json={"__args": [None, tt_num], "__gsh": gsh},
                )
            if r2.status_code != 200 or not r2.text.strip().startswith("{"):
                return None
            doc = r2.json().get("r", {}).get("dbiAccessorRes", {})
            tables = {t["id"]: (t.get("data_rows") or t.get("rows", [])) for t in doc.get("tables", [])}
            for row in tables.get("classes", []):
                if "name" in row and _group_matches(row["name"], group):
                    return row["name"]
            return None
        except Exception:
            return None

    async def get_tt_num(self, group: str) -> str:
        async with self._client() as c:
            gsh = await self._get_gsh(c, group)
            r1 = await c.post(
                "/timetable/server/ttviewer.js?__func=getTTViewerData",
                json={"__args": [None, date.today().year], "__gsh": gsh},
            )
        try:
            return str(r1.json()["r"]["regular"]["default_num"])
        except (KeyError, ValueError) as exc:
            raise EduPageError("Cannot resolve EduPage timetable serial") from exc

    async def get_timetable(self, group: str, start: date, end: date) -> list[Lesson]:
        if not self._base_url:
            raise EduPageError("EDUPAGE_TIMETABLE_URL is not configured")
        async with self._client() as c:
            gsh = await self._get_gsh(c, group)
            r1 = await c.post(
                "/timetable/server/ttviewer.js?__func=getTTViewerData",
                json={"__args": [None, start.year], "__gsh": gsh},
            )
            if r1.status_code != 200 or not r1.text.strip().startswith("{"):
                raise EduPageError(f"EduPage getTTViewerData failed: HTTP {r1.status_code}")
            tt_num = str(r1.json()["r"]["regular"]["default_num"])
            r2 = await c.post(
                "/timetable/server/regulartt.js?__func=regularttGetData",
                json={"__args": [None, tt_num], "__gsh": gsh},
            )
        if r2.status_code != 200 or not r2.text.strip().startswith("{"):
            raise EduPageError(f"EduPage regularttGetData failed: HTTP {r2.status_code}")
        try:
            payload = r2.json()
            doc = payload.get("r", {}).get("dbiAccessorRes", {})
        except ValueError as exc:
            raise EduPageError(f"EduPage regularttGetData returned non-JSON: {r2.text[:60]}") from exc
        tables = {t["id"]: (t.get("data_rows") or t.get("rows", [])) for t in doc.get("tables", [])}
        if not any(len(v) for v in tables.values()):
            raise EduPageError("EduPage returned an empty timetable (no data_rows in any table).")
        try:
            meta = r1.json().get("r", {}).get("regular", {})
            tmeta = meta.get("timetables", [{}])[0]
            term_start = date.fromisoformat(str(tmeta.get("datefrom"))) if tmeta.get("datefrom") else None
        except (ValueError, IndexError, KeyError):
            term_start = None
        return self._parse_lessons(tables, group, start, end, term_start=term_start)

    def _parse_lessons(
        self,
        tables: dict[str, list[dict]],
        group: str,
        start: date,
        end: date,
        term_start: date | None = None,
    ) -> list[Lesson]:
        classes = {row["id"]: row for row in tables.get("classes", []) if "id" in row}
        target = next((c for c in classes.values() if _group_matches(c.get("name", ""), group)), None)
        if target is None:
            available = ", ".join(sorted(c.get("name", "") for c in classes.values()))
            raise EduPageError(f"Group '{group}' not found in EduPage classes. Available: {available}")

        lessons_by_id = {r["id"]: r for r in tables.get("lessons", []) if "id" in r}
        classrooms = {r["id"]: r for r in tables.get("classrooms", []) if "id" in r}
        teachers = {r["id"]: r for r in tables.get("teachers", []) if "id" in r}
        subjects = {r["id"]: r for r in tables.get("subjects", []) if "id" in r}
        periods = {r["id"]: r for r in tables.get("periods", []) if "id" in r}
        ordered_periods = sorted(periods.values(), key=lambda p: int(p.get("period", 0) or 0))

        out: list[Lesson] = []
        for card in tables.get("cards", []):
            lesson_doc = lessons_by_id.get(card.get("lessonid"))
            if not lesson_doc:
                continue
            if target["id"] not in (lesson_doc.get("classids") or []):
                continue
            doc = subjects.get(lesson_doc.get("subjectid"), {})
            subject = doc.get("short") or doc.get("name") or "Lesson"
            teacher = next((_teacher_name(teachers.get(tid)) for tid in (lesson_doc.get("teacherids") or []) if teachers.get(tid)), None)
            room_doc = next((classrooms.get(rid) for rid in (card.get("classroomids") or []) if classrooms.get(rid)), None)
            room = (room_doc.get("short") or room_doc.get("name")) if room_doc else None
            start_period = periods.get(str(card.get("period")), {})
            if not start_period:
                continue
            try:
                idx = ordered_periods.index(start_period)
            except ValueError:
                idx = 0
            duration = int(lesson_doc.get("durationperiods") or 1)
            end_period = ordered_periods[min(idx + max(duration, 1) - 1, len(ordered_periods) - 1)]
            for d in _expand_card_dates(card, start, end, term_start=term_start):
                out.append(Lesson(
                    external_id=f"{card.get('id', '')}_{d.isoformat()}",
                    course_external_id=str(lesson_doc.get("subjectid", "")),
                    course_name=subject,
                    date=d,
                    start_time=str(start_period.get("starttime", "09:00"))[:5],
                    end_time=str(end_period.get("endtime", "09:45"))[:5],
                    room=room,
                    professor=teacher,
                ))
        return out


def _teacher_name(t: dict | None) -> str | None:
    if not t:
        return None
    return t.get("short") or t.get("name")


def _group_matches(candidate: str, wanted: str) -> bool:
    """'ICE-24-01' should match class 'ICE24-1'. Compare alnum tokens, ints normalized."""
    def norm(s: str):
        toks = re.findall(r"[a-z]+|\d+", s.lower())
        return tuple(int(t) if t.isdigit() else t for t in toks)

    c, w = norm(candidate), norm(wanted)
    return c == w or c[: len(w)] == w if w else c == w


def _expand_card_dates(card: dict, start: date, end: date, term_start: date | None = None):
    """EduPage `days` is a 5-char bit string (Mon..Fri, '10000' == Monday);
    `weeks` is a repeating bit pattern ('1' == every week)."""
    days = str(card.get("days") or "").strip()
    weeks = str(card.get("weeks") or "").strip()
    if not days or not weeks:
        return
    t0 = term_start or date(start.year, 9, 7)
    day = start
    while day <= end:
        weekday = day.weekday()
        if weekday < len(days) and days[weekday] == "1":
            week_index = (day - t0).days // 7 if day >= t0 else (day - t0).days // 7
            if week_index >= 0 and weeks[week_index % len(weeks)] == "1":
                yield day
        day += timedelta(days=1)
