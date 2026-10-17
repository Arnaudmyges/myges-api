"""Serveur MCP : outils appelés par un vrai client MCP, en mémoire, sur l'API simulée."""

from __future__ import annotations

import copy
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
        ("/me/news/banners", "getBanners"),
        ("/me/2026/practicals", "getPracticals"),
        ("/me/nextProjectSteps", "getNextProjectSteps"),
        ("/me/projectGroups/520001/messages", "getProjectGroupMessages"),
        ("/me/500001/syllabus", "getSyllabus"),
        ("/me/500001/files", "getCourseFiles"),
        ("/me/campus", "getCampuses"),
        ("/me/schools", "getSchools"),
        ("/me/partners", "getPartners"),
    ]:
        router.get(f"{API_URL}{path}").mock(return_value=httpx.Response(200, json=fixture(name)))
    router.get(f"{API_URL}/me/2026/absences").mock(return_value=httpx.Response(204))
    router.get(url__regex=rf"{API_URL}/me/500001/files/\d+").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 support")
    )
    router.get(f"{API_URL}/me/projectFiles/9001").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 sujet")
    )


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


MYGES_WRITES = {"subscribe_event", "unsubscribe_event"}
LOCAL_WRITES = {
    "draft_email",
    "cancel_pending_registration",
    "download_course_files",
    "download_project_files",
    "export_agenda_ics",
}


