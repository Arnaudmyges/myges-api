"""Serveur MCP : outils appelés par un vrai client MCP, en mémoire, sur l'API simulée."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import fixture
from myges import mail
from myges.client import API_URL

pytest.importorskip("mcp")

from mcp import Client

from myges.cli import app
from myges.mcp_server import build_server

pytestmark = pytest.mark.anyio

FIRST = datetime.fromtimestamp(fixture("getAgenda")["result"][0]["start_date"] / 1000, timezone.utc)


def school_api(router: respx.MockRouter) -> None:
    for path, name in [
        ("/me/years", "getYears"),
        ("/me/profile", "getProfile"),
        ("/me/2026/classes", "getClasses"),
        ("/me/2026/courses", "getCourses"),
        ("/me/2026/grades", "getGrades"),
        ("/me/2026/teachers", "getTeachers"),
        ("/me/2026/projects", "getProjects"),
        ("/me/classes/20001/students", "getClassStudents"),
        ("/me/agenda", "getAgenda"),
        ("/me/events", "getEvents"),
        ("/me/news", "getNews"),
        ("/me/news/4001", "getNewsItem"),
    ]:
        router.get(f"{API_URL}{path}").mock(return_value=httpx.Response(200, json=fixture(name)))


async def call(name: str, **arguments: Any) -> Any:
    async with Client(build_server()) as client:
        result = await client.call_tool(name, arguments)
    if result.is_error:
        raise AssertionError(result.content[0].text)  # type: ignore[union-attr]
    content = result.structured_content
    # Les retours qui ne sont pas des objets arrivent enveloppés dans {"result": …}.
    if isinstance(content, dict) and set(content) == {"result"}:
        return content["result"]
    return content


async def call_error(name: str, **arguments: Any) -> str:
    async with Client(build_server()) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error
    return str(result.content[0].text)  # type: ignore[union-attr]


async def test_tools_listed_and_read_only_except_draft() -> None:
    async with Client(build_server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert {"grades", "agenda", "next_class", "teachers", "draft_email"} <= set(tools)
    for name, tool in tools.items():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is (name != "draft_email"), name
    assert tools["draft_email"].annotations.destructive_hint is False  # type: ignore[union-attr]


async def test_grades_overall_average(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        school_api(router)
        data = await call("grades")
    assert data["overall_average"] == pytest.approx(15.25)  # anglais sans note : ignoré
    by_name = {c["course"]: c for c in data["courses"]}
    assert by_name["T1 - Réseaux avancés"]["source"] == "officielle"
    assert by_name["T1 - Réseaux avancés"]["grades"] == [14.5, 17.0]
    assert by_name["T1 - Anglais, niveau C1"]["source"] == "aucune"


async def test_whoami_is_minimal(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        school_api(router)
        data = await call("whoami")
    assert set(data) == {"name", "email", "year", "classes", "today"}
    assert data["classes"] == [{"puid": 20001, "name": "3ESGI SRC CL A"}]


async def test_agenda_query_and_next_class(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Frozen:  # type: ignore[override]
            return cls.fromtimestamp((FIRST - timedelta(days=1)).timestamp(), tz=tz)

    monkeypatch.setattr("myges.mcp_server.datetime", Frozen)
    with respx.mock(assert_all_called=False) as router:
        school_api(router)
        classes = await call("agenda", query="reseaux")
        upcoming = await call("next_class", query="anglais")
    assert [c["course"] for c in classes] == ["T1 - Réseaux avancés"]
    assert classes[0]["rooms"][0]["name"] == "B12"
    assert classes[0]["start"].endswith("+02:00") or classes[0]["start"].endswith("+01:00")
    assert upcoming["course"] == "T1 - Anglais, niveau C1"
    assert upcoming["ongoing"] is False


async def test_agenda_rejects_bad_date(home: Path) -> None:
    assert "AAAA-MM-JJ" in await call_error("agenda", start="demain")


async def test_teachers_courses_and_classmates(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        school_api(router)
        teachers = await call("teachers")
        courses = await call("courses")
        mates = await call("classmates")
    assert teachers[0]["email"] == "alovelace@example.org"
    assert teachers[0]["courses"] == ["T1 - Réseaux avancés"]
    reseaux = next(c for c in courses if c["rc_id"] == 500001)
    assert reseaux["teacher_email"] == "alovelace@example.org"
    me = fixture("getProfile")["result"]["uid"]
    assert all(m["email"] != "c.exemple@myskolae.fr" for m in mates)  # soi-même exclu
    assert len(mates) == len(fixture("getClassStudents")["result"]) - 1
    assert me


async def test_news_and_projects(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        school_api(router)
        found = await call("news", query="portes ouvertes")
        item = await call("read_news", news_id=4001)
        projects = await call("projects")
    assert found[0]["news_id"] == 4001
    assert item["text"] == "Contenu de l'actualité."
    assert projects[0]["name"] == "Mise en place d'un cluster Proxmox"


async def test_draft_email_never_sends(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(mail, "open_url", lambda url: opened.append(url) or True)
    with respx.mock() as router:  # aucune requête réseau attendue
        result = await call(
            "draft_email",
            to=["alovelace@example.org"],
            subject="Edusign oublié",
            body="Bonjour,\nJ'ai oublié de signer ce matin.",
        )
        assert not router.calls
    assert result["sent"] is False
    assert result["opened_in_browser"] is True
    query = parse_qs(urlsplit(opened[0]).query)
    assert query["to"] == ["alovelace@example.org"]
    assert query["subject"] == ["Edusign oublié"]


async def test_draft_email_requires_an_address(home: Path) -> None:
    assert "adresse" in await call_error("draft_email", to=["nobody"], subject="x", body="y")


async def test_not_logged_in(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYGES_USERNAME")
    monkeypatch.setattr("myges.cli._saved_username", lambda: None)
    assert "myges login" in await call_error("grades")


def test_mcp_install_registers_with_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    import subprocess

    calls: list[list[str]] = []
    monkeypatch.setattr(
        shutil, "which", lambda name: {"claude": "/bin/claude", "myges": "/bin/myges"}.get(name)
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **_: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    result = CliRunner().invoke(app, ["mcp", "--install"])
    assert result.exit_code == 0, result.output
    assert calls[-1] == [
        "/bin/claude",
        "mcp",
        "add",
        "-s",
        "user",
        "myges",
        "--",
        "/bin/myges",
        "mcp",
    ]


def test_mcp_missing_module_hint_keeps_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_mcp(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "mcp":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_mcp)
    result = CliRunner().invoke(app, ["mcp"])
    assert result.exit_code == 1
    assert "myges-api[cli]" in result.output
