"""Menu interactif (``myges`` seul) : sections, puis l'application pilotée par Textual."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import fixture
from myges import models as m
from myges.auth import AUTH_URL
from myges.cli import app as cli_app
from myges.client import API_URL
from myges.tui import sections as sec
from myges.tui.app import LoginScreen, MainScreen, MygesApp, SimulateModal

REDIRECT = (
    "comreseaugesskolae:/oauth2redirect"
    "#access_token=tok-2&token_type=bearer&expires_in=560063&scope=account"
)


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _agenda_around_now() -> dict[str, Any]:
    """Fixture d'agenda recalée : un cours en cours, un plus tard, un le lendemain."""
    data = copy.deepcopy(fixture("getAgenda"))
    now = datetime.now(timezone.utc)
    starts = [now - timedelta(minutes=30), now + timedelta(hours=2), now + timedelta(days=1)]
    for event, start in zip(data["result"], starts, strict=True):
        length = event["end_date"] - event["start_date"]
        event["start_date"] = _ms(start)
        event["end_date"] = _ms(start) + length
    return data


def _mock_api(router: respx.MockRouter) -> None:
    def get(path: str, name: str | None, data: Any = None) -> None:
        response = (
            httpx.Response(200, json=data if data is not None else fixture(name or ""))
            if name or data is not None
            else httpx.Response(204)
        )
        router.get(f"{API_URL}{path}").mock(return_value=response)

    get("/me/profile", "getProfile")
    get("/me/years", "getYears")
    get("/me/2026/courses", "getCourses")
    get("/me/2026/grades", "getGrades")
    get("/me/2026/classes", "getClasses")
    get("/me/2026/absences", "getAbsences")
    get("/me/2026/teachers", "getTeachers")
    get("/me/2026/projects", "getProjects")
    get("/me/2026/practicals", "getPracticals")
    get("/me/agenda", None, _agenda_around_now())
    get("/me/nextProjectSteps", "getNextProjectSteps")
    get("/me/news", "getNews")
    get("/me/events", "getEvents")
    router.get(url__regex=rf"{API_URL}/me/\d+/syllabus").mock(
        return_value=httpx.Response(200, json=fixture("getSyllabus"))
    )


# ---------------------------------------------------------------- Sections


def _grades_data() -> sec.GradesData:
    courses = [m.Course.model_validate(c) for c in fixture("getCourses")["result"]]
    grades = [m.Grade.model_validate(g) for g in fixture("getGrades")["result"]]
    return sec.GradesData(2026, courses, grades, {})


def test_grades_view_and_simulation() -> None:
    data, state = _grades_data(), sec.State()
    view = sec.render_grades(data, state)
    assert view.columns[:2] == ["#", "Matière"]
    assert [row[1] for row in view.rows] == sorted(c.name for c in data.courses)
    before = str(view.summary)

    course = view.items[0]
    state.simulated[course.rc_id] = 20.0
    after = sec.render_grades(data, state)
    assert str(after.summary) != before
    assert "1 note(s) simulée(s)" in str(after.summary)
    assert str(after.rows[0][4]).endswith("*")


@pytest.mark.parametrize(
    ("raw", "expected"), [("14", 14.0), ("12,5", 12.5), (" ", None), ("0", 0.0), ("20", 20.0)]
)
def test_parse_grade(raw: str, expected: float | None) -> None:
    assert sec.parse_grade(raw) == expected


