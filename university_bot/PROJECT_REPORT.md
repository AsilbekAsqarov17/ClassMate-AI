# ClassMate AI — Project Report

Technical reference for the ClassMate AI Telegram bot. Written from the current
source tree (`university_bot/`), SQLAlchemy models, Alembic history, and the
automated test suite. Where behaviour depends on a live university service, the
report states plainly whether it has been verified against that service or only
through fixtures.

---

## 1. Project overview

### What it does

ClassMate AI is a Telegram bot that acts as a personal academic assistant for a
single university. It authenticates against the university's **E-Class** (Moodle)
portal using the student's own credentials, keeps the resulting session, and
then surfaces the student's academic state inside Telegram:

- the weekly timetable (fetched from **EduPage**),
- upcoming assignments, quizzes, and deadlines,
- grades, with a push notification the moment one appears,
- per-course attendance.

Notifications are pushed, not polled by the user: the bot runs a background
scheduler that pushes deadline reminders, class reminders, a daily timetable
summary, new grades, and a weekly attendance digest.

### Intended users and problems solved

The target user is a student of the covered university who already has an
E-Class account. Registration is gated by a whitelist of Student IDs, so the bot
is a closed pilot rather than an open public service.

It solves four recurring problems:

| Problem | Approach |
|---|---|
| Checking the timetable from a phone | `/today`, `/tomorrow`, `/week`, `/next` plus a daily summary push |
| Missing an assignment deadline | Two reminders per item, at 4h and 1h before the deadline |
| Not noticing a released grade | Grade-diff detection during periodic sync, then a push |
| Manually auditing attendance | `/attendance` per course, plus a Saturday digest |

### Technologies

| Layer | Technology |
|---|---|
| Telegram interface | `aiogram` 3.x (async, long polling, FSM) |
| Database | PostgreSQL via `asyncpg`; SQLAlchemy 2.0 async ORM |
| Migrations | Alembic |
| Config | `pydantic-settings` + `.env` |
| Scheduling | APScheduler (async) |
| HTTP scraping | `httpx` |
| Session encryption | `cryptography` (Fernet) |
| Logging | stdlib `logging` + `structlog` |
| Tests | `pytest` with `pytest-asyncio`, SQLite in-memory via `aiosqlite` |

### Implementation status and known limitations

Feature-complete against its stated scope; all 206 automated tests pass.

Known limitations, stated honestly:

- **No live end-to-end verification.** Every E-Class and Telegram interaction is
  covered by fixtures and mocks. The parsers are written against observed Moodle
  markup but have not been re-validated against a live account during this work
  (see §12).
- **Passwordless by design.** There is no password fallback. When an E-Class
  session dies the student must reconnect with `/start` or `/settings`.
- **Polling-based.** The bot holds one Telegram long-polling connection and one
  APScheduler instance. Running a second process would duplicate every
  notification (see §13).
- **No retry queue.** A failed Telegram send is logged and dropped for that tick.
  Deduplication records for deadline reminders are written at collection time,
  so a send failure means that reminder is not retried — it is simply skipped
  for that threshold. Grade notifications behave differently and *are* retried
  (§6.6).
- **Attendance depends on the professor.** Not every E-Class course records
  attendance, and the bot reports that honestly rather than inventing zeros (§7).

---

## 2. Architecture and file structure

### Layering

The application is a strict four-layer pipeline. Each layer may call the one
below it; nothing calls upward.

```
Telegram  ──►  Services  ──►  E-Class / EduPage clients
   │              │
   └──────────────┴────────►  Repositories ──►  Database (SQLAlchemy)
```

### Directory overview

```
university_bot/
├── app/
│   ├── main.py                  entry point: Bot + Dispatcher + Scheduler
│   ├── config/
│   │   └── settings.py          pydantic-settings; reads .env
│   ├── bot/
│   │   ├── handlers/            one module per command family
│   │   │   ├── start.py         /start, login FSM, /help
│   │   │   ├── schedule.py      /today /tomorrow /week /next
│   │   │   ├── academic.py      /assignments /quizzes /deadlines /scores /sync
│   │   │   ├── attendance.py    /attendance + course-picker callbacks
│   │   │   └── settings.py      /settings + inline keyboards + FSM states
│   │   ├── keyboards/
│   │   │   └── main_menu.py     main reply keyboard button labels
│   │   └── states/
│   │       └── login.py         FSM states for the login flow
│   ├── eclass/
│   │   ├── client.py            abstract EClassClient + error taxonomy
│   │   ├── models.py            transport-agnostic dataclasses
│   │   └── web_client.py        Moodle HTML scraping + parsing
│   ├── edupage/
│   │   ├── client.py            EduPageError
│   │   └── web_client.py        EduPage timetable JSON adapter
│   ├── services/                all business logic lives here
│   │   ├── academic_sync.py     watchlist upsert, grade detection, cleanup
│   │   ├── assignment_service.py upcoming/not-completed assignment queries
│   │   ├── attendance_service.py  attendance sync, persistence, formatting
│   │   ├── crypto.py            Fernet encrypt/decrypt for session blobs
│   │   ├── eclass_session.py    rebuild an authenticated client per account
│   │   ├── grade_service.py     score queries + overall average
│   │   ├── group_service.py     get_or_create_group
│   │   ├── notification_service.py  due_* collectors, dedup, score delivery
│   │   ├── quiz_service.py      quiz availability rules
│   │   ├── schedule_service.py  timetable queries + formatting
│   │   ├── sync_service.py      one account's full sync, returns SyncOutcome
│   │   └── validation.py        Student ID regex + normalization
│   ├── database/
│   │   ├── database.py          engine / session factory / dispose
│   │   ├── models/              one module per table
│   │   └── repositories/        user and allowed-student-ID repositories
│   └── scheduler/
│       └── jobs.py              all APScheduler job definitions
├── migrations/versions/         0001 … 0004 (head)
├── tests/                       pytest suite (206 tests)
├── reset_users.py               one-off local admin script
├── live_verify_attendance.py    one-off live attendance diagnostic
├── alembic.ini
├── requirements.txt
└── .env.example
```

### Component responsibilities

**Telegram handlers** (`app/bot/handlers/`) — parse the incoming update, resolve
the user's DB id, call exactly one service, and format the reply. They hold no
business logic and perform no HTTP calls to E-Class directly. Handlers that need
a multi-step conversation use aiogram FSM states.

**E-Class client** (`app/eclass/web_client.py`) — the only component that speaks
Moodle HTTP. It performs the login, serialises the session, and parses HTML into
the dataclasses in `app/eclass/models.py`. Its three-state contract is
load-bearing: a genuine page problem returns `available=False`, while a session
or network problem raises.

