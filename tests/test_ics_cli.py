from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import fixture
from myges import models as m
from myges import to_ics
from myges.auth import AUTH_URL
from myges.cli import _default_download_dir, app, main
from myges.client import API_URL


def mock_courses(api: respx.MockRouter) -> None:
    api.get("/me/years").mock(return_value=httpx.Response(200, json=fixture("getYears")))
    api.get("/me/2026/courses").mock(return_value=httpx.Response(200, json=fixture("getCourses")))


def events() -> list[m.AgendaEvent]:
    return [m.AgendaEvent.model_validate(e) for e in fixture("getAgenda")["result"]]


def test_ics_structure_and_escaping() -> None:
    ics = to_ics(events())
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.endswith("END:VCALENDAR\r\n")
    assert ics.count("BEGIN:VEVENT") == 3
    assert "UID:3000001@myges" in ics
    assert "DTSTART:20261019T080000Z" in ics
    assert "SUMMARY:T1 - Anglais\\, niveau C1" in ics
    assert "LOCATION:B12 (CAMPUS1)" in ics


def test_ics_lines_are_folded_at_75_octets() -> None:
    event = events()[0].model_copy(update={"name": "é" * 120})
    for line in to_ics([event]).split("\r\n"):
        assert len(line.encode()) <= 75


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYGES_TOKEN", "tok-cli")
    monkeypatch.delenv("MYGES_USERNAME", raising=False)
    monkeypatch.delenv("MYGES_PASSWORD", raising=False)


@pytest.mark.usefixtures("env")
def test_cli_agenda_ics(tmp_path: Path) -> None:
    out = tmp_path / "cours.ics"
    with respx.mock(base_url=API_URL) as api:
        api.get("/me/agenda").mock(return_value=httpx.Response(200, json=fixture("getAgenda")))
        result = CliRunner().invoke(app, ["agenda", "--from", "2026-10-19", "--ics", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8").count("BEGIN:VEVENT") == 3


@pytest.mark.usefixtures("env")
def test_cli_grades_json() -> None:
    with respx.mock(base_url=API_URL) as api:
        api.get("/me/2025/grades").mock(return_value=httpx.Response(200, json=fixture("getGrades")))
        result = CliRunner().invoke(app, ["grades", "-y", "2025", "--json"])
    assert result.exit_code == 0, result.output
    assert '"average": 15.25' in result.output


@pytest.mark.usefixtures("env")
def test_cli_reports_api_errors() -> None:
    with respx.mock(base_url=API_URL) as api:
        api.get("/me/courses/9").mock(return_value=httpx.Response(404, json=fixture("error_404")))
        result = CliRunner().invoke(app, ["raw", "/me/courses/9"])
    assert result.exit_code == 1


@pytest.mark.usefixtures("env")
def test_cli_files_download_is_idempotent(tmp_path: Path) -> None:
    with respx.mock(base_url=API_URL) as api:
        mock_courses(api)
        api.get("/me/500001/files").mock(
            return_value=httpx.Response(200, json=fixture("getCourseFiles"))
        )
        dl = api.get(path__regex=r"/me/500001/files/\d+").mock(
            return_value=httpx.Response(200, content=b"%PDF-contenu")
        )
        first = CliRunner().invoke(app, ["files", "500001", "--download", str(tmp_path)])
        second = CliRunner().invoke(app, ["files", "500001", "--download", str(tmp_path)])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert dl.call_count == 2
    saved = sorted(p.name for p in (tmp_path / "T1 - Réseaux avancés").iterdir())
    assert saved == ["Chapitre 1 - Introduction.pdf", "TP1_sources.zip"]


def test_cli_login_remembers_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MYGES_USERNAME", "MYGES_PASSWORD", "MYGES_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    location = "comreseaugesskolae:/oauth2redirect#access_token=tok&expires_in=560063"
    runner = CliRunner()
    with respx.mock(base_url=API_URL) as api:
        api.get(AUTH_URL).mock(return_value=httpx.Response(302, headers={"Location": location}))
        years = api.get("/me/years").mock(
            return_value=httpx.Response(200, json=fixture("getYears"))
        )

        assert runner.invoke(app, ["years"]).exit_code == 2  # pas encore connecté
        login = runner.invoke(app, ["login"], input="jdupont\nsecret\n")
        assert login.exit_code == 0, login.output
        result = runner.invoke(app, ["years"])  # aucune variable d'environnement
        assert result.exit_code == 0, result.output
        assert "2026" in result.output
        assert years.calls.last.request.headers["Authorization"] == "Bearer tok"

        assert runner.invoke(app, ["logout"]).exit_code == 0
        assert runner.invoke(app, ["years"]).exit_code == 2


@pytest.mark.usefixtures("env")
def test_cli_expands_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # PowerShell transmet « ~ » tel quel : la CLI doit le développer elle-même.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with respx.mock(base_url=API_URL) as api:
        api.get("/me/agenda").mock(return_value=httpx.Response(200, json=fixture("getAgenda")))
        result = CliRunner().invoke(app, ["agenda", "--ics", "~/cours.ics"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "cours.ics").exists()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["files", "--download"], ["files", "--download", "."]),
        (["files", "-d", "--json"], ["files", "-d", ".", "--json"]),
        (["files", "--download", "~/cours"], ["files", "--download", "~/cours"]),
        (["files", "500001", "-d"], ["files", "500001", "-d", "."]),
        (["agenda", "-d"], ["agenda", "-d"]),  # -d = --days ici : on ne touche à rien
        ([], []),
    ],
)
def test_download_without_folder_defaults_to_cwd(argv: list[str], expected: list[str]) -> None:
    assert _default_download_dir(argv) == expected


