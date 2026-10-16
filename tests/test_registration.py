"""Inscription aux événements dès l'ouverture (horloge simulée : aucun test n'attend)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
import respx

from conftest import fixture
from myges import MemoryTokenStore, MyGES, WriteNotAllowedError
from myges.auth import AUTH_URL
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


def events_payload(subscribed: bool, opens: datetime, closes: datetime) -> dict[str, Any]:
    body = copy.deepcopy(fixture("getEvents"))
    event = body["result"][0]  # event_id 4501
    event["start_subscription_date"] = int(opens.timestamp() * 1000)
    event["end_subscription_date"] = int(closes.timestamp() * 1000)
    event["is_participant_subscribed"] = subscribed
    return body


def run(
    router: respx.MockRouter,
    clock: FakeClock,
    *,
    subscribed_after: bool = True,
    already: bool = False,
    closes: datetime | None = None,
    server_ahead: timedelta = timedelta(0),
) -> tuple[Any, respx.Route, respx.Route]:
    closes = closes or OPENING + timedelta(days=3)
    router.get(AUTH_URL).mock(
        side_effect=lambda req: httpx.Response(302, headers={"Location": LOCATION.format("t")})
    )
    router.get(f"{API_URL}/me/events").mock(
        side_effect=[
            httpx.Response(200, json=events_payload(already, OPENING, closes)),
            httpx.Response(200, json=events_payload(subscribed_after, OPENING, closes)),
        ]
    )
    router.get(f"{API_URL}/me/years").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json=fixture("getYears"),
            headers={"Date": format_datetime(clock() + server_ahead, usegmt=True)},
        )
    )
    attempts: list[datetime] = []

    def answer(request: httpx.Request) -> httpx.Response:
        attempts.append(clock() + server_ahead)
        if clock() + server_ahead < OPENING:
            return httpx.Response(
                400,
                json={
                    "response_code": 1,
                    "result": {"status": 400, "message": "Inscriptions pas encore ouvertes"},
                },
            )
        return httpx.Response(200, json={"response_code": 0, "result": None})

    post = router.post(f"{API_URL}/me/events/4501/subscribe").mock(side_effect=answer)
    auth = router.routes[0]
    with MyGES("jdupont", "secret", token_store=MemoryTokenStore(), allow_writes=True) as ges:
        outcome = subscribe_at_opening(
            ges, [4501], clock=clock, sleep=clock.sleep, log=lambda m: None
        )
    outcome.first_attempt = attempts[0] if attempts else None  # type: ignore[attr-defined]
    return outcome, post, auth


def test_waits_for_opening_then_subscribes() -> None:
    clock = FakeClock(OPENING - timedelta(minutes=45))
    with respx.mock() as router:
        outcome, post, auth = run(router, clock)
    assert outcome.subscribed == [4501]
    first = outcome.first_attempt
    assert OPENING - timedelta(seconds=3) <= first <= OPENING  # pas de tentative trop tôt
    assert post.call_count >= 2  # refusée juste avant l'heure, acceptée ensuite
    assert auth.call_count == 2  # connexion initiale + session renouvelée avant l'ouverture
    assert max(clock.slept) <= 30  # sommeil découpé, pas une seule longue attente


def test_follows_server_clock_not_local_clock() -> None:
    # Machine en retard de 40 s : sans correction, la 1re tentative partirait 40 s trop tard.
    clock = FakeClock(OPENING - timedelta(minutes=10))
    with respx.mock() as router:
        outcome, _, _ = run(router, clock, server_ahead=timedelta(seconds=40))
    assert outcome.subscribed == [4501]
    assert outcome.first_attempt <= OPENING


def test_already_subscribed_sends_nothing() -> None:
    clock = FakeClock(OPENING)
    with respx.mock(assert_all_called=False) as router:
        outcome, post, _ = run(router, clock, already=True)
    assert outcome.already == [4501]
    assert not post.called


def test_closed_registration_is_reported_without_retrying() -> None:
    clock = FakeClock(OPENING + timedelta(days=5))
    with respx.mock(assert_all_called=False) as router:
        outcome, post, _ = run(router, clock, closes=OPENING + timedelta(days=1))
    assert outcome.failed == {4501: "inscriptions closes"}
    assert not post.called


def test_gives_up_after_retry_window_and_reports_last_reason() -> None:
    clock = FakeClock(OPENING)
    with respx.mock(assert_all_called=False) as router:
        router.get(AUTH_URL).mock(
            return_value=httpx.Response(302, headers={"Location": LOCATION.format("t")})
        )
        router.get(f"{API_URL}/me/events").mock(
            return_value=httpx.Response(
                200, json=events_payload(False, OPENING, OPENING + timedelta(days=3))
            )
        )
        router.get(f"{API_URL}/me/years").mock(
            return_value=httpx.Response(200, json=fixture("getYears"))
        )
        router.post(f"{API_URL}/me/events/4501/subscribe").mock(
            return_value=httpx.Response(
                400, json={"response_code": 1, "result": {"status": 400, "message": "Complet"}}
            )
        )
        with MyGES("jdupont", "secret", token_store=MemoryTokenStore(), allow_writes=True) as ges:
            outcome = subscribe_at_opening(
                ges, [4501], clock=clock, sleep=clock.sleep, log=lambda m: None
            )
    assert outcome.failed == {4501: "HTTP 400 : Complet"}
    assert clock.now - OPENING <= timedelta(minutes=3, seconds=5)


def test_requires_explicit_write_permission() -> None:
    with MyGES(token="t") as ges, pytest.raises(WriteNotAllowedError):
        subscribe_at_opening(ges, [4501])