**EduPage client** (`app/edupage/web_client.py`) — a JSON adapter for aSC
EduPage's timetable API. It resolves a study-group name to EduPage's canonical
spelling and fetches lessons.

**Services** (`app/services/`) — all business logic. Notably
`sync_service.sync_user()` orchestrates one user's whole sync and returns a
`SyncOutcome` describing which parts succeeded, which lets the scheduler report
partial failures precisely instead of a single boolean.

**Repositories** (`app/database/repositories/`) — a deliberately thin layer; only
two exist today (`user_repository`, `allowed_student_id_repository`) where the
query has real reuse or domain meaning.

**Database** (`app/database/`) — engine lifecycle in `database.py`, SQLAlchemy
declarative models under `models/`.

**Scheduler** (`app/scheduler/jobs.py`) — the four job definitions and the
message builders. Each job is a thin loop that calls `notification_service`.

### Request flow, end to end

`/attendance` is representative:

1. `att_course` callback fires in `handlers/attendance.py`.
2. The handler calls `attendance_service.get_persisted()` — a pure PostgreSQL
   read of the user's `attendance` rows. **No E-Class request happens here.**
3. `attendance_service.format_attendance_row()` renders text, adding a stale
   banner when `attendance_is_stale()` is true.
4. The handler edits the message with a "back" keyboard.

The E-Class leg happens elsewhere, on the scheduler's 30-minute `sync_all` job:
`jobs.sync_all` → `sync_service.sync_user` → `attendance_service.sync_attendance`
→ `client.get_attendance()` → `get_course_attendance()` → `parse_attendance_html()`.
The scheduler then calls `get_persisted()` again to read back what was just
written. Reads and writes are therefore fully decoupled: `/attendance` is fast
and works even when E-Class is down.

---

## 3. Telegram commands and interface

Commands verified by reading the handlers, not assumed.

| Command | Handler | Behaviour |
|---|---|---|
| `/start` | `start.cmd_start` | If an active E-Class account exists, greets and shows the main menu. Otherwise enters the login FSM and asks for the Student ID. |
| `/help` | `start.cmd_help` | Static help text. Requires no authentication — works for unknown users. |
| `/today` | `schedule.cmd_today` | Lessons for today in the user's timezone; `🎉 No classes scheduled.` if empty. |
| `/tomorrow` | `schedule.cmd_tomorrow` | Same for tomorrow. |
| `/week` | `schedule.cmd_week` | Today through +6 days, grouped by date. |
| `/next` | `schedule.cmd_next` | Next active lesson with a "Starts in N minutes" countdown; `🎉 No upcoming classes.` if none. |
| `/assignments` | `academic.cmd_assignments` | Upcoming *and* completed-unscored homework (excludes quizzes). |
| `/quizzes` | `academic.cmd_quizzes` | Upcoming, not-yet-opened quizzes are skipped. |
| `/deadlines` | `academic.cmd_deadlines` | Upcoming deadlines with a local date/time. |
| `/scores` | `academic.cmd_scores` | Items with a non-null score, grouped by course, plus an average percentage. |
| `/sync` | `academic.cmd_sync` | Runs a full sync, then edits the placeholder with a per-subsystem ✅/⚠️ line each. |
| `/settings` | `settings.cmd_settings` | Current settings with an inline keyboard. |
| `/attendance` | `attendance.cmd_attendance` | Course picker; picking a course shows its detail view. |

Every command also answers to its main-menu button label, via stacked
`@router.message(F.text == BTN_*)` decorators.

### Inline keyboards

**Main menu** (`keyboards/main_menu.py`): `📅 Today`, `📆 Tomorrow`,
`🗓 This Week`, `➡️ Next Class`, `📝 Assignments`, `🔄 Sync`, `⚙️ Settings`,
`🧪 Quizzes`, `📌 Deadlines`, `📊 Scores`, `🗓 Attendance`.

**Attendance course picker** (`att:list` → per-course `att:course:<external_id>`),
with a `att:list` back button on each detail view.

**Settings** (`set:` namespace):
- `set:student_id` → `waiting_new_student_id` → `waiting_id_password`
- `set:reconnect` → `waiting_reconnect_password`
- `set:group` → `waiting_new_group`
- `set:notif` → toggles for daily/class/deadline notifications
- `set:notif:cycle_minutes` → cycles 15 → 30 → 60 → 15 minutes
- `set:back` → returns to the settings view

### Reconnect workflow

`/settings` → **🔐 Reconnect E-Class** → the bot asks for the password →
the message is deleted → login is attempted against E-Class →

- **Auth failure:** previous session is kept untouched, user stays in the FSM
  state and can retry.
- **E-Class unreachable:** session unchanged, message tells the user to retry
  later.
- **Success:** new encrypted session replaces the old one, `is_active = True`.

The same three-way handling is duplicated in `waiting_id_password` (for a new
Student ID) and in the `/start` flow.

---

## 4. Registration and allowed Student IDs

### Validation and normalization

`services/validation.py` defines the single source of truth:

```python
STUDENT_ID_RE = re.compile(r"^[uU]2\d{6}$")   # exactly 8 chars: u2 + 6 digits
```

`normalize_student_id()` strips whitespace and lowercases the value, returning
`None` for anything invalid. Accepted: `u2410037`, `U2410037`. Rejected:
`u241037` (too short), `u24100378` (too long), `u3410037` (wrong letter),
`x2410037`, `u2abcdef`, `u2-410037`, and bare `2410037` (missing the `u`).

### The whitelist

`allowed_student_ids` is an application-wide table (`models/allowed_student_id.py`,
introduced in migration `0003`). Access is through
`AllowedStudentIDRepository.is_allowed()`.

Flow in `start.got_student_id`:

1. Normalize. On failure → `INVALID_STUDENT_ID_MSG`, **stay in the state**, store
   nothing, let the user retry.
2. Check the whitelist. On rejection → `NOT_ALLOWED_STUDENT_ID_MSG`, again
   **staying in the state** with nothing stored.
3. Only on success does it advance to the password step.

So a rejected ID has no side effect whatsoever: no `User` row is written and no
E-Class request is attempted.

### Adding another allowed ID

The example below uses a fictional ID:

```sql
INSERT INTO allowed_student_ids (student_id, created_at)
VALUES ('u2999999', NOW())
ON CONFLICT (student_id) DO NOTHING;
```

Verify without printing the full list:

```sql
SELECT COUNT(*) FROM allowed_student_ids;
```

`student_id` is unique, so the `ON CONFLICT` clause makes re-running the insert
safe.

