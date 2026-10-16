"""myges watch : détection des nouveautés et envoi des notifications."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import fixture, mock_api
from myges import models as m
from myges import notify
from myges.auth import AUTH_URL
from myges.cli import app
from myges.watch import Change, diff, snapshot

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)  # la veille du 1er créneau (18/10)


def base_snapshot(**changes: Any) -> dict[str, Any]:
    grades = [m.Grade.model_validate(g) for g in fixture("getGrades")["result"]]
    absences = [m.Absence.model_validate(a) for a in fixture("getAbsences")["result"]]
    files = {
        500001: (
            "T1 - Réseaux avancés",
            [m.CourseFile.model_validate(f) for f in fixture("getCourseFiles")["result"]],
        )
    }
    agenda = [m.AgendaEvent.model_validate(e) for e in fixture("getAgenda")["result"]]
    snap = snapshot(grades, absences, files, agenda, NOW)
    for key, value in changes.items():
        snap[key] = value
    return snap


def kinds(changes: list[Change]) -> list[str]:
    return [c.kind for c in changes]


def test_identical_snapshots_have_no_changes() -> None:
    assert diff(base_snapshot(), base_snapshot()) == []


def test_new_grade_and_exam() -> None:
    old = base_snapshot()
    new = copy.deepcopy(old)
    anglais = new["grades"]["500002"]
    anglais["grades"] = [12.5]
    anglais["average"] = 12.5
    reseaux = new["grades"]["500001"]
    reseaux["exam"] = 18.0
    texts = {c.text for c in diff(old, new)}
    assert texts == {
        "Nouvelle note en T1 - Anglais, niveau C1 : 12.5 (moyenne 12.5)",
        "Note d'examen en T1 - Réseaux avancés : 18",
    }


def test_duplicate_grade_value_is_detected() -> None:
    old = base_snapshot()
    new = copy.deepcopy(old)
    new["grades"]["500001"]["grades"] = [14.5, 17.0, 14.5]  # même valeur une 2e fois
    assert diff(old, new)[0].text.endswith(": 14.5 (moyenne 15.25)")


def test_new_file_and_absence() -> None:
    old = base_snapshot()
    new = copy.deepcopy(old)
    new["files"]["500001"]["items"]["610099"] = "Chapitre 2.pdf"
    new["absences"].append(f"{int(NOW.timestamp() * 1000)}|T1 - Anglais, niveau C1|0")
    changes = diff(old, new)
    assert kinds(changes) == ["absence", "file"]
    assert changes[1].text == "Nouveau support en T1 - Réseaux avancés : Chapitre 2.pdf"


def test_agenda_cancelled_moved_added() -> None:
    old = base_snapshot()
    new = copy.deepcopy(old)
    del new["agenda"]["3000001"]
    new["agenda"]["3000002"]["room"] = "Amphi A"
    new["agenda"]["3999999"] = {
        "name": "Rattrapage",
        "start": old["taken_at"] + 3_600_000,
        "end": old["taken_at"] + 7_200_000,
        "room": "B12",
    }
    assert sorted(kinds(diff(old, new))) == ["agenda_added", "agenda_moved", "agenda_removed"]


def test_past_class_is_not_reported_as_cancelled() -> None:
    old = base_snapshot()
    new = copy.deepcopy(old)
    first = old["agenda"]["3000001"]
    # Nouveau passage après la fin du 1er créneau : il sort de l'agenda sans être annulé.
    new["taken_at"] = first["end"] + 60_000
    del new["agenda"]["3000001"]
    assert diff(old, new) == []


# ---------------------------------------------------------------- Canaux


def test_ntfy_payload() -> None:
    with respx.mock() as router:
        route = router.post("https://ntfy.sh/myges-secret").mock(return_value=httpx.Response(200))
        notify.send(
            {"type": "ntfy", "server": "https://ntfy.sh", "topic": "myges-secret"},
            "myGES : 1 nouveauté",
            "📊 Nouvelle note",
        )
    request = route.calls.last.request
    assert request.content.decode() == "📊 Nouvelle note"
    assert request.headers["Title"].startswith("=?utf-8?")  # titre non ASCII encodé


def test_discord_splits_long_messages() -> None:
    body = "\n".join(f"📄 Nouveau support numéro {i:04d} " + "x" * 80 for i in range(60))
    with respx.mock() as router:
        route = router.post("https://discord.com/api/webhooks/1/abc").mock(
            return_value=httpx.Response(204)
        )
        notify.send(
            {"type": "discord", "webhook": "https://discord.com/api/webhooks/1/abc"}, "myGES", body
        )
    sent = [json.loads(c.request.content)["content"] for c in route.calls]
    assert len(sent) > 1
    assert all(len(chunk) <= notify.DISCORD_LIMIT for chunk in sent)
    assert sum(chunk.count("📄") for chunk in sent) == 60


def test_email(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            sent.append(("connect", host, port))

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *exc: object) -> None: ...

        def starttls(self, context: Any) -> None:
            sent.append(("starttls",))

        def login(self, user: str, password: str) -> None:
            sent.append(("login", user, password))

        def send_message(self, message: Any) -> None:
            sent.append(("send", message["To"], message["Subject"], message.get_content()))

    monkeypatch.setattr("myges.notify.smtplib.SMTP", FakeSMTP)
    notify.send(
        {
            "type": "email",
            "to": "moi@example.org",
            "host": "smtp.example.org",
            "port": 587,
            "user": "moi@example.org",
            "password": "pw",
        },
        "Titre",
        "Corps",
    )
    assert sent[0] == ("connect", "smtp.example.org", 587)
    assert ("login", "moi@example.org", "pw") in sent
    assert sent[-1] == ("send", "moi@example.org", "Titre", "Corps\n")


# ---------------------------------------------------------------- Commande watch


def test_watch_baseline_then_notifies_new_grade(home: Path) -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as router:
        ntfy = router.post("https://ntfy.sh/myges-t").mock(return_value=httpx.Response(200))
        mock_api(router, fixture("getGrades"))
        first = runner.invoke(app, ["watch"])
        assert first.exit_code == 0, first.output
        assert "État initial enregistré" in first.output
        assert not ntfy.called

        again = runner.invoke(app, ["watch", "--quiet"])
        assert again.exit_code == 0, again.output
        assert again.output == ""

        updated = fixture("getGrades")
        updated["result"][1]["grades"] = [11.0]
        mock_api(router, updated)
        third = runner.invoke(app, ["watch"])
    assert third.exit_code == 0, third.output
    assert "Nouvelle note en T1 - Anglais, niveau C1 : 11" in third.output
    assert ntfy.call_count == 1
    assert "11" in ntfy.calls.last.request.content.decode()


def test_watch_expired_session_notifies_once(home: Path) -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as router:
        mock_api(router, fixture("getGrades"))
        assert runner.invoke(app, ["watch"]).exit_code == 0  # état initial
    with respx.mock(assert_all_called=False) as router:
        ntfy = router.post("https://ntfy.sh/myges-t").mock(return_value=httpx.Response(200))
        router.get(AUTH_URL).mock(return_value=httpx.Response(401))
        router.route(host="api.kordis.fr").mock(return_value=httpx.Response(401))
        for _ in range(3):
            result = runner.invoke(app, ["watch"])
            assert result.exit_code == 1
    assert ntfy.call_count == 1
    assert "session" in ntfy.calls.last.request.content.decode().lower()


def test_notify_add_list_remove(home: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["notify", "add", "ntfy"]).exit_code == 0
    assert (
        runner.invoke(
            app, ["notify", "add", "discord", "https://discord.com/api/webhooks/1/abcdefgh"]
        ).exit_code
        == 0
    )
    listing = runner.invoke(app, ["notify", "list"]).output
    assert "1. ntfy" in listing
    assert "3. discord" in listing
    config = home / "xdg_config_home" / "myges" / "config.json"
    assert (config.stat().st_mode & 0o777) == 0o600
    topics = [c["topic"] for c in json.loads(config.read_text())["notify"] if c["type"] == "ntfy"]
    assert topics[1].startswith("myges-")
    assert len(topics[1]) > 20  # sujet aléatoire, pas devinable
    assert runner.invoke(app, ["notify", "remove", "1"]).exit_code == 0
    assert "1. ntfy" in runner.invoke(app, ["notify", "list"]).output  # l'ancien n°2


def test_notify_test_reports_failures(home: Path) -> None:
    with respx.mock() as router:
        router.post("https://ntfy.sh/myges-t").mock(return_value=httpx.Response(500))
        result = CliRunner().invoke(app, ["notify", "test"])
    assert result.exit_code == 1
    assert "Échec d'envoi" in result.output


def test_login_save_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MYGES_USERNAME", "MYGES_PASSWORD", "MYGES_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "k"))
    location = "comreseaugesskolae:/oauth2redirect#access_token=tok&expires_in=560063"
    with respx.mock() as router:
        router.get(AUTH_URL).mock(return_value=httpx.Response(302, headers={"Location": location}))
        result = CliRunner().invoke(app, ["login", "--save-password"], input="jdupont\nsecret\n")
    assert result.exit_code == 0, result.output
    config = json.loads((tmp_path / "c" / "myges" / "config.json").read_text())
    assert (config["username"], config["password"]) == ("jdupont", "secret")
    assert CliRunner().invoke(app, ["logout"]).exit_code == 0
    assert "password" not in json.loads((tmp_path / "c" / "myges" / "config.json").read_text())


def test_delivery_error_explains_why() -> None:
    quota = {"code": 42908, "http": 429, "error": "limit reached: daily message quota reached"}
    with respx.mock() as router:
        router.post("https://ntfy.sh/t").mock(return_value=httpx.Response(429, json=quota))
        with pytest.raises(notify.DeliveryError, match="HTTP 429 : limit reached: daily"):
            notify.send({"type": "ntfy", "server": "https://ntfy.sh", "topic": "t"}, "t", "b")