@pytest.mark.usefixtures("env")
def test_cli_download_into_current_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["myges", "files", "500001", "--download"])
    with respx.mock(base_url=API_URL) as api:
        mock_courses(api)
        api.get("/me/500001/files").mock(
            return_value=httpx.Response(200, json=fixture("getCourseFiles"))
        )
        api.get(path__regex=r"/me/500001/files/\d+").mock(
            return_value=httpx.Response(200, content=b"%PDF")
        )
        with pytest.raises(SystemExit) as done:
            main()
    assert done.value.code == 0
    assert sorted(p.name for p in (tmp_path / "T1 - Réseaux avancés").iterdir()) == [
        "Chapitre 1 - Introduction.pdf",
        "TP1_sources.zip",
    ]


# ---------------------------------------------------------------- Désignation des matières
# Fixture getCourses, triée par nom : #1 = « T1 - Anglais, niveau C1 » (500002),
#                                      #2 = « T1 - Réseaux avancés » (500001).


@pytest.mark.usefixtures("env")
@pytest.mark.parametrize(
    ("ref", "rc_id"),
    [
        ("1", 500002),  # numéro de la colonne #
        ("2", 500001),
        ("reseaux", 500001),  # nom partiel, sans accent
        ("ANGLAIS c1", 500002),  # casse ignorée, plusieurs mots
        ("500001", 500001),  # rc_id
        ("424242", 424242),  # rc_id d'une autre année : transmis tel quel
    ],
)
def test_course_reference_resolution(ref: str, rc_id: int) -> None:
    with respx.mock(base_url=API_URL) as api:
        mock_courses(api)
        route = api.get(f"/me/{rc_id}/files").mock(
            return_value=httpx.Response(200, json=fixture("getCourseFiles"))
        )
        result = CliRunner().invoke(app, ["files", ref])
    assert result.exit_code == 0, result.output
    assert route.called


@pytest.mark.usefixtures("env")
@pytest.mark.parametrize(("ref", "message"), [("t1", "ambigu"), ("chimie", "Aucune matière")])
def test_course_reference_errors(ref: str, message: str) -> None:
    with respx.mock(base_url=API_URL) as api:
        mock_courses(api)
        result = CliRunner().invoke(app, ["syllabus", ref])
    assert result.exit_code == 2
    assert message in result.output


@pytest.mark.usefixtures("env")
def test_course_numbers_shown_in_listings() -> None:
    with respx.mock(base_url=API_URL) as api:
        mock_courses(api)
        api.get("/me/2026/grades").mock(return_value=httpx.Response(200, json=fixture("getGrades")))
        courses = CliRunner().invoke(app, ["courses"])
        grades = CliRunner().invoke(app, ["grades"])
    assert courses.exit_code == 0, courses.output
    assert grades.exit_code == 0, grades.output
    for output in (courses.output, grades.output):
        lines = [line for line in output.splitlines() if "Réseaux avancés" in line]
        assert lines
        assert "│ 2 " in lines[0] or lines[0].lstrip("│ ").startswith("2")


# ---------------------------------------------------------------- today / next
# Premier créneau de la fixture getAgenda : T1 - Réseaux avancés (#2), 3 h, salle B12.


def _freeze(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Frozen:  # type: ignore[override]
            return cls.fromtimestamp(now.timestamp(), tz=tz or timezone.utc)

    monkeypatch.setattr("myges.cli.datetime", Frozen)


def _first_slot() -> datetime:
    return datetime.fromtimestamp(
        fixture("getAgenda")["result"][0]["start_date"] / 1000, timezone.utc
    )


@pytest.mark.usefixtures("env")
@pytest.mark.parametrize(
    ("offset", "expected"),
    [(timedelta(hours=-2), "Dans 2 h"), (timedelta(hours=1), "En cours")],
)
def test_cli_next(monkeypatch: pytest.MonkeyPatch, offset: timedelta, expected: str) -> None:
    _freeze(monkeypatch, _first_slot() + offset)
    with respx.mock(base_url=API_URL) as api:
        mock_courses(api)
        api.get("/me/agenda").mock(return_value=httpx.Response(200, json=fixture("getAgenda")))
        result = CliRunner().invoke(app, ["next"])
    assert result.exit_code == 0, result.output
    assert expected in result.output
    assert "Réseaux avancés" in result.output
    assert "#2" in result.output


@pytest.mark.usefixtures("env")
def test_cli_today_without_classes_shows_next(monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch, _first_slot() - timedelta(days=2))
    empty = {"response_code": 0, "version": "1", "result": [], "links": []}
    with respx.mock(base_url=API_URL) as api:
        mock_courses(api)
        api.get("/me/agenda").mock(
            side_effect=[
                httpx.Response(200, json=empty),  # aujourd'hui
                httpx.Response(200, json=fixture("getAgenda")),  # recherche du prochain
            ]
        )
        result = CliRunner().invoke(app, ["today"])
    assert result.exit_code == 0, result.output
    assert "Pas de cours aujourd'hui" in result.output
    assert "Dans 2 j" in result.output


def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("myges-api ")