### Whitelist survives the user reset

`reset_users.py` deletes only the six user-owned tables. `allowed_student_ids`,
`groups`, and `lessons` are on an explicit preserve list and are never touched.
The intent of the reset is to clear bot registrations, not to revoke
authorization or erase the shared timetable.

---

## 5. E-Class authentication and security

### Initial login

`EClassWebClient.login(username, password)` (`eclass/web_client.py`):

1. `GET /login/index.php` to establish the pre-auth session.
2. `POST /login/index.php` with the credentials.
3. Check a `MoodleSession*` cookie was received.
4. Reject if the body contains "Invalid login" or the URL is still `/login/index.php`.
5. **Probe an authenticated page.** `GET /my/` — if it redirects to login, raise
   `EClassAuthError`.

Step 5 is essential: Moodle issues a `MoodleSession` cookie to anonymous
visitors too, so the cookie alone proves nothing about authentication.

### Failure handling

| Condition | Exception | User-visible result |
|---|---|---|
| No session cookie | `EClassAuthError` | "❌ E-Class authentication failed" |
| "Invalid login" in body | `EClassAuthError` | same |
| Redirected to `/login*` | `EClassAuthError` | same |
| Redirected to `/login.php` on `_get_text` | `EClassAuthError` | "session expired, reconnect" |
| HTTP error / timeout | `EClassUnavailableError` | "E-Class unavailable, try later" |
| Probe succeeds | — | session serialised and stored |

The distinction matters: an auth failure means *try again*, an availability
failure means *try later*, and only the former should keep the user in the FSM
state. `_get_text` raises `EClassAuthError` on any 401/403 or a login redirect.

### Password message deletion

Every handler that accepts a password does, before doing anything with it:

```python
try:
    await message.delete()
except Exception:
    pass
```

The deletion failure is swallowed deliberately — a user without delete rights
must still be able to log in. Deletion happens *before* the network call, so the
plaintext is not retained in the message object while awaiting E-Class.

### The password is never persisted

`del password` runs immediately after `session_data()` in every password path.
There is no `encrypted_password` column — migration `0001_drop_encrypted_password.py`
actively removes it, so the schema makes re-introduction awkward.

What *is* stored is the serialised cookie jar:

```python
def session_data(self) -> str:
    cookies = {c.name: c.value for c in self._client.cookies.jar}
    return encrypt(json.dumps(cookies))
```

### Fernet encryption

`services/crypto.py` wraps `cryptography.fernet.Fernet`. Only the cookie jar is
encrypted, using `ENCRYPTION_KEY` from `.env`.

**The key must stay stable across any deployment that reuses existing session
data.** Fernet is authenticated symmetric encryption: a different or rotated key
cannot decrypt rows written under the old one. Rotating `ENCRYPTION_KEY` while
`eclass_accounts.session_data` still holds data makes every stored session
undecryptable, and since passwords are never stored there is no way to recover
them except each student reconnecting individually. When rotating, plan either a
full re-registration or a migration that re-encrypts rows under the new key
before deploying it.

### Session validation, expiry, keepalive, reconnect

`client_for_account()` (`services/eclass_session.py`) rebuilds a client, loads the
decrypted cookies, and probes with `get_courses()`.

- Probe succeeds → usable client.
- Probe raises `EClassAuthError` → `SessionExpiredError`.
- Probe raises a network error → `EClassUnavailableError` (account stays active).

`sync_user()` maps `SessionExpiredError` to setting `is_active = False`,
committing, and setting `outcome.session_expired`. `jobs.sync_all` then notifies
the student to reconnect with `/start`. Moodle offers no token refresh, so the
session is extended only by activity: the 30-minute `sync_all` job doubles as the
keepalive. There is no password fallback by design.

### Sensitive data

Never log, print, or include in any report: the Telegram bot token, E-Class
passwords, `ENCRYPTION_KEY`, database credentials, `DATABASE_URL` contents,
`EDUPAGE_COOKIE`, raw or encrypted `session_data`, and raw Moodle/EduPage HTML
containing session identifiers. Diagnostic scripts in this repo print only
course-level or aggregate information by design.

---

## 6. E-Class assignments, quizzes, and grades

### Fetching

`get_courses()` scrapes `/local/ubion/user/`, matching rows that carry both a
`coursefullname` link and a professor cell.

`get_assignments()` iterates courses, fetches `/mod/assign/index.php?id=<course>`,
and matches rows of the shape:

```
mod/assign/view.php?id=<id>">Title</a></td>
<td class="cell c2">deadline</td>
<td class="cell c3">submission status</td>
<td class="cell c4">grade</td>
```

`get_quizzes()` does the same over `/mod/quiz/index.php`, then fetches each
quiz's view page for `open_time`, `time_limit`, `attempts_allowed`, and the
attempt state.

### Storage

`sync_academic_records()` upserts by `external_id` into `assignments`, and
returns the list of rows eligible for a grade notification. The table is a
**watchlist**, not a grade archive: rows are deleted once their grade has been
delivered, and expired unsubmitted items are pruned (§6.6).

### Lifecycle states

| State | `submission_status` | Kept? | Grade reminder? |
|---|---|---|---|
| Upcoming, not submitted | `No submission` / `None` | yes | yes |
| Submitted, awaiting grade | `Submitted for grading` | yes | no |
| Completed quiz | `Completed` | yes | no |
| Graded, notification pending | any | yes, until delivered | no |
| Expired, not submitted, ungraded | `No submission` | **no** (pruned) | no |

Completion and grading are strictly distinct: `is_submitted()` treats anything
outside `{"", "-", "no submission", "not submitted"}` as submitted, and is used
independently of `score`.

### Grade parsing

`_parse_grade()` handles the grade column:

| Cell content | Result |
|---|---|
| `8.00/10.00` | `("8.00", "10.00")` |
| `8.00 out of 10.00` | `("8.00", "10.00")` |
| `8.00` | `("8.00", None)` — maximum not published |
| `0.00/10.00` | `("0.00", "10.00")` — an explicit zero is a real grade |
| `80%` | `("80", None)` |
| `-`, empty, `None` | `(None, None)` — not graded |
| `8.00 (60%)` | `(None, None)` — ambiguous, deliberately not guessed |

### Quiz attempt-page grades

Moodle prints `-` in the quiz index Grade column once an attempt is closed, so
the mark must come from the attempt page. `_parse_attempt_mark()` tries scalar
forms first —

- `Marks: 8.00/10.00`
- `Raw score 8.00/10.00.` / `Score: 8.00 out of 10.00`

