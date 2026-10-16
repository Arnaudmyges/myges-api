"""Inscription aux événements dès l'ouverture (horloge simulée : aucun test n'attend)."""

from __future__ import annotations

import copy
import os
import subprocess
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import fixture
from myges import MemoryTokenStore, MyGES, WriteNotAllowedError, pending
from myges.auth import AUTH_URL
from myges.cli import app
from myges.client import API_URL
from myges.registration import subscribe_at_opening

OPENING = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)  # 12:00 à Paris
LOCATION = "comreseaugesskolae:/oauth2redirect#access_token={}&expires_in=560063"


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class FakeSchool:
    """myGES simulé : événements avec leur propre ouverture, état d'inscription réel."""

    def __init__(self, clock: FakeClock, server_ahead: timedelta = timedelta(0)) -> None:
        self.clock = clock
        self.server_ahead = server_ahead
        self.events: dict[int, dict[str, Any]] = {}
        self.attempts: dict[int, list[datetime]] = {}
        self.listings = 0
        self.on_listing: Any = None  # rappel(n° de lecture) pour modifier les événements

    def now(self) -> datetime:
        return self.clock() + self.server_ahead

    def add(
        self,
        event_id: int,
        opens: datetime,
        *,
        closes: datetime | None = None,
        subscribed: bool = False,
        accept: bool = True,
    ) -> None:
        self.events[event_id] = {
            "opens": opens,
            "closes": closes or opens + timedelta(days=3),
            "subscribed": subscribed,
            "accept": accept,
        }

    def listing(self, request: httpx.Request) -> httpx.Response:
        self.listings += 1
        if self.on_listing:
            self.on_listing(self.listings)
        template = fixture("getEvents")["result"][0]
        result = []
        for event_id, e in self.events.items():
            item = copy.deepcopy(template)
            item.update(
                event_id=event_id,
                start_subscription_date=int(e["opens"].timestamp() * 1000),
                end_subscription_date=int(e["closes"].timestamp() * 1000),
                is_participant_subscribed=e["subscribed"],
            )
            result.append(item)
        return httpx.Response(200, json={"response_code": 0, "version": "1", "result": result})

    def subscribe(self, request: httpx.Request, event_id: str) -> httpx.Response:
        event = self.events[int(event_id)]
        self.attempts.setdefault(int(event_id), []).append(self.now())
        if self.now() < event["opens"] or not event["accept"]:
            message = "Complet" if not event["accept"] else "Inscriptions pas encore ouvertes"
            return httpx.Response(
                400, json={"response_code": 1, "result": {"status": 400, "message": message}}
            )
        event["subscribed"] = True
        return httpx.Response(200, json={"response_code": 0, "result": None})

    def mount(self, router: respx.MockRouter) -> respx.Route:
        auth = router.get(AUTH_URL).mock(
            side_effect=lambda req: httpx.Response(302, headers={"Location": LOCATION.format("t")})
        )
        router.get(f"{API_URL}/me/events").mock(side_effect=self.listing)
        router.get(f"{API_URL}/me/years").mock(
            side_effect=lambda req: httpx.Response(
                200,
                json=fixture("getYears"),
                headers={"Date": format_datetime(self.now(), usegmt=True)},
            )
        )
        router.post(url__regex=rf"{API_URL}/me/events/(?P<event_id>\d+)/subscribe").mock(
            side_effect=self.subscribe
        )
        return auth

    def run(self, router: respx.MockRouter, *ids: int) -> tuple[Any, respx.Route]:
        auth = self.mount(router)
        with MyGES("jdupont", "secret", token_store=MemoryTokenStore(), allow_writes=True) as ges:
            outcome = subscribe_at_opening(
                ges, ids, clock=self.clock, sleep=self.clock.sleep, log=lambda m: None
            )
        return outcome, auth


