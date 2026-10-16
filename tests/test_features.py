"""Rappels de cours, récapitulatif, nouveaux événements et boutons « S'inscrire »."""

from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import FakeSystem, fixture, mock_api
from myges import notify, schedule, watch
from myges.cli import _event_actions, _parse_duration, app
from myges.client import API_URL
from myges.serve import make_server

NTFY = "https://ntfy.sh/myges-t"
FIRST = datetime.fromtimestamp(fixture("getAgenda")["result"][0]["start_date"] / 1000, timezone.utc)


def freeze(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Frozen:  # type: ignore[override]
            return cls.fromtimestamp(now.timestamp(), tz=tz or timezone.utc)

    monkeypatch.setattr("myges.cli.datetime", Frozen)


def set_config(home: Path, **values: Any) -> None:
    path = home / "xdg_config_home" / "myges" / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


# ---------------------------------------------------------------- Rappels


@pytest.mark.parametrize(
    ("text", "minutes"), [("15m", 15), ("15", 15), ("1h", 60), ("1h30", 90), ("90min", 90)]
)
def test_parse_duration(text: str, minutes: int) -> None:
    assert _parse_duration(text) == timedelta(minutes=minutes)


def test_due_reminders_window() -> None:
    agenda = watch.agenda_section(
        [watch.AgendaEvent.model_validate(e) for e in fixture("getAgenda")["result"]],
        FIRST - timedelta(days=1),
    )
    rid = next(iter(agenda))
    before = timedelta(minutes=15)
    assert (
        watch.due_reminders(agenda, FIRST - timedelta(minutes=20), before, [], timezone.utc) == []
    )
    due = watch.due_reminders(agenda, FIRST - timedelta(minutes=14), before, [], timezone.utc)
    assert [r for r, _ in due] == [rid]
    assert due[0][1].text.startswith("Dans 14 min : T1 - Réseaux avancés · B12 (CAMPUS1)")
    assert (
        watch.due_reminders(agenda, FIRST - timedelta(minutes=10), before, [rid], timezone.utc)
        == []
    )


def test_watch_sends_each_reminder_once(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_config(home, remind={"before_minutes": 15})
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as router:
        ntfy = router.post(NTFY).mock(return_value=httpx.Response(200))
        mock_api(router, fixture("getGrades"))
        freeze(monkeypatch, FIRST - timedelta(hours=3))
        assert runner.invoke(app, ["watch"]).exit_code == 0  # état initial
        freeze(monkeypatch, FIRST - timedelta(minutes=12))
        first = runner.invoke(app, ["watch", "--quiet"])
        agenda_reads = [c for c in router.calls if c.request.url.path == "/me/agenda"]
        again = runner.invoke(app, ["watch", "--quiet"])
    assert first.exit_code == 0, first.output
    assert again.exit_code == 0, again.output
    reminders = [c for c in ntfy.calls if "rappel" in c.request.headers["Title"].lower()
                 or "Dans 12 min" in c.request.content.decode()]  # fmt: skip
    assert len(reminders) == 1
    assert "B12 (CAMPUS1)" in reminders[0].request.content.decode()
    assert len(agenda_reads) == 2  # état initial + relecture pour la salle à jour


def test_remind_command(home: Path) -> None:
    runner = CliRunner()
    assert "désactivés" in runner.invoke(app, ["remind"]).output
    assert "1 h avant" in runner.invoke(app, ["remind", "--before", "1h"]).output
    assert "1 h avant" in runner.invoke(app, ["remind"]).output
    assert runner.invoke(app, ["remind", "--before", "abc"]).exit_code == 2
    assert "désactivés" in runner.invoke(app, ["remind", "--off"]).output


# ---------------------------------------------------------------- Nouveaux événements


def test_new_event_is_detected_but_not_on_first_listing() -> None:
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    events = [watch.CampusEvent.model_validate(e) for e in fixture("getEvents")["result"]]
    section = watch.events_section(events, now)
    assert watch.diff({"taken_at": 0}, {"taken_at": 0, "events": section}) == []  # 1re fois
    changes = watch.diff({"events": {}}, {"taken_at": watch._ms(now), "events": section})
    assert [(c.kind, c.event_id) for c in changes] == [("event_new", 4501)]
    assert "Afterwork de rentrée" in changes[0].text
    assert "inscriptions le" in changes[0].text


def test_event_buttons(home: Path) -> None:
    change = watch.Change("event_new", "Nouvel événement : x", 4525)
    view = _event_actions([change])
    assert view == [{"type": "view", "label": "Voir (4525)",
                     "url": "https://myges.fr/common/event/4525"}]  # fmt: skip
    set_config(home, serve={"url": "http://100.64.0.1:2587", "token": "secret"})
    actions = _event_actions([change])
    assert actions[0]["url"] == "http://100.64.0.1:2587/events/4525/subscribe"
    assert actions[0]["headers"] == {"Authorization": "Bearer secret"}
    assert actions[1]["type"] == "view"
    header = notify.ntfy_actions(actions)
    assert header.startswith("http, S'inscrire, http://100.64.0.1:2587/events/4525/subscribe")
    assert "headers.Authorization=Bearer secret" in header


def test_ntfy_sends_actions_header() -> None:
    with respx.mock() as router:
        route = router.post("https://ntfy.sh/t").mock(return_value=httpx.Response(200))
        notify.send(
            {"type": "ntfy", "server": "https://ntfy.sh", "topic": "t"},
            "t",
            "b",
            actions=[{"type": "view", "label": "Voir", "url": "https://x.test"}],
        )
    assert route.calls.last.request.headers["Actions"] == "view, Voir, https://x.test"


# ---------------------------------------------------------------- myges serve


def test_serve_subscribe_endpoint() -> None:
    received: list[int] = []
    server = make_server("127.0.0.1", 0, "s3cret", lambda i: received.append(i) or "ok")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with httpx.Client() as http:
            assert http.get(f"{base}/health").status_code == 200
            assert http.post(f"{base}/events/4525/subscribe").status_code == 401
            bad = http.post(f"{base}/events/4525/subscribe", headers={"Authorization": "Bearer x"})
            assert bad.status_code == 401
            ok = http.post(
                f"{base}/events/4525/subscribe", headers={"Authorization": "Bearer s3cret"}
            )
            assert ok.status_code == 202
            assert (
                http.post(f"{base}/admin", headers={"Authorization": "Bearer s3cret"}).status_code
                == 404
            )
    finally:
        server.shutdown()
        server.server_close()
    assert received == [4525]


# ---------------------------------------------------------------- Récapitulatif


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("lundi 07:00", ("lundi", 7, 0)),
        ("dim 20h", ("dimanche", 20, 0)),
        ("vendredi 18h30", ("vendredi", 18, 30)),
        ("MARDI 9:05", ("mardi", 9, 5)),
    ],
)
def test_parse_weekly(spec: str, expected: tuple[str, int, int]) -> None:
    assert schedule.parse_weekly(spec) == expected