— and only then falls back to `_parse_attempts_table()`, which locates the
`Marked out of` column **by header name** rather than by position. Positional
matching is wrong here: the summary table also has a `Raw` column containing a
duplicate of the mark, so counting `<td>`s yields `(8.00, 8.00)`. The result only
fills a gap — a grade published in the index wins.

### Missing vs. zero

This distinction is enforced in three places:

1. `_parse_grade` returns `None` (not `0`) for `-` and empty cells.
2. `academic_sync` tests `if row.score is not None:`, never truthiness, so a
   stored `"0"` is graded and a `None` is not.
3. `get_course_attendance`'s equivalent for attendance keys off dict membership.

`grade_service.scores()` filters `Assignment.score.is_not(None)`, so an ungraded
item never appears in `/scores`.

### Grade notifications, change detection, retry, dedup, cleanup

**Delivery ordering** (`notification_service.send_score_notifications`) is
deliberate:

1. `bot.send_message(...)`.
2. On success **only**, write the `NotificationRecord`.
3. Delete the academic row.
4. Commit.

A send failure `continue`s, so nothing is recorded and the row survives for the
next sync. This is the one notification type that is genuinely retryable.

**Deduplication key** is `score_ref(external_id, score)` → `"<id>#<score>"`. A
corrected grade produces a different key and therefore its own message; an
unchanged grade collides and is never re-sent. `max_score` is deliberately
excluded, because E-Class sometimes publishes the mark before the maximum and
that must not read as a change.

**Legacy keys.** Records written before this scheme used a bare `external_id`.
`score_notification_sent()` honours both `"<id>#<score>"` and the bare
`"<id>"` as delivered. The score is not recoverable from a legacy record, so it
is treated as "this item was announced" — erring toward not re-sending a score
the student may already hold.

**First discovery with a grade.** An item seen for the first time that already
carries a score *is* queued for notification. The `new_scores` list is what
carries this: there is no NULL→value transition requirement, because a row
inserted with a score present satisfies `score is not None` immediately.

**Cleanup.** A graded row is deleted only once a notification for its current
grade exists. Nothing is cleaned up ahead of its message.

**Known at-least-once window.** If Telegram accepts a message but the process
dies before the commit, that single message may be duplicated on the next sync.
This is documented rather than papered over: losing a grade silently is worse
than a duplicated alert, and the window is one commit wide.

### Verification status

| Behaviour | Automated fixtures | Live E-Class |
|---|---|---|
| Assignment index parsing | yes | historical only |
| Quiz attempt-page mark recovery | yes | **not verified in this work** |
| `"out of"` grade format | yes | historical only |
| Explicit zero grade | yes | historical only |
| Score-change notification policy | yes | n/a (pure logic) |
| Dedup, retry, cleanup ordering | yes | n/a (pure logic) |

Telegram delivery is mocked everywhere. No test sends a real message.

---

## 7. Attendance

### Discovery

`get_course_attendance()` fetches `/course/view.php?id=<course>`, looks for any
`href` containing `attendance`, and follows the first match.

- **No attendance link** → the professor does not track attendance through
  E-Class. `available=False`, rendered as "Attendance data is not available".
- **Link present** → fetch the report page and parse it.

This is a real and common case. Not every professor records attendance in
E-Class, and the bot says so rather than reporting zeros.

### Extraction

`parse_attendance_html()` returns `(counts, records)` where `counts` maps
canonical status names to integers.

Three sources are combined:

1. **Summary rows** — `_summary_counts_from_row()` pairs a label with the
   *adjacent* cell only, handling `<td>Absent</td><td>5</td>` and the single-row
   `<td>Present</td><td>18</td><td>Late</td><td>0</td>...` layout.
2. **Inline prose** — `_inline_counts()` handles `Summary — Present: 18  Late: 1
   Absent: 5`. A label only binds to a number when the gap contains no letters
   and no other label, and numbers are consumed once so one count cannot be
   attributed to two labels.
3. **Session rows** — when no summary exists, counts are derived by tallying
   dated rows.

Supporting detail that matters:

- `_STATUS_ALIASES` maps words to canonical statuses, including `absence`,
  `unexcused`, `not present`, `justified`, and Korean terms (`결석`, `지각`,
  `출석`, `공결`). `_build_status_regex()` orders alternatives longest-first, so
  `"unexcused absence"` resolves to Absent rather than to a shorter alias.
  The word `"attendance"` is deliberately **not** an alias — it is the activity
  name, and treating it as "Present" misclassified every row that mentioned it.
- `_is_standalone_number()` rejects numbers that are part of a date or time, so
  `10:00 Present` cannot bind the clock's minutes to Present.
- Dated session rows are excluded from count extraction for the same reason.

### Three states, strictly distinguished

| State | Meaning | User sees |
|---|---|---|
| `counts` present | E-Class recorded attendance | Real numbers, including a genuine `0` |
| `counts is None` | Blank / unrecorded section | "Attendance data is not available" |
| `EClassAuthError` / `EClassUnavailableError` | Session or network failure | Propagates; the user is told to reconnect or retry |

**A status absent from the page stays absent from the dict.** It is never
fabricated as zero. When a course is tracked but publishes no absence figure, the
detail view says "Absences: not reported by E-Class for this course yet."

### Schema

`attendance` (migration `0004`) is the latest successfully-fetched snapshot per
user and course:

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `user_id` | FK → `users.id` | indexed |
| `course_external_id` | str(64) | indexed |
| `course_name` | str(512) | |
| `available` | bool NOT NULL | False ⇒ genuinely blank, counts stay NULL |
| `absences` | int NULL | NULL ⇒ not reported, **not** zero |
| `present`, `late`, `excused` | int NULL | same semantics |
| `records_json` | text NULL | JSON `[{"date":…,"status":…}]` |
| `updated_at` | timestamptz | last **successful** fetch |

`UNIQUE (user_id, course_external_id)` — `uq_attendance_user_course` — guarantees
one row per user/course, so re-syncing updates in place and can never duplicate.

### Persistence

`sync_attendance()` runs inside the same `sync_user()` transaction as the rest of
the sync, and only after `client.get_attendance()` has returned successfully. Any
failure propagates to `sync_user`, which records it in `outcome.error` and leaves
previously persisted rows untouched. `updated_at` is set to the sync timestamp
only on success.

### `/attendance`

`cmd_attendance` reads persisted rows and renders a per-course keyboard. Selecting
a course calls `format_attendance_row()`, which prints Absences, Late, Present,
and Excused (omitting any the page did not report), an optional "Recent
attendance" list from `records_json`, and a "Last updated" line rendered in the
user's timezone. A `⬅️ Back` button returns to the list.