def test_waits_for_opening_then_subscribes() -> None:
    clock = FakeClock(OPENING - timedelta(minutes=45))
    school = FakeSchool(clock)
    school.add(4501, OPENING)
    with respx.mock() as router:
        outcome, auth = school.run(router, 4501)
    assert outcome.subscribed == [4501]
    tries = school.attempts[4501]
    assert OPENING - timedelta(seconds=3) <= tries[0] <= OPENING  # pas de tentative trop tôt
    assert len(tries) >= 2  # refusée juste avant l'heure, acceptée ensuite
    assert auth.call_count == 2  # connexion initiale + session renouvelée avant l'ouverture
    assert max(clock.slept) <= 30  # sommeil découpé, pas une seule longue attente


def test_follows_server_clock_not_local_clock() -> None:
    # Machine en retard de 40 s : sans correction, la 1re tentative partirait 40 s trop tard.
    clock = FakeClock(OPENING - timedelta(minutes=10))
    school = FakeSchool(clock, server_ahead=timedelta(seconds=40))
    school.add(4501, OPENING)
    with respx.mock() as router:
        outcome, _ = school.run(router, 4501)
    assert outcome.subscribed == [4501]
    assert school.attempts[4501][0] <= OPENING


def test_already_subscribed_sends_nothing() -> None:
    clock = FakeClock(OPENING)
    school = FakeSchool(clock)
    school.add(4501, OPENING, subscribed=True)
    with respx.mock(assert_all_called=False) as router:
        outcome, _ = school.run(router, 4501)
    assert outcome.already == [4501]
    assert 4501 not in school.attempts


def test_closed_registration_is_reported_without_retrying() -> None:
    clock = FakeClock(OPENING + timedelta(days=5))
    school = FakeSchool(clock)
    school.add(4501, OPENING, closes=OPENING + timedelta(days=1))
    with respx.mock(assert_all_called=False) as router:
        outcome, _ = school.run(router, 4501)
    assert outcome.failed == {4501: "inscriptions closes"}
    assert 4501 not in school.attempts


def test_gives_up_after_retry_window_and_reports_last_reason() -> None:
    clock = FakeClock(OPENING)
    school = FakeSchool(clock)
    school.add(4501, OPENING, accept=False)
    with respx.mock(assert_all_called=False) as router:
        outcome, _ = school.run(router, 4501)
    assert outcome.failed == {4501: "HTTP 400 : Complet"}
    assert clock.now - OPENING <= timedelta(minutes=3, seconds=5)


def test_each_event_is_tried_at_its_own_opening() -> None:
    # 12:00 et 14:00 : le second ne doit pas être abandonné avant même d'avoir ouvert.
    clock = FakeClock(OPENING - timedelta(minutes=30))
    school = FakeSchool(clock)
    school.add(4501, OPENING)
    school.add(4502, OPENING + timedelta(hours=2))
    with respx.mock() as router:
        outcome, auth = school.run(router, 4501, 4502)
    assert sorted(outcome.subscribed) == [4501, 4502]
    later = OPENING + timedelta(hours=2)
    assert later - timedelta(seconds=3) <= school.attempts[4502][0] <= later
    assert auth.call_count == 3  # session renouvelée avant chaque ouverture


def test_follows_an_opening_moved_after_launch() -> None:
    clock = FakeClock(OPENING - timedelta(hours=1))
    school = FakeSchool(clock)
    school.add(4501, OPENING)
    moved = OPENING + timedelta(minutes=30)

    def school_moves_it(listing: int) -> None:
        if listing == 2:  # relecture juste avant l'ancienne heure
            school.events[4501]["opens"] = moved

    school.on_listing = school_moves_it
    with respx.mock() as router:
        outcome, _ = school.run(router, 4501)
    assert outcome.subscribed == [4501]
    assert school.attempts[4501][0] >= moved - timedelta(seconds=3)  # pas de rafale à 12:00


def test_requires_explicit_write_permission() -> None:
    with MyGES(token="t") as ges, pytest.raises(WriteNotAllowedError):
        subscribe_at_opening(ges, [4501])


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("4525", 4525),
        ("https://myges.fr/common/event/4525", 4525),
        ("myges.fr/common/event/4529/", 4529),
    ],
)
def test_event_reference_parsing(ref: str, expected: int) -> None:
    from myges.cli import _event_id

    assert _event_id(ref) == expected


