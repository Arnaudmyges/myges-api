"""Synchro Google Calendar, contre une API Google simulée."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import fixture
from myges import models as m
from myges.cli import app
from myges.client import API_URL
from myges.gcal import API, GoogleCalendar, GoogleCalendarError, build_event, event_id, plan, sync

CAL = "abc123@group.calendar.google.com"
EVENTS_URL = f"{API}/calendars/abc123%40group.calendar.google.com/events"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


class FakeCredentials:
    valid = True
    token = "google-token"
    service_account_email = "sync@projet.iam.gserviceaccount.com"


def agenda() -> list[m.AgendaEvent]:
    return [m.AgendaEvent.model_validate(e) for e in fixture("getAgenda")["result"]]


def calendar(http: httpx.Client | None = None) -> GoogleCalendar:
    return GoogleCalendar("unused.json", CAL, http=http, credentials=FakeCredentials())


def test_event_ids_are_valid_base32hex() -> None:
    eid = event_id(3000001)
    assert set(eid) <= set("abcdefghijklmnopqrstuv0123456789")
    assert len(eid) >= 5


def test_plan_creates_skips_unchanged_and_deletes_future_only() -> None:
    events = agenda()
    unchanged = {"id": event_id(events[0].reservation_id), **build_event(events[0])}
    future_orphan = {
        "id": "ges999",
        "summary": "Cours annulé",
        "start": {"dateTime": (NOW + timedelta(days=2)).isoformat()},
    }
    past_orphan = {
        "id": "ges998",
        "summary": "Cours passé",
        "start": {"dateTime": (NOW - timedelta(hours=3)).isoformat()},
    }
    to_write, to_delete, same = plan(events, [unchanged, future_orphan, past_orphan], NOW)
    assert set(to_write) == {event_id(e.reservation_id) for e in events[1:]}
    assert to_delete == ["ges999"]
    assert same == 1


def test_plan_updates_moved_event() -> None:
    events = agenda()
    moved = {"id": event_id(events[0].reservation_id), **build_event(events[0])}
    moved["start"] = {"dateTime": (events[0].start_date + timedelta(hours=1)).isoformat()}
    to_write, _, _ = plan(events[:1], [moved], NOW)
    assert list(to_write) == [moved["id"]]


def test_sync_against_api() -> None:
    events = agenda()
    with respx.mock() as google:
        google.get(EVENTS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "ges999",
                            "summary": "Annulé",
                            "start": {"dateTime": (NOW + timedelta(days=1)).isoformat()},
                        }
                    ]
                },
            )
        )
        put = google.put(url__startswith=f"{EVENTS_URL}/").mock(
            side_effect=[httpx.Response(404), httpx.Response(200), httpx.Response(200)]
        )
        post = google.post(EVENTS_URL).mock(return_value=httpx.Response(200))
        delete = google.delete(f"{EVENTS_URL}/ges999").mock(return_value=httpx.Response(204))
        report = sync(calendar(), events, NOW, days=30)
    assert put.call_count == 3
    assert post.call_count == 1  # le 1er n'existait pas : créé avec son id
    assert json.loads(post.calls.last.request.content)["id"] == event_id(events[0].reservation_id)
    assert delete.called
    assert report.summary == "3 créé(s) ou mis à jour, 0 inchangé(s), 1 supprimé(s)"
    assert put.calls.last.request.headers["Authorization"] == "Bearer google-token"


def test_dry_run_writes_nothing() -> None:
    with respx.mock() as google:
        google.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"items": []}))
        report = sync(calendar(), agenda(), NOW, dry_run=True)
    assert len(report.created_or_updated) == 3  # seule la lecture a eu lieu


def test_already_deleted_event_is_fine() -> None:
    with respx.mock() as google:
        google.delete(f"{EVENTS_URL}/ges1").mock(return_value=httpx.Response(410))
        calendar().delete("ges1")


def test_unshared_calendar_gives_actionable_error() -> None:
    error = {"error": {"code": 404, "message": "Not Found"}}
    with respx.mock() as google:
        google.get(EVENTS_URL).mock(return_value=httpx.Response(404, json=error))
        with pytest.raises(GoogleCalendarError, match="partagé avec le compte de service"):
            calendar().check_access()


def test_managed_events_paginates() -> None:
    with respx.mock() as google:
        route = google.get(EVENTS_URL).mock(
            side_effect=[
                httpx.Response(200, json={"items": [{"id": "ges1"}], "nextPageToken": "p2"}),
                httpx.Response(200, json={"items": [{"id": "ges2"}]}),
            ]
        )
        found = calendar().managed_events(NOW, NOW + timedelta(days=1))
    assert [e["id"] for e in found] == ["ges1", "ges2"]
    assert route.calls.last.request.url.params["pageToken"] == "p2"
    assert route.calls[0].request.url.params["privateExtendedProperty"] == "source=myges-api"


def test_cli_setup_and_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYGES_TOKEN", "tok")
    monkeypatch.delenv("MYGES_USERNAME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("myges.gcal._load_credentials", lambda path: FakeCredentials())
    key = tmp_path / "cle.json"
    key.write_text(json.dumps({"type": "service_account", "client_email": "x"}))
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as router:
        router.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"items": []}))
        router.put(url__startswith=f"{EVENTS_URL}/").mock(return_value=httpx.Response(200))
        router.get(f"{API_URL}/me/agenda").mock(
            return_value=httpx.Response(200, json=fixture("getAgenda"))
        )
        setup = runner.invoke(app, ["gcal", "setup", str(key), CAL])
        assert setup.exit_code == 0, setup.output
        result = runner.invoke(app, ["gcal", "sync"])
    assert result.exit_code == 0, result.output
    assert "3 créé(s) ou mis à jour" in result.output
    copied = tmp_path / "config" / "myges" / "gcal-service-account.json"
    assert (copied.stat().st_mode & 0o777) == 0o600


def test_cli_sync_requires_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["gcal", "sync"])
    assert result.exit_code == 2
    assert "non configuré" in result.output