`attendance_is_stale()` compares the newest `updated_at` against
`EClassAccount.last_sync`. If the account synced more recently than the
attendance rows, a banner explains that E-Class could not be reached and the
figures shown are the last successful ones. An inactive account always shows the
stale banner. `/attendance` therefore works without a live E-Class session — it
is a pure database read.

### Weekly report

`send_weekly_attendance_reports()` runs Saturday 21:00 in the configured timezone,
one message per user with an active E-Class account, deduplicated per ISO week via
`WEEKLY_ATTENDANCE_KIND` and the week-start date. Each course line reads
`"<course> — N absences"`, `"Attendance not available in E-Class"`, or
`"absence count not reported by E-Class"`. A stale-data footer is appended when
appropriate.

### Verifying against a real account

`live_verify_attendance.py` runs the production code path against the stored
authenticated session and prints only course-level diagnostics — never cookies,
keys, session data, or raw HTML. It performs no database writes and sends no
Telegram messages.

```
cd university_bot
python live_verify_attendance.py            # first active account
python live_verify_attendance.py <telegram_id>
```

If it reports a session error, reconnect with `/start` first.

---

## 8. EduPage timetable

### Fetching

`EduPageWebClient` (`edupage/web_client.py`) speaks EduPage's JSON API rather
than scraping HTML:

1. `GET /timetable/view.php?class=<GROUP>` → the `ASC.gsechash` token.
2. `POST /timetable/server/ttviewer.js?__func=getTTViewerData` → the default
   timetable serial for the year.
3. `POST /timetable/server/regulartt.js?__func=regularttGetData` → the timetable
   document, as a set of tables (classes, lessons, cards, periods, classrooms,
   teachers, subjects).

Anonymous access works for group timetables; only personal items require a
login, which is why `EDUPAGE_COOKIE` is optional. A notable quirk: anonymous
responses return row payloads under `data_rows` rather than `rows`, and the
client reads both.

### Group resolution and normalization

`resolve_group_name()` matches the user's input against the `classes` table and
returns **EduPage's canonical spelling**, or `None`. This is what prevents
duplicate groups from spelling variations. In `settings.got_new_group`, an
unresolvable name leaves the user in the FSM state with nothing written;
a resolved name goes through `group_service.get_or_create_group()` and triggers an
immediate timetable sync.

### Storage and synchronization

`timetables` are stored in `lessons`, keyed by `group_id`, and are **shared data**:
one timetable serves every user in that study group. Sync deletes future lessons
(`lesson_date >= today`) and reinserts the current window, so changes and
cancellations in EduPage replace local rows on the next sync.

`sync_service.sync_user()` fetches a 30-day window starting today and writes it
in the same transaction as the rest of the sync. On failure, `timetable_ok` is
False, previously stored lessons are kept, and `outcome.error` explains why.

### Cancellations

`lessons.status` is `active` or `cancelled`. Every read in
`schedule_service` filters `status == "active"`, so a cancelled lesson disappears
from `/today`, `/week`, and `/next` immediately.

### Views

`/today` and `/tomorrow` → `lessons_on(session, uid, date)`.
`/week` → `lessons_between(...)` over today..+6, grouped by date.
`/next` → `next_lesson(...)`, which selects the first active lesson whose
`end_time` is still in the future and returns it with a countdown.

All date arithmetic uses the user's own timezone, resolved from `users.timezone`
and falling back to `DEFAULT_TIMEZONE`.

### Shared vs. user-specific

| Data | Scope | Survives user reset |
|---|---|---|
| `lessons`, `groups` | shared per study group | **yes** |
| `users.group_id` | per user | no (user deleted) |
| `assignments`, `courses`, `attendance`, `eclass_accounts`, `notifications` | per user | no |

---

## 9. Database design and migrations

### Tables

```
allowed_student_ids  [shared/app-wide]
    columns: id, student_id, created_at
    indexes : ix_allowed_student_ids_student_id

groups  [shared/app-wide]
    columns: id, name, created_at
    indexes : ix_groups_name

lessons  [shared/app-wide]
    columns: id, group_id, external_id, course_name, professor, room,
             lesson_date, start_time, end_time, status, created_at
    FK      : group_id -> groups.id
    indexes : ix_lessons_group_date, ix_lessons_lesson_date,
              ix_lessons_group_id, ix_lessons_external_id

users  [user-specific]
    columns: id, telegram_id, username, timezone, group_id,
             class_notifications, deadline_notifications,
             daily_timetable_notifications, reminder_minutes, created_at
    FK      : group_id -> groups.id
    indexes : ix_users_group_id, ix_users_telegram_id

assignments  [user-specific]
    columns: id, user_id, external_id, course_name, title, kind, deadline,
             open_time, submission_status, score, max_score, url, created_at
    FK      : user_id -> users.id
    indexes : ix_assignments_user_deadline, ix_assignments_user_id,
              ix_assignments_deadline, ix_assignments_external_id

attendance  [user-specific]
    columns: id, user_id, course_external_id, course_name, available,
             absences, present, late, excused, records_json,
             updated_at, created_at
    FK      : user_id -> users.id
    UNIQUE  : uq_attendance_user_course
    indexes : ix_attendance_course_external_id, ix_attendance_user_id

courses  [user-specific]
    columns: id, user_id, external_id, name, professor, kind, created_at
    FK      : user_id -> users.id
    indexes : ix_courses_user_id, ix_courses_external_id

eclass_accounts  [user-specific]
    columns: id, user_id, username, session_data, last_sync, is_active, created_at
    FK      : user_id -> users.id
    indexes : ix_eclass_accounts_user_id

notifications  [user-specific]
    columns: id, user_id, kind, ref_id, sent_at
    FK      : user_id -> users.id
    UNIQUE  : uq_notification_once
    indexes : ix_notifications_ref_id, ix_notifications_user_id
```

### Entity relationships

```
groups (shared)
   │ 1
   │
   ├───< lessons            (shared timetable, one set per group)
   │
   └───< users              (user-specific)
              │ 1
              ├───< eclass_accounts   (encrypted session blob)
              ├───< courses
              ├───< assignments       (watchlist)
              ├───< attendance        (UNIQUE per user+course)
              └───< notifications     (UNIQUE per user+kind+ref_id)
```

### Notable constraints

- **`uq_attendance_user_course`** — one attendance snapshot per user/course.
  Re-sync updates in place; duplicates are impossible.
- **`uq_notification_once`** `(user_id, kind, ref_id)` — the backbone of all
  notification deduplication. `kind` distinguishes reminder thresholds
  (`deadline_4h` vs `deadline_1h`), which is what lets both reminders fire for
  the same item.