# ---------------------------------------------------------------- Pré-inscriptions


def test_cancelled_pre_registration_is_dropped_before_opening() -> None:
    clock = FakeClock(OPENING - timedelta(minutes=20))
    school = FakeSchool(clock)
    school.add(4501, OPENING)
    with respx.mock(assert_all_called=False) as router:
        school.mount(router)
        with MyGES("jdupont", "secret", token_store=MemoryTokenStore(), allow_writes=True) as ges:
            outcome = subscribe_at_opening(
                ges,
                [4501],
                clock=clock,
                sleep=clock.sleep,
                log=lambda m: None,
                wanted=lambda ids: set(),  # annulée entre-temps
            )
    assert outcome.cancelled == [4501]
    assert outcome.failed == {}
    assert 4501 not in school.attempts


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MYGES_TOKEN", "tok")
    monkeypatch.delenv("MYGES_USERNAME", raising=False)
    return tmp_path


def test_registry_roundtrip(state: Path) -> None:
    now = OPENING.isoformat()
    pending.register([pending.Pending(1, now, 111, now), pending.Pending(2, now, 111, now)])
    pending.register([pending.Pending(2, now, 222, now)])  # remplacé par événement
    assert {(p.event_id, p.pid) for p in pending.load()} == {(1, 111), (2, 222)}
    assert pending.still_wanted({1, 2}, 111) == {1}
    assert pending.remove({2}, pid=111) == []  # pas à ce processus
    assert pending.remove({2}) == [2]


def test_alive() -> None:
    assert pending.alive(os.getpid()) is True
    dead = subprocess.Popen(["true"])
    dead.wait()
    assert pending.alive(dead.pid) is False


def test_events_listing_shows_pre_registrations(state: Path) -> None:
    dead = subprocess.Popen(["true"])
    dead.wait()
    now = OPENING.isoformat()
    payload = fixture("getEvents")
    future = int((datetime.now(timezone.utc) + timedelta(days=5)).timestamp() * 1000)
    second = copy.deepcopy(payload["result"][0])
    second["event_id"] = 4502
    payload["result"][0]["event_date"] = second["event_date"] = future
    payload["result"].append(second)
    pending.register(
        [pending.Pending(4501, now, os.getpid(), now), pending.Pending(4502, now, dead.pid, now)]
    )
    with respx.mock() as router:
        router.get(f"{API_URL}/me/events").mock(return_value=httpx.Response(200, json=payload))
        result = CliRunner().invoke(app, ["events"], env={"COLUMNS": "200"})
        listed = CliRunner().invoke(app, ["events", "pending"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "⏳ pré-inscrit" in result.output
    assert "⚠ interrompu" in result.output
    assert "⏳ en attente" in listed.output
    assert "relancer subscribe" in listed.output


def test_cancel_stops_an_idle_waiting_process(state: Path) -> None:
    waiting = subprocess.Popen(["sleep", "60"])
    now = OPENING.isoformat()
    pending.register([pending.Pending(4501, now, waiting.pid, now)])
    result = CliRunner().invoke(app, ["events", "cancel", "https://myges.fr/common/event/4501"])
    assert result.exit_code == 0, result.output
    assert "Pré-inscription annulée : 4501" in result.output
    assert waiting.wait(timeout=5) != 0  # arrêté (SIGTERM)
    assert pending.load() == []


def test_background_returns_immediately(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []

    class FakeChild:
        pid = 424242

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            spawned.append(command)
            assert kwargs.get("start_new_session") is True
            now = OPENING.isoformat()
            pending.register([pending.Pending(4525, now, self.pid, now)])

        def poll(self) -> None:
            return None

    monkeypatch.setattr("subprocess.Popen", FakeChild)
    result = CliRunner().invoke(app, ["events", "subscribe", "4525", "-b"])
    assert result.exit_code == 0, result.output
    assert "Pré-inscription enregistrée" in result.output
    assert spawned[0][-3:] == ["events", "subscribe", "4525"]  # sans -b : pas de boucle