async def test_tools_annotations() -> None:
    """Lecture seule par défaut ; seules les inscriptions sont marquées « destructives »,
    pour que le client MCP demande confirmation."""
    async with Client(build_server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert len(tools) >= 30
    assert set(tools) >= MYGES_WRITES | LOCAL_WRITES
    for name, tool in tools.items():
        hints = tool.annotations
        assert hints is not None, name
        assert tool.description, name
        writes = name in MYGES_WRITES | LOCAL_WRITES
        assert hints.read_only_hint is (not writes), name
        if writes:
            assert hints.destructive_hint is (name in MYGES_WRITES), name


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


# ---------------------------------------------------------------- Outils ajoutés


async def test_school_tools(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        school_api(router)
        syllabus = await call("syllabus", course="reseaux")
        files = await call("course_files")
        practicals = await call("practicals")
        deadlines = await call("next_deadlines")
        messages = await call("project_messages", project="proxmox")
        campuses = await call("campuses")
        schools = await call("schools")
        partners = await call("partners")
        banners = await call("announcements")
        classes = await call("classes")
    assert syllabus["syllabus_name"] == "Réseaux avancés"
    assert "links" not in syllabus
    assert [f["file_id"] for f in files] == [610001, 610002]  # anglais : pas de documents
    assert practicals[0]["name"] == "TP noté : pare-feu"
    assert deadlines[0]["project_id"] == 24001
    assert messages == {
        "project": "Mise en place d'un cluster Proxmox",
        "group": "Groupe 1",
        "messages": [],
    }
    for listing in (campuses, schools, partners, banners):
        assert listing
    assert classes[0]["puid"] == 20001


async def test_course_reference_errors(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        school_api(router)
        unknown = await call_error("syllabus", course="chimie")
        ambiguous = await call_error("syllabus", course="T1")
    assert "aucune matière" in unknown
    assert "ambigu" in ambiguous
    assert "500001" in ambiguous


async def test_simulate_and_grade_needed(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        school_api(router)
        simulated = await call("simulate_average", simulated={"anglais": 11})
        needed = await call("grade_needed", course="anglais", target=14)
        out_of_range = await call_error("simulate_average", simulated={"anglais": 25})
    # Réseaux 15.25 (coef 3) + anglais 11 (coef 1) → 14.1875
    assert simulated["overall_average"] == pytest.approx(14.1875)
    # 14 = (3 × 15.25 + x) / 4  →  x = 10.25
    assert needed["needed"] == pytest.approx(10.25)
    assert needed["reachable"] is True
    assert "0–20" in out_of_range


async def test_weekly_summary(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        school_api(router)
        summary = await call("weekly_summary")
    assert summary["title"].startswith("myGES")
    assert summary["text"]


async def test_downloads_and_ics(home: Path, tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        school_api(router)
        first = await call("download_course_files", directory=str(tmp_path / "dl"))
        again = await call("download_course_files", directory=str(tmp_path / "dl"))
        project = await call("download_project_files", project="24001", directory=str(tmp_path))
        ics = await call("export_agenda_ics", path=str(tmp_path / "cours.ics"))
        bad = await call_error("export_agenda_ics", path=str(tmp_path / "cours.txt"))
    assert len(first["downloaded"]) == 2
    assert again == {**again, "downloaded": [], "already_present": 2}
    saved = Path(project["downloaded"][0])
    assert saved.name == "Sujet.pdf"
    assert saved.read_bytes() == b"%PDF-1.4 sujet"
    assert not list(tmp_path.rglob("*.part"))
    assert Path(ics["path"]).read_text().startswith("BEGIN:VCALENDAR")
    assert ".ics" in bad


# ---------------------------------------------------------------- Inscriptions


@pytest.fixture
def cli_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Commandes `myges` lancées par les outils d'inscription (rien n'est exécuté)."""
    import subprocess

    runs: list[list[str]] = []

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        runs.append(args[3:])  # sans « python -m myges.cli »
        return subprocess.CompletedProcess(args, 0, "Inscrit : test", "")

    monkeypatch.setattr("myges.mcp_server.subprocess.run", fake_run)
    return runs


def events_with(**changes: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(fixture("getEvents"))
    for event in payload["result"]:
        event.update(changes.get(str(event["event_id"]), {}))
    return payload


async def test_subscribe_runs_cli_only_when_needed(home: Path, cli_runs: list[list[str]]) -> None:
    payload = fixture("getEvents")
    event = payload["result"][0]
    event_id = event["event_id"]
    future = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp() * 1000)
    past = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp() * 1000)
    with respx.mock(assert_all_called=False) as router:
        school_api(router)
        route = router.get(f"{API_URL}/me/events")

        route.mock(
            return_value=httpx.Response(
                200,
                json=events_with(
                    **{
                        str(event_id): {
                            "is_participant_subscribed": False,
                            "end_subscription_date": future,
                        }
                    }
                ),
            )
        )
        done = await call("subscribe_event", event_id=event_id)

        route.mock(
            return_value=httpx.Response(
                200, json=events_with(**{str(event_id): {"is_participant_subscribed": True}})
            )
        )
        already = await call("subscribe_event", event_id=event_id)
        leave = await call("unsubscribe_event", event_id=event_id)
        assert leave["output"] == "Inscrit : test"  # sortie de la CLI simulée

        route.mock(
            return_value=httpx.Response(
                200,
                json=events_with(
                    **{
                        str(event_id): {
                            "is_participant_subscribed": False,
                            "end_subscription_date": past,
                        }
                    }
                ),
            )
        )
        closed = await call_error("subscribe_event", event_id=event_id)
        not_in = await call("unsubscribe_event", event_id=event_id)
        unknown = await call_error("subscribe_event", event_id=1)
        # Aucune écriture directe sur l'API : tout passe par la CLI.
        assert all(c.request.method == "GET" for c in router.calls)
    assert done["output"] == "Inscrit : test"
    assert "Déjà inscrit" in already["output"]
    assert "Pas inscrit" in not_in["output"]
    assert "closes" in closed
    assert "introuvable" in unknown
    assert cli_runs == [
        ["events", "subscribe", str(event_id)],
        ["events", "unsubscribe", str(event_id)],
    ]


async def test_cancel_pending(home: Path, cli_runs: list[list[str]]) -> None:
    from myges import pending as pending_mod

    nothing = await call("cancel_pending_registration", event_id=4525)
    pending_mod.register([pending_mod.Pending(4525, "2026-10-16T12:00:00+02:00", 1, "x")])
    listed = await call("pending_registrations")
    await call("cancel_pending_registration", event_id=4525)
    assert "Aucune" in nothing["output"]
    assert listed == [{"event_id": 4525, "opens": "2026-10-16T12:00:00+02:00"}]
    assert cli_runs == [["events", "cancel", "4525"]]


async def test_cli_failure_becomes_tool_error(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(
        "myges.mcp_server.subprocess.run",
        lambda args, **_: subprocess.CompletedProcess(args, 1, "", "Erreur : 403 EVENT_CLOSED"),
    )
    from myges import pending as pending_mod

    pending_mod.register([pending_mod.Pending(4525, "2026-10-16T12:00:00+02:00", 1, "x")])
    assert "EVENT_CLOSED" in await call_error("cancel_pending_registration", event_id=4525)
