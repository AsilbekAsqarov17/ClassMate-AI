from app.services.grade_service import overall_average
from app.database.models.assignment import Assignment


def _a(score, max_score):
    return Assignment(user_id=1, external_id=score, course_name="C", title="T", score=score, max_score=max_score)


def test_overall_average():
    items = [_a("18", "20"), _a("9", "10")]
    assert overall_average(items) == 90.0


def test_overall_average_empty():
    assert overall_average([]) is None