- **`allowed_student_ids.student_id` unique** — a whitelist cannot contain an ID
  twice.
- **`Assignment.score` is `is_not(None)`-tested, never truth-tested**, so `0`
  survives as a real grade.

### Migration history

| Revision | File | Purpose |
|---|---|---|
| `0001` | `0001_drop_encrypted_password.py` | **Dropped** `eclass_accounts.encrypted_password` |
| `0002` | `0002_daily_timetable_setting.py` | Added `users.daily_timetable_notifications` |
| `0003` | `0003_allowed_student_ids.py` | Created the whitelist table + index + seed data |
| `0004` | `0004_attendance.py` | Created `attendance` + indexes |

**Current head: `0004`.** Verified with `python -m alembic current` → `0004 (head)`.

```bash
python -m alembic current           # show applied revision
python -m alembic heads             # show head(s)
python -m alembic upgrade head      # apply
python -m alembic downgrade -1      # roll back one
```

Migration `0003` also seeds the whitelist via `op.bulk_insert`, so a fresh
database comes up with the initially authorized IDs already present.

### The user-reset procedure

`reset_users.py` is a one-off local admin script. It is **not** a Telegram
command and has no network-facing entry point.

Safety properties built into it:

- **Locality guard.** `guard_local_dev()` refuses to run unless
  `DATABASE_URL` resolves to `localhost` / `127.0.0.1` / `::1`, and also refuses
  if `sslmode` is present (a strong hint of a remote deployment).
- **Dry run by default.** Without `--execute` it prints the plan and exits.
- **Explicit allowlists.** Deletes only the six user-owned tables; preserves
  `allowed_student_ids`, `groups`, `lessons`, `alembic_version`.
- **Single transaction** in FK-safe order: children before parents
  (`notifications`, `attendance`, `assignments`, `courses`, `eclass_accounts`,
  then `users`), so no orphan can exist even momentarily.
- **Counts only.** Prints row counts, never session data or credentials.

```bash
python reset_users.py             # dry run
python reset_users.py --execute   # apply
```

Back up first:

```bash
pg_dump -h localhost -U postgres -d classmate_ai -Fc -f backups/classmate_ai_pre_reset.dump
pg_restore --list backups/classmate_ai_pre_reset.dump   # verify
```

---

## 10. Scheduler and notification schedule

`build_scheduler()` (`app/scheduler/jobs.py`) defines four jobs.

| Job | Trigger | Timezone | Target | Dedup / retry | Failure handling |
|---|---|---|---|---|---|
| `notifications` | interval, **every 1 minute** | UTC internally; per-user logic uses each user's tz | All users with an active E-Class account | Per `(user, kind, ref_id)` in `notifications`; reminders marked at collection | Per-message `try/except` logs and continues |
| `sync` | interval, **every 30 minutes** | n/a | All **active** accounts | Grade dedup via `external_id#score`; retries on failure | Per-subsystem flags on `SyncOutcome`; session expiry notifies the user |
| `weekly_attendance` | cron, **Saturday 21:00** | `DEFAULT_TIMEZONE` (Asia/Tashkent) | All users with an active E-Class account | One per user per ISO week (`weekly_attendance` + week-start date) | `try/except` logs a warning |
| deadline reminders | *inside* `notifications`, every minute | per-user | Each user's watchlist | See below | See below |

### Deadline reminders — 4 hours and 1 hour

`DEADLINE_REMINDER_HOURS = [4, 1]`. This **replaces** the previous
48h/24h/3h-style schedule; no reference to those thresholds remains in
`due_deadline_reminders`.

| Threshold | `kind` | Scheduled instant |
|---|---|---|
| 4 hours | `deadline_4h` | `deadline - 4h` |
| 1 hour | `deadline_1h` | `deadline - 1h` |

Both apply to assignments **and** quizzes.

**Independent dedup identity.** The `kind` differs per threshold, so
`uq_notification_once (user_id, 'deadline_4h', a.id)` and
`(user_id, 'deadline_1h', a.id)` are distinct rows. Delivering the 4-hour
reminder cannot suppress the 1-hour one, and both are delivered exactly once for
the same item.

**Window logic.** The scheduler polls once a minute, so an exact-instant match
would miss anything landing between ticks. Each threshold fires when:

```
scheduled <= now <= scheduled + REMINDER_GRACE + DEADLINE_REMINDER_MAX_LAG
```

with `REMINDER_GRACE = 5 minutes` (absorbs tick jitter) and
`DEADLINE_REMINDER_MAX_LAG = 10 minutes` (absorbs a late first sync). A
verification test polls every minute across a whole item's lifetime and asserts
the collected sequence is exactly `["deadline_4h", "deadline_1h"]` — proving no
repeat-per-hour behaviour.

**Late-arriving items — defined policy.** If an item first enters the system
after its 4-hour instant has already passed by more than 15 minutes, that 4-hour
reminder is **dropped as stale** rather than sent as an overdue notification. The
1-hour reminder still fires when its own instant arrives. This prevents a burst
of overdue pings after a downtime while never losing an upcoming reminder. An
item discovered at exactly 2h before its deadline therefore gets only the 1-hour
reminder.

**Restart safety.** Dedup lives in the database, not in memory. A restart re-reads
`notifications`, finds the delivered record, and skips.

**Retry behaviour — an honest caveat.** In this architecture the dedup record is
written when the reminder is *collected*, and the send is attempted afterwards
inside `check_notifications`' `try/except`. A Telegram send failure is caught and
logged, and **that reminder is not retried** — the record already exists. This is
pre-existing behaviour for all non-grade notifications, and it was preserved
rather than changed. Grade notifications are the exception: there the record is
written only *after* a confirmed send, so they are genuinely retryable (§6.6).

**Exclusions.** No reminder is produced when `deadline_notifications` is off,
the deadline has passed, the deadline is NULL, the item is submitted
(`is_submitted`), or a quiz has not opened yet.

**Message format.** `build_deadline_message()` renders
"⏰ ASSIGNMENT DEADLINE REMINDER … 🕐 4 hours remaining" for the 4-hour
threshold and the urgent "🚨 ASSIGNMENT DEADLINE IN 1 HOUR" wording for 1 hour.

### Unchanged subsystems

- **Class reminders** — `reminder_minutes` (15/30/60) before each lesson, via
  `due_class_reminders`, deduped on `str(lesson.id)`.
- **Daily timetable summary** — 1 hour before the first lesson, via
  `due_daily_timetable`, deduped on the date, with a 5-minute no-late-send rule.
- **Weekly attendance report** — Saturday 21:00, unchanged.