@pytest.mark.parametrize("spec", ["lu 7h", "lundi", "lundi 25:00", "demain 7h"])
def test_parse_weekly_rejects(spec: str) -> None:
    with pytest.raises(schedule.ScheduleError):
        schedule.parse_weekly(spec)


def test_summary_install_and_uninstall(home: Path, fake_system: FakeSystem) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["summary", "--install", "lundi 07:00"])
    assert result.exit_code == 0, result.output
    line = schedule.find_entry(fake_system.crontab or "", schedule.SUMMARY_MARKER)
    assert line is not None
    assert line.startswith("0 7 * * 1 ")
    assert "summary --send --quiet" in line
    runner.invoke(app, ["watch", "--install"])  # les deux planifications cohabitent
    assert schedule.find_entry(fake_system.crontab or "", schedule.SUMMARY_MARKER)
    assert runner.invoke(app, ["summary", "--uninstall"]).exit_code == 0
    assert schedule.find_entry(fake_system.crontab or "", schedule.SUMMARY_MARKER) is None
    assert schedule.find_entry(fake_system.crontab or "")  # watch toujours là
    assert runner.invoke(app, ["summary", "--install", "bientôt"]).exit_code == 1


def test_summary_content_and_send(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze(monkeypatch, FIRST - timedelta(days=1))
    events = copy.deepcopy(fixture("getEvents"))
    events["result"][0]["is_participant_subscribed"] = True
    steps = copy.deepcopy(fixture("getNextProjectSteps"))
    steps["result"][0]["psp_limit_date"] = int((FIRST + timedelta(days=3)).timestamp() * 1000)
    state = home / "xdg_state_home" / "myges" / "watch-jdupont.json"
    state.parent.mkdir(parents=True)
    recent = int((FIRST - timedelta(days=2)).timestamp() * 1000)
    state.write_text(
        json.dumps(
            {
                "snapshot": {},
                "history": [
                    {
                        "at": recent,
                        "kind": "file",
                        "text": "Nouveau support en Réseaux : chap2.pdf",
                    },
                    {"at": 0, "kind": "grade", "text": "trop ancien"},
                ],
            }
        )
    )
    with respx.mock(assert_all_called=False) as router:
        ntfy = router.post(NTFY).mock(return_value=httpx.Response(200))
        mock_api(router, fixture("getGrades"))
        router.get(f"{API_URL}/me/events").mock(return_value=httpx.Response(200, json=events))
        router.get(f"{API_URL}/me/nextProjectSteps").mock(
            return_value=httpx.Response(200, json=steps)
        )
        result = CliRunner().invoke(app, ["summary", "--send"])
    assert result.exit_code == 0, result.output
    body = ntfy.calls.last.request.content.decode()
    assert "📅 3 cours" in body
    assert "T1 - Réseaux avancés (#2) · B12 (CAMPUS1)" in body
    assert "📌 Échéances" in body
    assert "🎟️ Événements où tu es inscrit" in body
    assert "chap2.pdf" in body
    assert "trop ancien" not in body


def test_serve_unknown_event_is_404() -> None:
    def missing(event_id: int) -> str:
        raise LookupError(f"événement {event_id} introuvable sur myGES")

    server = make_server("127.0.0.1", 0, "t", missing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        response = httpx.post(
            f"http://127.0.0.1:{server.server_address[1]}/events/1/subscribe",
            headers={"Authorization": "Bearer t"},
        )
    finally:
        server.shutdown()
        server.server_close()
    assert response.status_code == 404
    assert "introuvable" in response.json()["error"]
