"""Blocs de compétences : moyennes pondérées et simulation."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import fixture
from myges import models as m
from myges.blocs import compute, overall
from myges.cli import app
from myges.client import API_URL

# Fixtures : 500001 « T1 - Réseaux avancés » coef 3, moyenne 15.25 ;
#            500002 « T1 - Anglais, niveau C1 » coef 1, pas encore de note.


def data() -> tuple[list[m.Course], list[m.Grade]]:
    courses = [m.Course.model_validate(c) for c in fixture("getCourses")["result"]]
    grades = [m.Grade.model_validate(g) for g in fixture("getGrades")["result"]]
    return courses, grades


def test_bloc_average_uses_coefficients_and_ignores_ungraded() -> None:
    courses, grades = data()
    results, others = compute(courses, grades, {"Bloc 1": [500001, 500002]})
    assert results[0].average == pytest.approx(15.25)  # l'anglais sans note n'est pas compté
    assert results[0].validated is True
    assert others.lines == []


def test_simulation_changes_the_average() -> None:
    courses, grades = data()
    results, _ = compute(courses, grades, {"Bloc 1": [500001, 500002]}, {500002: 7.0})
    # (3 × 15.25 + 1 × 7) / 4
    assert results[0].average == pytest.approx(13.1875)
    assert [line.source for line in results[0].lines] == ["officielle", "simulée"]


def test_unpublished_coefficient_counts_as_one() -> None:
    courses, grades = data()
    courses = [c.model_copy(update={"coef": 0.0}) for c in courses]
    grades = [g.model_copy(update={"coef": "N.C."}) for g in grades]
    results, _ = compute(courses, grades, {"B": [500001, 500002]}, {500002: 7.0})
    assert results[0].average == pytest.approx((15.25 + 7.0) / 2)
    assert not results[0].lines[0].official_weight


def test_provisional_average_from_continuous_assessment() -> None:
    courses, grades = data()
    grades = [g.model_copy(update={"average": None}) for g in grades]
    results, _ = compute(courses, grades, {"B": [500001]})
    assert results[0].lines[0].source == "provisoire"
    assert results[0].average == pytest.approx(15.75)  # ccaverage


def test_overall_counts_each_course_once() -> None:
    courses, grades = data()
    results, others = compute(courses, grades, {"A": [500001]}, {500002: 9.0})
    assert overall(results, others) == pytest.approx((3 * 15.25 + 9.0) / 4)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MYGES_TOKEN", "tok")
    monkeypatch.delenv("MYGES_USERNAME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def mock(router: respx.MockRouter) -> None:
    router.get(f"{API_URL}/me/years").mock(
        return_value=httpx.Response(200, json=fixture("getYears"))
    )
    router.get(f"{API_URL}/me/2026/courses").mock(
        return_value=httpx.Response(200, json=fixture("getCourses"))
    )
    router.get(f"{API_URL}/me/2026/grades").mock(
        return_value=httpx.Response(200, json=fixture("getGrades"))
    )


def test_cli_blocs_workflow(cli_env: Path) -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as router:
        mock(router)
        empty = runner.invoke(app, ["blocs"])
        assert empty.exit_code == 0, empty.output
        assert "Aucun bloc défini" in empty.output

        added = runner.invoke(app, ["blocs", "add", "Bloc 1", "reseaux", "1"])
        assert added.exit_code == 0, added.output
        stored = json.loads((cli_env / "myges" / "config.json").read_text())
        assert stored["blocs"] == {"2026": {"Bloc 1": [500001, 500002]}}

        simulated = runner.invoke(app, ["blocs", "-s", "1=7", "-s", "reseaux=12,5"])
        assert simulated.exit_code == 0, simulated.output
        assert "Bloc 1 : moyenne 11.12" in simulated.output  # (3 × 12.5 + 7) / 4
        assert "7.00 *" in simulated.output

        moved = runner.invoke(app, ["blocs", "add", "Bloc 2", "1"])  # 1 seul bloc par matière
        assert moved.exit_code == 0
        stored = json.loads((cli_env / "myges" / "config.json").read_text())
        assert stored["blocs"]["2026"] == {"Bloc 1": [500001], "Bloc 2": [500002]}

        assert runner.invoke(app, ["blocs", "remove", "Bloc 2"]).exit_code == 0
        stored = json.loads((cli_env / "myges" / "config.json").read_text())
        assert stored["blocs"]["2026"] == {"Bloc 1": [500001]}


@pytest.mark.parametrize("raw", ["3", "1=25", "1=abc", "=12"])
def test_cli_rejects_bad_simulation(cli_env: Path, raw: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        mock(router)
        result = CliRunner().invoke(app, ["blocs", "-s", raw])
    assert result.exit_code == 2
