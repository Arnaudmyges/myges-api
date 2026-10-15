from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import fixture
from myges import models as m
from myges import to_ics
from myges.cli import app
from myges.client import API_URL


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
    saved = sorted(p.name for p in (tmp_path / "500001").iterdir())
    assert saved == ["Chapitre 1 - Introduction.pdf", "TP1_sources.zip"]
