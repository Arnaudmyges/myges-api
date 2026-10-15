"""Contrat contre la vraie API. Lancer avec : pytest -m live

Nécessite MYGES_USERNAME + MYGES_PASSWORD (ou MYGES_TOKEN). Vérifie que chaque
réponse réelle respecte la spec et ne contient aucun champ inconnu des modèles.
Les rapports ne contiennent que des chemins JSON, jamais de valeurs.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import TypeAdapter

from contract import schema_errors
from myges import MyGES
from myges.client import to_millis
from test_models import LISTS, SINGLE, extra_fields

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("MYGES_TOKEN") or os.environ.get("MYGES_USERNAME")),
        reason="identifiants myGES absents",
    ),
]


@pytest.fixture(scope="module")
def ges() -> Iterator[MyGES]:
    with MyGES.from_env() as client:
        yield client


@pytest.fixture(scope="module")
def ids(ges: MyGES) -> dict[str, Any]:
    year = max(ges.years())
    courses = ges.courses(year)
    classes = ges.classes(year)
    projects = ges.projects(year)
    news = ges.news(size=1).content
    return {
        "year": year,
        "rcId": courses[0].rc_id if courses else None,
        "teacherId": courses[0].teacher_id if courses else None,
        "classId": classes[0].puid if classes else None,
        "projectId": projects[0].project_id if projects else None,
        "newsId": news[0].ne_id if news else None,
        "docsRcId": next((c.rc_id for c in courses if c.has_documents), None),
        "syllabusRcId": next((c.rc_id for c in courses if c.syllabus_id), None),
    }


def calls(ids: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any] | None]]:
    today = date.today()
    start, end = to_millis(today), to_millis(today + timedelta(days=14))
    y = ids["year"]
    return {
        "getProfile": ("/me/profile", None),
        "getYears": ("/me/years", None),
        "getAgenda": ("/me/agenda", {"start": start, "end": end}),
        "getCourses": (f"/me/{y}/courses", None),
        "getCourse": (f"/me/courses/{ids['rcId']}", None),
        "getGrades": (f"/me/{y}/grades", None),
        "getAbsences": (f"/me/{y - 1}/absences", None),
        "getSyllabus": (f"/me/{ids['syllabusRcId']}/syllabus", None),
        "getCourseFiles": (f"/me/{ids['docsRcId']}/files", None),
        "getPracticals": (f"/me/{y - 1}/practicals", None),
        "getSchools": ("/me/schools", None),
        "getCampuses": ("/me/campus", None),
        "getTeachers": (f"/me/{y}/teachers", None),
        "getTeacher": (f"/me/teachers/{ids['teacherId']}", None),
        "getClasses": (f"/me/{y}/classes", None),
        "getClass": (f"/me/classes/{ids['classId']}", None),
        "getClassStudents": (f"/me/classes/{ids['classId']}/students", None),
        "getProjects": (f"/me/{y}/projects", None),
        "getProject": (f"/me/projects/{ids['projectId']}", None),
        "getNextProjectSteps": ("/me/nextProjectSteps", None),
        "getNews": ("/me/news", {"size": 5}),
        "getNewsItem": (f"/me/news/{ids['newsId']}", None),
        "getBanners": ("/me/news/banners", None),
        "getEvents": ("/me/events", None),
        "getPartners": ("/me/partners", None),
    }


OPS = sorted(
    calls(
        {
            "year": 0,
            **dict.fromkeys(
                ["rcId", "teacherId", "classId", "projectId", "newsId", "docsRcId", "syllabusRcId"]
            ),
        }
    )
)


@pytest.mark.parametrize("operation_id", OPS)
def test_live_contract(ges: MyGES, ids: dict[str, Any], operation_id: str) -> None:
    path, params = calls(ids)[operation_id]
    if "None" in path:
        pytest.skip("aucun identifiant disponible sur ce compte")
    response = ges._http.get(
        f"{ges.base_url}{path}", params=params, headers=ges._headers(ges.get_token())
    )
    if response.status_code == 204:
        pytest.skip("204 : aucun élément à valider sur ce compte")
    assert response.status_code == 200
    body = response.json()
    assert schema_errors(operation_id, 200, body) == []

    result = body["result"]
    if operation_id in SINGLE:
        parsed: Any = SINGLE[operation_id].model_validate(result)
    elif operation_id in LISTS:
        parsed = TypeAdapter(list[LISTS[operation_id]]).validate_python(result)  # type: ignore[valid-type]
    else:
        return
    assert extra_fields(parsed) == [], "champs renvoyés par l'API absents de la spec"