All three are covered by regression tests in `tests/test_deadline_reminders.py`
(`test_class_reminders_unchanged`, `test_weekly_attendance_report_unchanged`).

---

## 11. Configuration and local setup

### Requirements

- **Python 3.11+** (developed and tested on **3.13.12**; the code uses modern
  `X | None` annotations and `zoneinfo`)
- **PostgreSQL 12+** with a database and role
- A Telegram bot token from @BotFather
- Access to the university's E-Class and EduPage endpoints

### Virtual environment

```bash
cd university_bot
python -m venv .venv
```

PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

bash/zsh:

```bash
source .venv/bin/activate
```

### Dependencies

```bash
pip install -r requirements.txt
```

### Environment variables

Copy the template and fill it in:

```bash
cp .env.example .env      # PowerShell: Copy-Item .env.example .env
```

| Variable | Purpose | Secret |
|---|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather | **yes** |
| `DATABASE_URL` | SQLAlchemy async URL, `postgresql+asyncpg://…` | **yes** |
| `ENCRYPTION_KEY` | Fernet key protecting stored sessions | **yes** |
| `ECLASS_BASE_URL` | E-Class base URL | no |
| `EDUPAGE_TIMETABLE_URL` | EduPage timetable base URL | no |
| `DEFAULT_TIMEZONE` | IANA tz name; defaults to `Asia/Tashkent` | no |
| `EDUPAGE_COOKIE` | Optional cookie for personal EduPage items | **yes** |

Generate a Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`.env` is already in `.gitignore`. **Never commit it.**

### PostgreSQL

```bash
createuser -s postgres
createdb classmate_ai
```

The application calls `create_tables()` on startup, but Alembic owns the schema —
run `python -m alembic upgrade head` on a fresh database.

### Running

```bash
python -m app.main
```

Startup order in `app/main.py`: `create_tables()` → build `Bot`/`Dispatcher` →
register routers → `delete_webhook(drop_pending_updates=True)` → start scheduler
→ `start_polling`.

Shutdown is a `finally` block in reverse order: scheduler `shutdown(wait=False)`
→ FSM storage → Telegram HTTP session → `dispose_engine()`. `KeyboardInterrupt`
is caught at the top level so Ctrl+C exits without a traceback. `syslog`-style
logging goes to stdout at INFO.

### Tests

```bash
python -m pytest              # all 206 tests
python -m pytest -q           # quiet
python -m pytest tests/test_deadline_reminders.py -v
```

`pytest.ini` sets `asyncio_mode = auto`, so `async def` tests need no decorator
beyond `@pytest.mark.asyncio`. Tests use an **in-memory SQLite** database via the
`db` fixture in `conftest.py` and never touch the configured PostgreSQL instance.
Telegram delivery is always mocked.

### Re-registering after the user reset

Every registered user has been deleted, so all students must register again:

1. Start the bot and send `/start`.
2. Enter the Student ID. It must be in `allowed_student_ids` — the whitelist was
   **preserved**, so no one needs re-authorisation.
3. Enter the E-Class password once. The message is deleted immediately and the
   password is never stored.
4. The bot performs an initial sync and shows the main menu.

Because `users` was emptied, group membership (`users.group_id`) is gone too.
Students must set their study group again via `/settings` → **Change Study
group**. Timetable data itself survived, so the group is usually already known to
the bot and only the link back to the user needs re-establishing.

---

## 12. Testing and reliability

### Current result

**206 tests pass, 0 fail** (`python -m pytest`). Run on Python 3.13.12,
pytest-asyncio in auto mode.

| Test file | Tests | Focus |
|---|---|---|
| `test_auth_flow.py` | 35 | Login FSM, whitelist gate, auth/unavailable branches |
| `test_attendance.py` | 41 | Parser fixtures, client, persistence, `/attendance`, weekly report |
| `test_grade_notifications.py` | 36 | Grade extraction, notification policy, retry, dedup |
| `test_deadline_reminders.py` | 32 | 4h/1h schedule, dedup, timezone, untouched subsystems |
| `test_daily_timetable.py` | 13 | Daily summary dedup and grace window |
| `test_quiz_availability.py` | 8 | Open-time gating, reminder interplay |
| `test_academic_sync.py` | 9 | Watchlist lifecycle, cleanup after delivery |
| `test_eclass_session.py` | 5 | Session rehydration, expiry classification |
| `test_session_auth.py` | 8 | Login probes |
| `test_assignments.py` | 5 | Ordering and text helpers |
| `test_schedule_notifications.py` | 5 | Class reminders, cancelled lessons |
| `test_help.py` | 4 | `/help` contents, works unauthenticated |
| `test_eclass_parser.py` | 3 | Regex-level parsing of sample markup |
| `test_grades.py` | 2 | Average computation |
| **Total** | **206** | |

### What is covered

**Unit tests with fixtures.** Parser behaviour is driven by representative
Moodle `mod_assign`, `mod_quiz`, and `mod_attendance` markup. `httpx.MockTransport`
serves these to the real client code, so `get_assignments()`, `get_quizzes()`, and
`get_attendance()` are exercised end to end — only the socket is absent. This is
how the missing-quiz-grade defect was reproduced and fixed: the pre-fix code
returned `score=None` while the attempt page clearly published `8.00/10.00`.

**Database behaviour.** Tests run against real SQLAlchemy models on in-memory
SQLite, exercising the actual unique constraints, FK relationships, and query
logic. SQLite differs from PostgreSQL in some areas, so constraint-violation
behaviour is not perfectly representative.

**Telegram delivery.** Always mocked with a `FakeBot`. No test sends a real
message, and none requires a bot token.

**Scheduler jobs.** `send_weekly_attendance_reports` and the reminder collectors
are invoked directly with a patched session factory. The 4h/1h window logic is
verified by polling minute-by-minute across a full item lifetime.

### What is NOT verified

- **No live E-Class verification.** Every stored session in the local database
  was expired at the time of this work — all requests redirected to the login
  page — and passwords are never stored, so re-authentication was not possible.
  The parsers are based on previously observed markup plus realistic fixtures.
  The quiz attempt-page grade extraction in particular has **not** been validated
  against a live response.
- **No live Telegram delivery.** Delivery is mocked throughout.
- **No live EduPage verification** beyond the previously observed JSON shapes.
- **No load or concurrency testing.** Long-running behaviour, connection-pool
  exhaustion, and multi-process safety are unverified.
- **No end-to-end test** spanning a real sync, a real database, and a real
  Telegram send.

Passing tests therefore establish that the logic is correct given the fixtures
provided. They do not establish that the fixtures match the live services today.

---

