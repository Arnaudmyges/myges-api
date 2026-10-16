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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

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
    cancelled: list[int] = field(default_factory=list)
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


def _next_opening(pending: dict[int, CampusEvent], now: datetime) -> datetime:
    """Prochaine heure d'ouverture parmi les événements restants (``now`` si déjà ouvert)."""
    return max(min((_opening(e) or now for e in pending.values()), default=now), now)


def subscribe_at_opening(
    client: MyGES,
    event_ids: Iterable[int],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
    tz: tzinfo | None = None,
    wanted: Callable[[set[int]], set[int]] | None = None,
) -> Outcome:
    """Inscrit à chaque événement dès que possible ; bloque jusqu'à la fin.

    Écriture côté myGES : le client doit avoir été créé avec ``allow_writes=True``.
    ``wanted`` (facultatif) filtre, avant chaque ouverture, les événements toujours voulus :
    c'est ainsi qu'une pré-inscription annulée entre-temps est abandonnée.
    """
    client._check_write("subscribe_at_opening")
    outcome = Outcome()
    requested = list(dict.fromkeys(event_ids))
    known = {e.event_id: e for e in client.events()}
    pending: dict[int, CampusEvent] = {}
    for event_id in requested:
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

    # Une « vague » par heure d'ouverture : deux événements ouvrant à 12:00 et 14:00 sont
    # tentés chacun à leur heure, avec chacun sa fenêtre de RETRY_FOR.
    while pending:
        opening = _next_opening(pending, server_now())
        if opening - WAKE_BEFORE > server_now():
            shown = opening.astimezone(tz).strftime("%d/%m %H:%M:%S")
            log(f"Ouverture des inscriptions : {shown} ; en attente.")
            _sleep_until(opening - REFRESH_BEFORE, server_now, sleep)
            # L'école a pu déplacer l'ouverture depuis le lancement : on relit avant d'agir.
            fresh = {e.event_id: e for e in client.events()}
            for event_id in list(pending):
                event = fresh.get(event_id)
                if event is None:
                    continue
                if event.is_participant_subscribed:  # inscrit entre-temps (site, appli)
                    del pending[event_id]
                else:
                    pending[event_id] = event
            if wanted is not None:
                keep = wanted(set(pending))
                for event_id in [i for i in pending if i not in keep]:
                    log(f"{event_id} : pré-inscription annulée, abandon.")
                    outcome.cancelled.append(event_id)
                    del pending[event_id]
            if not pending:
                break
            moved = _next_opening(pending, server_now())
            if moved != opening:
                log("Heure d'ouverture modifiée par l'école : on suit la nouvelle.")
                continue
            if client._can_login():
                client.login()  # session neuve pour ne pas expirer au mauvais moment
            _sleep_until(opening - WAKE_BEFORE, server_now, sleep)

        wave = [i for i, e in pending.items() if (_opening(e) or opening) <= opening]
        deadline = max(opening, server_now()) + RETRY_FOR
        tries: dict[int, int] = {}
        while wave and server_now() < deadline:
            # Tous les événements de la vague en même temps : une requête lente (serveur
            # saturé à l'ouverture) ne doit pas retarder les autres.
            def post(event_id: int) -> httpx.Response:
                return client._send("POST", f"/me/events/{event_id}/subscribe")

            with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                responses = list(zip(wave, pool.map(post, wave), strict=True))
            for event_id, response in responses:
                body = response.text[:300]
                reason = _reason(body)
                outcome.attempts.append(Attempt(server_now(), event_id, response.status_code, body))
                tries[event_id] = tries.get(event_id, 0) + 1
                closes = pending[event_id].end_subscription_date
                if response.is_success or reason == "ALREADY_SUBSCRIBED":
                    log(f"✓ {event_id} : inscription acceptée (HTTP {response.status_code})")
                    wave.remove(event_id)
                elif reason == "EVENT_CLOSED" and closes is not None and closes <= server_now():
                    # Fermeture passée : inutile d'insister (cas d'une fermeture égale à
                    # l'ouverture, saisie par l'école).
                    log(f"✗ {event_id} : inscriptions closes côté myGES (fermeture {closes:%H:%M})")
                    outcome.failed[event_id] = "inscriptions closes par myGES dès l'ouverture"
                    wave.remove(event_id)
                elif tries[event_id] == 1:
                    log(f"… {event_id} : HTTP {response.status_code}, on réessaie ({reason})")
            if wave:
                sleep(RETRY_EVERY)
        for event_id in [i for i, e in pending.items() if (_opening(e) or opening) <= opening]:
            del pending[event_id]  # réussi ou abandonné : la vérification finale tranche

    # Vérification indépendante : la réponse HTTP ne suffit pas à conclure.
    confirmed = {e.event_id for e in client.events() if e.is_participant_subscribed}
    for event_id in requested:
        if event_id in outcome.already or event_id in outcome.failed:
            continue
        if event_id in outcome.cancelled:
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
