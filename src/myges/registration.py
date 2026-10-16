"""Inscription à des événements dès l'ouverture des inscriptions.

Les places partent vite : on attend l'heure d'ouverture **selon l'horloge du serveur**
(en-tête ``Date`` de ses réponses, pas celle de la machine, qui peut dériver), on
renouvelle la session juste avant, puis on retente toutes les demi-secondes jusqu'à
l'acceptation ou l'expiration du délai. Chaque tentative est journalisée telle que reçue.

Le temps et l'attente sont injectables (``clock``, ``sleep``) : la logique se teste sans
attendre réellement.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import MyGES
    from .models import CampusEvent

WAKE_BEFORE = timedelta(seconds=2)  # premier essai juste avant l'heure annoncée
REFRESH_BEFORE = timedelta(seconds=60)  # session renouvelée une minute avant
RETRY_EVERY = 0.5  # secondes entre deux salves de tentatives
RETRY_FOR = timedelta(minutes=3)  # abandon si toujours refusé après ce délai


@dataclass
class Attempt:
    at: datetime
    event_id: int
    status: int
    body: str


@dataclass
class Outcome:
    subscribed: list[int] = field(default_factory=list)
    already: list[int] = field(default_factory=list)
    failed: dict[int, str] = field(default_factory=dict)
    attempts: list[Attempt] = field(default_factory=list)


def server_offset(client: MyGES, clock: Callable[[], datetime]) -> timedelta:
    """Avance du serveur sur la machine locale (positive si le serveur est en avance)."""
    before = clock()
    response = client._send("GET", "/me/years")
    after = clock()
    header = response.headers.get("date")
    if not header:
        return timedelta(0)
    server = parsedate_to_datetime(header)
    local = before + (after - before) / 2
    return server - local


def _opening(event: CampusEvent) -> datetime | None:
    return event.start_subscription_date


def subscribe_at_opening(
    client: MyGES,
    event_ids: Iterable[int],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> Outcome:
    """Inscrit à chaque événement dès que possible ; bloque jusqu'à la fin.

    Écriture côté myGES : le client doit avoir été créé avec ``allow_writes=True``.
    """
    client._check_write("subscribe_at_opening")
    outcome = Outcome()
    wanted = list(dict.fromkeys(event_ids))
    known = {e.event_id: e for e in client.events()}
    pending: dict[int, CampusEvent] = {}
    for event_id in wanted:
        event = known.get(event_id)
        if event is None:
            outcome.failed[event_id] = "événement introuvable dans /me/events"
        elif event.is_participant_subscribed:
            outcome.already.append(event_id)
        else:
            pending[event_id] = event
    if not pending:
        return outcome

    offset = server_offset(client, clock)
    if abs(offset) >= timedelta(seconds=2):
        log(f"Horloge locale décalée de {offset.total_seconds():+.0f} s : on suit le serveur.")

    def server_now() -> datetime:
        return clock() + offset

    for event_id, event in list(pending.items()):
        opens, closes = event.start_subscription_date, event.end_subscription_date
        # Fermeture égale à l'ouverture : saisie douteuse côté école, on tente quand même.
        if opens and closes and closes > opens and closes < server_now():
            outcome.failed[event_id] = "inscriptions closes"
            del pending[event_id]
    if not pending:
        return outcome

    openings = [o for o in (_opening(e) for e in pending.values()) if o is not None]
    start = min(openings, default=server_now())
    if start > server_now():
        log(f"Ouverture des inscriptions : {start.astimezone().strftime('%d/%m %H:%M:%S')}")
        _sleep_until(start - REFRESH_BEFORE, server_now, sleep)
        if client._can_login():
            client.login()  # session neuve pour ne pas expirer au mauvais moment
        _sleep_until(start - WAKE_BEFORE, server_now, sleep)

    deadline = max(start, server_now()) + RETRY_FOR
    while pending and server_now() < deadline:
        for event_id in list(pending):
            response = client._send("POST", f"/me/events/{event_id}/subscribe")
            body = response.text[:300]
            outcome.attempts.append(Attempt(server_now(), event_id, response.status_code, body))
            if response.is_success:
                log(f"✓ {event_id} : inscription acceptée (HTTP {response.status_code})")
                del pending[event_id]
            elif len([a for a in outcome.attempts if a.event_id == event_id]) == 1:
                log(f"… {event_id} : HTTP {response.status_code}, on réessaie ({_reason(body)})")
        if pending:
            sleep(RETRY_EVERY)

    # Vérification indépendante : la réponse HTTP ne suffit pas à conclure.
    confirmed = {e.event_id for e in client.events() if e.is_participant_subscribed}
    for event_id in wanted:
        if event_id in outcome.already or event_id in outcome.failed:
            continue
        if event_id in confirmed:
            outcome.subscribed.append(event_id)
        else:
            last = next((a for a in reversed(outcome.attempts) if a.event_id == event_id), None)
            outcome.failed[event_id] = (
                f"HTTP {last.status} : {_reason(last.body)}" if last else "aucune tentative"
            )
    return outcome


def _reason(body: str) -> str:
    import json

    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:120] or "réponse vide"
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, dict):
        return str(result.get("message") or result.get("error") or data)[:120]
    return str(data)[:120]


def _sleep_until(
    moment: datetime, now: Callable[[], datetime], sleep: Callable[[float], None]
) -> None:
    while True:
        remaining = (moment - now()).total_seconds()
        if remaining <= 0:
            return
        sleep(min(remaining, 30.0))