## 13. Deployment readiness

### Persistent PostgreSQL

Use a real PostgreSQL instance, not SQLite. The app relies on `asyncpg`,
timezone-aware timestamps, and row-level constraints that SQLite does not enforce
identically. Persist the data directory and back it up regularly.

### Environment variables and key persistence

`ENCRYPTION_KEY` is the critical one. It must be **stable for the lifetime of every
stored session**: Fernet cannot decrypt under a rotated key, and because
passwords are never stored, a lost key means every student must reconnect
individually. Back it up with the same care as the database, separately from it.
`BOT_TOKEN` and `DATABASE_URL` are likewise required at every start.

### Migrations

Run `python -m alembic upgrade head` on deploy, before starting the bot. Never
use `create_tables()` as a substitute — it only creates missing tables and will
not alter existing ones.

### Process management and graceful shutdown

Run under systemd, Docker with an init, or a supervisor. Send `SIGTERM` so the
`finally` block runs and the connection pool closes cleanly; a `SIGKILL` leaves
pooled connections to be reaped by PostgreSQL and skips scheduler shutdown.

### Exactly one active instance

This is the most important operational constraint. The bot uses **long polling**,
not webhooks (`delete_webhook(drop_pending_updates=True)` then `start_polling`).
Two instances means:

- Telegram rejects the second `getUpdates`, or the two interleave updates.
- Both schedulers fire, and every notification is duplicated — the
  `notifications` unique constraint prevents *duplicate rows* but cannot prevent
  duplicate *messages*, because each process marks and sends independently.
- Concurrent `sync_all` runs race on the same rows.

Run a single replica unless leader election or a scheduler lock is implemented.
Neither exists today.

### Logging, backups, session expiry, secrets

- **Logging** is stdlib logging at INFO via `basicConfig`; scheduler jobs use
  `log.warning` / `log.exception` with the account id only. Never log session
  data, tokens, or passwords — no current code path does.
- **Backups.** `pg_dump -Fc` on a schedule, plus off-site copies. Restore with
  `pg_restore`. Back up `ENCRYPTION_KEY` separately and securely; a database
  backup without its key is useless for restoring sessions.
- **Session expiry** is expected, not exceptional. The 30-minute sync doubles as
  keepalive; on genuine expiry the account is deactivated and the student is
  prompted to reconnect. With a large user base this will be a recurring support
  load — there is no automated recovery because no password is stored.
- **Secrets.** Keep `.env` out of version control and out of images. Prefer
  environment injection over a file in production.

### Unresolved limitations

1. Single-instance only; no leader election.
2. No retry queue for non-grade notifications; a transient Telegram failure drops
   that reminder.
3. Reminder dedup records are written before the send, so a crash between mark and
   send silently loses one reminder.
4. No live verification of the E-Class parsers against the current production
   markup.
5. Session expiry has no automated recovery path.
6. EduPage parsing depends on a third-party JSON API's undocumented shape
   (`ASC.gsechash`, `data_rows`), which could change without notice.
7. Attendance parsing is heuristic. It handles several Moodle layouts and falls
   back to "not reported" rather than guessing, but an unusual page shape will
   yield "unavailable" rather than wrong numbers — correct, but not useful.
8. SQLite-based tests do not perfectly mirror PostgreSQL constraint behaviour.
9. No observability beyond stdout logs — no metrics, tracing, or alerting.

---

## 14. Current state and next steps

### Verified by tests

All 206 tests pass. Confirmed by automated test coverage:

- Student ID validation, normalization, and the whitelist gate.
- E-Class login probes, session rehydration, and expiry classification.
- Assignment and quiz index parsing, including the `"out of"` grade format.
- Quiz attempt-page mark recovery, including the header-based column lookup.
- Explicit-zero vs. missing-grade distinction throughout.
- Grade notification: first discovery, corrections, unchanged-score suppression,
  retry after failure, no premature cleanup.
- Attendance: summary/row/inline extraction, blank vs. unreported vs. error,
  in-place update, `/attendance` display, weekly report.
- Deadline reminders at 4h and 1h for both assignments and quizzes, with
  independent dedup, minute-by-minute poll verification, and stale-drop policy.
- Class reminders, daily timetable, and the weekly attendance report are
  unaffected by the deadline change.

### Verified against live services

Nothing in this work. All E-Class sessions in the local database were expired, and
the bot's Telegram delivery path was exercised only through mocks. The most
valuable live confirmation would be a single successful `/sync` followed by a
real quiz-grade notification.

### Still unverified

- Quiz attempt-page grade extraction against a live response (§6.7).
- Every E-Class parser against current production markup.
- Telegram delivery, end to end.
- EduPage timetable parsing against a live response.
- Behaviour under real concurrency or connection-pool pressure.

### Current migration head

**`0004`** — confirmed with `python -m alembic current`.

### New notification schedule

| Notification | Schedule |
|---|---|
| Deadline reminders (assignments + quizzes) | **4 hours before**, and **1 hour before**, each once |
| Class reminders | `reminder_minutes` before each lesson (default 15) |
| Daily timetable summary | 1 hour before the first lesson of the day |
| E-Class sync | every 30 minutes |
| Weekly attendance report | Saturday 21:00, `DEFAULT_TIMEZONE` |

### User reset outcome

All registered users and their user-owned data were removed after a verified
`pg_dump` backup. The `allowed_student_ids` whitelist, the shared `groups` and
`lessons` timetable data, and the Alembic history are intact. Details in §9.

### Recommended next steps

Before deployment:

1. **Reconnect one account and run a live `/sync`.** Confirm quiz grades and
   attendance parse correctly against production markup. This is the single
   highest-value action remaining, and `live_verify_attendance.py` covers the
   attendance half without touching stored data.
2. **Confirm the 4h/1h reminders end to end** by temporarily creating an
   assignment with a deadline 4 hours out and watching for exactly two pushes.
3. **Back up `ENCRYPTION_KEY`** separately from the database and store it in a
   secret manager. Document the recovery implication if it is ever lost.
4. **Verify graceful shutdown** under the real supervisor: send `SIGTERM` and
   confirm logs show `Resources released.` and no orphaned connections.
5. **Confirm exactly one instance** is running before enabling the scheduler.

Worth doing afterwards:

6. Implement a real retry queue so a transient Telegram failure does not lose a
   deadline reminder.
7. Add leader election or a scheduler lock so the bot can be scaled or restarted
   without duplicate notifications.
8. Add structured logging with redaction, and basic alerting on repeated sync
   failures per account.
9. Consider a session-expiry dashboard so reconnect requests are visible rather
   than arriving piecemeal through Telegram.