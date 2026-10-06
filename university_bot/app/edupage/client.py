from app.eclass.models import Lesson


class EduPageError(Exception):
    pass


class EduPageClient:
    async def get_timetable(self, group: str, start, end) -> list[Lesson]:
        raise NotImplementedError
