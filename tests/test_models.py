"""Les modèles couvrent exactement les champs de la spec."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from conftest import fixture
from myges import models as m

SINGLE: dict[str, type[BaseModel]] = {
    "getProfile": m.Profile,
    "getCourse": m.CourseDetail,
    "getTeacher": m.PersonDetail,
    "getStudent": m.PersonDetail,
    "getClass": m.SchoolClassDetail,
    "getProject": m.Project,
    "getNewsItem": m.NewsDetail,
    "getNews": m.Page[m.News],
    "getBanners": m.Page[m.Banner],
}
LISTS: dict[str, type[BaseModel]] = {
    "getAgenda": m.AgendaEvent,
    "getCourses": m.Course,
    "getGrades": m.Grade,
    "getAbsences": m.Absence,
    "getTeachers": m.Person,
    "getClasses": m.SchoolClass,
    "getClassStudents": m.Person,
    "getProjects": m.Project,
    "getNextProjectSteps": m.NextProjectStep,
    "getEvents": m.CampusEvent,
    "getPartners": m.Partner,
    "getCourseFiles": m.CourseFile,
    "getSyllabus": m.Syllabus,
    "getPracticals": m.Project,
    "getSchools": m.School,
    "getCampuses": m.Campus,
}


def extra_fields(value: Any, path: str = "$") -> list[str]:
    """Chemins des champs reçus mais non déclarés dans un modèle."""
    found: list[str] = []
    if isinstance(value, BaseModel):
        found += [f"{path}.{key}" for key in value.model_extra or {}]
        for name in type(value).model_fields:
            found += extra_fields(getattr(value, name), f"{path}.{name}")
    elif isinstance(value, list):
        for item in value:
            found += extra_fields(item, f"{path}[]")
    elif isinstance(value, dict):
        for key, item in value.items():
            found += extra_fields(item, f"{path}.{key}")
    return sorted(set(found))


@pytest.mark.parametrize("operation_id", sorted(SINGLE))
def test_single_resource_fully_modelled(operation_id: str) -> None:
    parsed = SINGLE[operation_id].model_validate(fixture(operation_id)["result"])
    assert extra_fields(parsed) == []


@pytest.mark.parametrize("operation_id", sorted(LISTS))
def test_collection_fully_modelled(operation_id: str) -> None:
    items = TypeAdapter(list[LISTS[operation_id]]).validate_python(  # type: ignore[valid-type]
        fixture(operation_id)["result"]
    )
    assert items
    assert extra_fields(items) == []


def test_unknown_fields_are_kept() -> None:
    raw = fixture("getCourses")["result"][0] | {"nouveau_champ": 1}
    course = m.Course.model_validate(raw)
    assert course.model_extra == {"nouveau_champ": 1}


def test_timestamps_become_utc_datetimes_and_round_trip() -> None:
    raw = fixture("getAgenda")["result"][0]
    event = m.AgendaEvent.model_validate(raw)
    assert event.start_date == datetime.fromtimestamp(raw["start_date"] / 1000, tz=timezone.utc)
    assert event.model_dump(mode="json")["start_date"] == raw["start_date"]


def test_agenda_location() -> None:
    first, remote, _ = (m.AgendaEvent.model_validate(e) for e in fixture("getAgenda")["result"])
    assert first.location == "B12 (CAMPUS1)"
    assert remote.location == ""


def test_grade_decimal_strings() -> None:
    graded, ungraded = (m.Grade.model_validate(g) for g in fixture("getGrades")["result"])
    assert (graded.coef_value, graded.ects_value) == (3.0, 4.0)
    assert (ungraded.coef_value, ungraded.ects_value) == (None, None)


def test_links_helpers() -> None:
    teacher = m.Person.model_validate(fixture("getTeachers")["result"][0])
    assert teacher.photo_url is not None
    assert teacher.photo_url.startswith("https://ges-dl.kordis.fr/")
    detail = m.PersonDetail.model_validate(fixture("getTeacher")["result"])
    assert detail.photo_url == teacher.photo_url
    assert detail.model_dump(by_alias=True)["_links"]["self"]["href"].endswith("/1000000001")
    profile = m.Profile.model_validate(fixture("getProfile")["result"])
    assert profile.href("agenda") is not None


def test_validation_errors_never_echo_personal_data() -> None:
    from pydantic import ValidationError

    from myges.client import _parse_list

    raw = fixture("getProfile")["result"] | {"uid": "pas-un-entier-SECRET"}
    with pytest.raises(ValidationError) as single:
        m.Profile.model_validate(raw)
    assert "SECRET" not in str(single.value)
    with pytest.raises(ValidationError) as listed:
        _parse_list(m.Course, "SECRET-pas-une-liste")
    assert "SECRET" not in str(listed.value)
