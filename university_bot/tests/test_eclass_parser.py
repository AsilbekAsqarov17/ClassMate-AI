import re

import pytest

from app.eclass.web_client import EClassWebClient, _parse_mdy

SAMPLE_COURSES = """
<tr><td>1</td><td><div><span class="label label-course">OFFLINE</span> <a href="https://eclass.inha.ac.kr/course/view.php?id=2528" class="coursefullname">Electronic Circuit[202602-ICE2080-001]</a></div></td><td class="text-center">An Chongkoo</td><td>29</td><td>Student</td></tr>
"""

SAMPLE_ASSIGN = """
<tr><td>2</td><td><a href="https://eclass.inha.ac.kr/mod/assign/view.php?id=71055">Homework 01 </a></td>
<td class="cell c2">2026-09-25 23:55</td>
<td class="cell c3">No submission</td>
<td class="cell c4 lastcol">-</td></tr>
"""


def test_parse_mdy():
    assert _parse_mdy("2026-09-25 23:55") is not None
    assert _parse_mdy("-") is None
    assert _parse_mdy("") is None


def test_course_regex():
    m = re.search(
        r'course/view\.php\?id=(\d+)"[^>]*class="coursefullname">([^<]+)</a>.*?<td class="text-center">([^<]+)</td>',
        SAMPLE_COURSES, re.S,
    )
    assert m and m.group(1) == "2528" and "Electronic Circuit" in m.group(2)


def test_assign_regex():
    m = re.search(
        r'mod/assign/view\.php\?id=(\d+)">([^<]+)</a></td>\s*'
        r'<td class="cell c2"[^>]*>([^<]*)</td>\s*'
        r'<td class="cell c3"[^>]*>([^<]*)</td>\s*'
        r'<td class="cell c4[^>]*>([^<]*)</td>',
        SAMPLE_ASSIGN,
    )
    assert m and m.group(1) == "71055" and "Homework 01" in m.group(2)
