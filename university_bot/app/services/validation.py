"""Input validation helpers (Student ID / E-Class username)."""

import re

# Exactly 8 chars: 'u2' ('u' case-insensitive) + 6 digits.
STUDENT_ID_RE = re.compile(r"^[uU]2\d{6}$")

INVALID_STUDENT_ID_MSG = (
    "❌ Invalid Student ID.\n\n"
    "Student ID must be 8 characters:\n"
    "u2 followed by 6 digits.\n\n"
    "Example:\nu2410037"
)

NOT_ALLOWED_STUDENT_ID_MSG = (
    "This Student ID is not registered for ClassMate AI.\n"
    "Please ask Asqarbek to add your Student ID."
)


def normalize_student_id(text: str | None) -> str | None:
    """Return the normalized (lowercase) Student ID, or None if invalid.

    Valid: u2410037, U2410037, u2123456 ...
    Invalid: u241037, u24100378, u3410037, x2410037, u2abcdef, u2-410037, 2410037
    """
    if text is None:
        return None
    value = text.strip()
    if STUDENT_ID_RE.match(value):
        return value.lower()
    return None
