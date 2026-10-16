"""myges watch : rythme des vérifications et fiabilité (envois, incidents, verrou)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import fixture, mock_api
from myges import pending, watch
from myges.cli import app
from myges.client import API_URL

NTFY = "https://ntfy.sh/myges-t"


def state_file(home: Path) -> Path:
    return home / "xdg_state_home" / "myges" / "watch-jdupont.json"


def load_state(home: Path) -> dict[str, Any]:
    return json.loads(state_file(home).read_text())


def edit_state(home: Path, change: Any) -> None:
    data = load_state(home)
    change(data)
    state_file(home).write_text(json.dumps(data))


def files_with(extra: bool) -> dict[str, Any]:
    payload = copy.deepcopy(fixture("getCourseFiles"))
    if extra:
        new = copy.deepcopy(payload["result"][0])
        new.update(oc_id=610099, label="Chapitre 2")
        payload["result"].append(new)
    return payload


def baseline(router: respx.MockRouter) -> None:
    mock_api(router, fixture("getGrades"))
    assert CliRunner().invoke(app, ["watch"]).exit_code == 0


def paths(router: respx.MockRouter) -> list[str]:
    return [call.request.url.path for call in router.calls]


# ---------------------------------------------------------------- Rythme


def test_scheduled_pass_checks_only_course_files(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(NTFY).mock(return_value=httpx.Response(200))
        baseline(router)
        already = len(router.calls)
        result = CliRunner().invoke(app, ["watch", "--quiet"])
        called = paths(router)[already:]
    assert result.exit_code == 0, result.output
    assert "/me/500001/files" in called
    assert not any(p.endswith(("/grades", "/absences", "/agenda", "/years")) for p in called)


def test_slow_sections_are_checked_every_30_minutes(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(NTFY).mock(return_value=httpx.Response(200))
        baseline(router)
        edit_state(home, lambda s: s["checked"].update(slow=0))  # dernier contrôle « ancien »
        already = len(router.calls)
        assert CliRunner().invoke(app, ["watch", "--quiet"]).exit_code == 0
        called = paths(router)[already:]
    assert any(p.endswith("/grades") for p in called)


def test_new_course_file_is_notified_on_a_quick_pass(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        ntfy = router.post(NTFY).mock(return_value=httpx.Response(200))
        baseline(router)
        router.get(f"{API_URL}/me/500001/files").mock(
            return_value=httpx.Response(200, json=files_with(extra=True))
        )
        result = CliRunner().invoke(app, ["watch", "--quiet"])
    assert result.exit_code == 0, result.output
    assert "Chapitre 2" in ntfy.calls.last.request.content.decode()


# ---------------------------------------------------------------- Fiabilité


def test_failed_delivery_is_retried_next_pass(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        ntfy = router.post(NTFY).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
        baseline(router)
        router.get(f"{API_URL}/me/500001/files").mock(
            return_value=httpx.Response(200, json=files_with(extra=True))
        )
        first = CliRunner().invoke(app, ["watch", "--quiet"])
        assert first.exit_code == 1  # canal en échec : signalé
        assert load_state(home)["outbox"]
        second = CliRunner().invoke(app, ["watch", "--quiet"])  # rien de nouveau entre-temps
    assert second.exit_code == 0, second.output
    assert ntfy.call_count == 2
    assert "Chapitre 2" in ntfy.calls.last.request.content.decode()
    assert not load_state(home).get("outbox")


def test_transient_empty_answer_causes_no_false_alerts(home: Path) -> None:
    empty = {"response_code": 0, "version": "1", "result": [], "links": []}
    with respx.mock(assert_all_called=False) as router:
        ntfy = router.post(NTFY).mock(return_value=httpx.Response(200))
        baseline(router)
        router.get(f"{API_URL}/me/500001/files").mock(return_value=httpx.Response(200, json=empty))
        assert CliRunner().invoke(app, ["watch", "--quiet"]).exit_code == 0  # incident
        router.get(f"{API_URL}/me/500001/files").mock(
            return_value=httpx.Response(200, json=fixture("getCourseFiles"))
        )
        assert CliRunner().invoke(app, ["watch", "--quiet"]).exit_code == 0  # retour normal
    assert not ntfy.called  # rien n'a été présenté comme « nouveau »


def test_stabilize_accepts_a_confirmed_empty_section() -> None:
    previous = {"grades": {"1": {"course": "x"}}, "absences": [], "agenda": {}, "files": {}}
    current = {**previous, "grades": {}}
    streaks: dict[str, int] = {}
    for _ in range(watch.EMPTY_CONFIRM - 1):
        kept, streaks = watch.stabilize(previous, current, streaks)
        assert kept["grades"] == previous["grades"]
    accepted, streaks = watch.stabilize(previous, current, streaks)
    assert accepted["grades"] == {}
    assert streaks == {}


def test_overlapping_pass_does_nothing(home: Path) -> None:
    import fcntl

    with respx.mock(assert_all_called=False) as router:
        baseline(router)
        lock = state_file(home).with_suffix(".lock")
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            already = len(router.calls)
            result = CliRunner().invoke(app, ["watch", "--quiet"])
            assert len(router.calls) == already  # aucune requête : passage sauté
    assert result.exit_code == 0


def test_outage_alerts_once_then_announces_recovery(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        ntfy = router.post(NTFY).mock(return_value=httpx.Response(200))
        baseline(router)
        router.get(f"{API_URL}/me/2026/courses").mock(return_value=httpx.Response(503))
        assert CliRunner().invoke(app, ["watch", "--quiet"]).exit_code == 1
        three_hours_ago = int((datetime.now(timezone.utc) - timedelta(hours=3)).timestamp() * 1000)
        edit_state(home, lambda s: s["failures"].update(since=three_hours_ago))
        for _ in range(2):
            assert CliRunner().invoke(app, ["watch", "--quiet"]).exit_code == 1
        assert ntfy.call_count == 1
        assert "injoignable" in ntfy.calls.last.request.content.decode()
        router.get(f"{API_URL}/me/2026/courses").mock(
            return_value=httpx.Response(200, json=fixture("getCourses"))
        )
        assert CliRunner().invoke(app, ["watch", "--quiet"]).exit_code == 0
    assert ntfy.call_count == 2
    assert "de nouveau joignable" in ntfy.calls.last.request.content.decode()


def test_v1_state_is_migrated_without_false_alerts(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        ntfy = router.post(NTFY).mock(return_value=httpx.Response(200))
        baseline(router)
        snapshot = load_state(home)["snapshot"]
        state_file(home).write_text(json.dumps(snapshot))  # ancien format : l'instantané seul
        result = CliRunner().invoke(app, ["watch", "--quiet"])
    assert result.exit_code == 0, result.output
    assert not ntfy.called


def test_interrupted_pre_registration_is_relaunched(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    dead = subprocess.Popen(["true"])
    dead.wait()
    soon = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    pending.register(
        [pending.Pending(4525, soon, dead.pid, past), pending.Pending(4400, past, dead.pid, past)]
    )  # trop tard : ignorée
    relaunched: list[list[int]] = []
    monkeypatch.setattr("myges.cli._subscribe_in_background", relaunched.append)
    with respx.mock(assert_all_called=False) as router:
        router.post(NTFY).mock(return_value=httpx.Response(200))
        baseline(router)
        assert CliRunner().invoke(app, ["watch", "--quiet"]).exit_code == 0
    assert relaunched == [[4525]]  # relancée ; celle dont l'ouverture est passée, non


def test_status_summarizes_everything(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(NTFY).mock(return_value=httpx.Response(200))
        baseline(router)
    runner = CliRunner()
    assert runner.invoke(app, ["watch", "--install"]).exit_code == 0
    status = runner.invoke(app, ["watch", "--status"])
    assert status.exit_code == 0, status.output
    assert "actif" in status.output
    assert "toutes les 5 min" in status.output
    assert "ntfy" in status.output
    assert "Dernier passage" in status.output
    assert "mot de passe non enregistré" in status.output


def test_interval_below_minimum_is_refused(home: Path) -> None:
    result = CliRunner().invoke(app, ["watch", "--install", "--every", "2"])
    assert result.exit_code == 1
    assert "entre 5 et 59" in result.output