@pytest.mark.parametrize("raw", ["21", "-1", "abc"])
def test_parse_grade_rejects(raw: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        sec.parse_grade(raw)


def test_week_bounds_follow_offset() -> None:
    state = sec.State()
    first, last = sec.week_bounds(state)
    assert first.weekday() == 0
    assert (last - first).days == 7
    state.week = -2
    assert sec.week_bounds(state)[0] == first - timedelta(weeks=2)


def test_next_class_line() -> None:
    now = datetime.now(timezone.utc)
    events = [m.AgendaEvent.model_validate(e) for e in _agenda_around_now()["result"]]
    assert "En cours" in str(sec.next_class_line(events[0], now))
    line = str(sec.next_class_line(events[1], now))
    assert "Prochain cours dans 1 h 59" in line or "Prochain cours dans 2 h" in line
    assert "Aucun cours" in str(sec.next_class_line(None, now))


def test_today_without_classes_shows_the_next_one() -> None:
    now = datetime.now(timezone.utc)
    upcoming = m.AgendaEvent.model_validate(_agenda_around_now()["result"][2])
    view = sec.render_today(([], upcoming), sec.State())
    assert view.rows == []
    assert "Prochain cours" in str(view.summary)
    assert upcoming.start_date > now


def test_fit_widths_shrinks_long_text_columns_first() -> None:
    # « Matière » (60) puis « Nom » (40) se réduisent ; les dates (16) restent lisibles.
    assert sec.fit_widths([60, 40, 16], (0, 1), 100, 2) == [39, 39, 16]
    assert sum(w + 2 for w in sec.fit_widths([60, 40, 16], (0, 1), 70, 2)) == 70
    assert sec.fit_widths([10, 12], (), 200, 2) == [10, 12]  # assez de place
    assert sec.fit_widths([30, 30], (0,), 10, 2) == [sec.MIN_FLEX, 30]  # plancher


def test_cells_are_never_markup() -> None:
    from myges.tui.app import _cell

    cell = _cell("[Info] Fermeture du campus [/b]")
    assert cell.plain == "[Info] Fermeture du campus [/b]"
    assert cell.overflow == "ellipsis"


# ---------------------------------------------------------------- Application


async def _settle(app: MygesApp, pilot: Any) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.anyio
async def test_menu_navigates_and_simulates(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_api(router)
        app = MygesApp()
        async with app.run_test(size=(130, 40)) as pilot:
            await _settle(app, pilot)
            main = app.main
            assert isinstance(app.screen, MainScreen)
            assert main.header is not None
            assert main.header.name == "Camille EXEMPLE"
            assert main.current is not None
            assert main.current.key == "today"
            assert len(main._items) == 3

            await pilot.press("down", "down")  # Semaine, puis Notes
            await _settle(app, pilot)
            assert main.current.key == "grades"
            await pilot.press("enter", "enter")  # tableau, puis simulation
            await _settle(app, pilot)
            assert isinstance(app.screen, SimulateModal)
            await pilot.press("1", "8", "enter")
            await _settle(app, pilot)
            assert isinstance(app.screen, MainScreen)
            course = main._items[0]
            assert app.state.simulated == {course.rc_id: 18.0}

            await pilot.press("x")  # efface les simulations
            assert app.state.simulated == {}

            await pilot.press("left", "up")  # retour au menu, Semaine
            await _settle(app, pilot)
            assert main.current.key == "week"
            await pilot.press("right_square_bracket")
            await _settle(app, pilot)
            assert app.state.week == 1
            assert "week:1" in app.cache


@pytest.mark.anyio
async def test_menu_keys_and_theme(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_api(router)
        app = MygesApp()
        async with app.run_test(size=(130, 40)) as pilot:
            await _settle(app, pilot)
            main = app.main
            await pilot.press("j", "j", "k")  # Semaine
            await _settle(app, pilot)
            assert main.current is not None
            assert main.current.key == "week"
            await pilot.press("right")
            assert app.focused is not None
            assert app.focused.id == "table"
            await pilot.press("escape")
            assert app.focused is not None
            assert app.focused.id == "menu"

            app.theme = "myges-clair"
            await pilot.pause()
    config = (home / "xdg_config_home" / "myges" / "config.json").read_text()
    assert '"tui_theme": "myges-clair"' in config


@pytest.mark.anyio
async def test_menu_asks_to_log_in(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYGES_USERNAME")
    with respx.mock(assert_all_called=False) as router:
        _mock_api(router)
        auth = router.get(AUTH_URL).mock(
            return_value=httpx.Response(302, headers={"Location": REDIRECT})
        )
        app = MygesApp()
        async with app.run_test(size=(130, 40)) as pilot:
            await _settle(app, pilot)
            assert isinstance(app.screen, LoginScreen)
            await pilot.click("#username")
            await pilot.press(*"jdupont", "enter", *"secret", "enter")
            await _settle(app, pilot)
            assert auth.called
            assert isinstance(app.screen, MainScreen)
            assert app.main.header is not None
    config = (home / "xdg_config_home" / "myges" / "config.json").read_text()
    assert '"username": "jdupont"' in config
    assert "secret" not in config  # mot de passe non retenu sans la case cochée


@pytest.mark.anyio
async def test_menu_shows_network_errors(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_api(router)
        router.get(f"{API_URL}/me/2026/absences").mock(side_effect=httpx.ConnectError("x"))
        app = MygesApp()
        async with app.run_test(size=(130, 40)) as pilot:
            await _settle(app, pilot)
            await pilot.press("down", "down", "down")  # Absences
            await _settle(app, pilot)
            assert app.main.current is not None
            assert app.main.current.key == "absences"
            assert "Pas de connexion" in app.main._errors["absences"]


def test_bare_myges_without_terminal_prints_help() -> None:
    # CliRunner n'est pas un terminal : l'aide, comme avant, et non le menu.
    result = CliRunner().invoke(cli_app, [])
    assert result.exit_code == 2
    assert "Usage" in result.output
    assert "menu" in result.output


def test_menu_command_needs_a_terminal() -> None:
    result = CliRunner().invoke(cli_app, ["menu"])
    assert result.exit_code == 2
