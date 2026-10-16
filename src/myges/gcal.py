"""Synchronisation de l'agenda myGES vers un agenda Google Calendar dédié.

Authentification par **compte de service** : on crée un agenda dédié dans Google Calendar,
on le partage avec l'adresse du compte de service (« Apporter des modifications aux
événements »), et la synchro tourne sans intervention. Un flux OAuth utilisateur ne
conviendrait pas : tant que l'écran de consentement du projet Google est en « Test »,
ses refresh tokens expirent au bout de 7 jours.

Chaque créneau a un identifiant d'événement stable dérivé de ``reservation_id`` : relancer
la synchro met à jour au lieu de dupliquer. Seuls les événements créés par cette synchro
(propriété privée ``source=myges-api``) sont modifiés ou supprimés.

Nécessite l'extra ``gcal`` : ``pip install 'myges-api[gcal]'``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .models import AgendaEvent

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
API = "https://www.googleapis.com/calendar/v3"
SOURCE = "myges-api"
TIMEZONE = "Europe/Paris"

# Champs comparés pour décider s'il faut mettre à jour un événement existant.
_COMPARED = ("summary", "location", "description")


class GoogleCalendarError(RuntimeError):
    """Erreur renvoyée par l'API Google Calendar."""


def event_id(reservation_id: int) -> str:
    """Google impose ``[a-v0-9]{5,1024}`` (base32hex) : « myges » serait refusé (le y)."""
    return f"ges{reservation_id}"


def build_event(event: AgendaEvent) -> dict[str, Any]:
    details = [
        f"Intervenant : {event.teacher}" if event.teacher else "",
        f"Modalité : {event.modality}" if event.modality else "",
        f"Type : {event.type}" if event.type else "",
        "Synchronisé depuis myGES (myges-api).",
    ]
    return {
        "summary": (event.name or "Cours").strip(),
        "location": event.location or "",
        "description": "\n".join(d for d in details if d),
        "start": {"dateTime": event.start_date.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": event.end_date.isoformat(), "timeZone": TIMEZONE},
        "status": "confirmed",
        "extendedProperties": {
            "private": {"source": SOURCE, "reservation_id": str(event.reservation_id)}
        },
    }


def _same(existing: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
    if existing.get("status") == "cancelled":
        return False
    if any((existing.get(k) or "") != (wanted.get(k) or "") for k in _COMPARED):
        return False
    for bound in ("start", "end"):
        have = existing.get(bound, {}).get("dateTime")
        want = wanted[bound]["dateTime"]
        if have is None or datetime.fromisoformat(have) != datetime.fromisoformat(want):
            return False
    return True


class GoogleCalendar:
    """Accès minimal à un agenda Google Calendar via un compte de service (REST + httpx)."""

    def __init__(
        self,
        key_file: Path | str,
        calendar_id: str,
        *,
        http: httpx.Client | None = None,
        credentials: Any = None,
    ) -> None:
        self.calendar_id = calendar_id
        self._http = http or httpx.Client(timeout=20.0)
        self._credentials = credentials or _load_credentials(Path(key_file))

    @property
    def service_account_email(self) -> str:
        return str(getattr(self._credentials, "service_account_email", ""))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GoogleCalendar:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ HTTP

    def _token(self) -> str:
        if not self._credentials.valid:
            self._credentials.refresh(_HttpxAuthRequest(self._http))
        return str(self._credentials.token)

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{API}/calendars/{quote(self.calendar_id, safe='')}{path}"
        response = self._http.request(
            method, url, headers={"Authorization": f"Bearer {self._token()}"}, **kwargs
        )
        return response

    @staticmethod
    def _raise(response: httpx.Response, action: str) -> None:
        if response.is_success:
            return
        try:
            reason = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            reason = response.text[:200]
        hint = ""
        if response.status_code in (403, 404):
            hint = (
                " — l'agenda est-il bien partagé avec le compte de service, avec le droit "
                "« Apporter des modifications aux événements » ?"
            )
        raise GoogleCalendarError(f"{action} : HTTP {response.status_code} {reason}{hint}")

    # ------------------------------------------------------------ Opérations

    def check_access(self) -> None:
        """Vérifie que l'agenda est accessible en écriture (lève sinon)."""
        response = self._call("GET", "/events", params={"maxResults": 1})
        self._raise(response, "Accès à l'agenda")

    def managed_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "privateExtendedProperty": f"source={SOURCE}",
            "singleEvents": "true",
            "showDeleted": "true",
            "maxResults": 2500,
        }
        while True:
            response = self._call("GET", "/events", params=params)
            self._raise(response, "Lecture de l'agenda")
            payload = response.json()
            events.extend(payload.get("items", []))
            if not payload.get("nextPageToken"):
                return events
            params["pageToken"] = payload["nextPageToken"]

    def put(self, eid: str, body: Mapping[str, Any]) -> None:
        """Met à jour l'événement (même supprimé), ou le crée s'il n'a jamais existé."""
        response = self._call("PUT", f"/events/{eid}", json=body)
        if response.status_code == 404:
            response = self._call("POST", "/events", json={**body, "id": eid})
        self._raise(response, f"Écriture de l'événement {eid}")

    def delete(self, eid: str) -> None:
        response = self._call("DELETE", f"/events/{eid}")
        if response.status_code != 410:  # déjà supprimé
            self._raise(response, f"Suppression de l'événement {eid}")


def _load_credentials(key_file: Path) -> Any:
    try:
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover
        raise GoogleCalendarError(
            "La synchro Google Calendar nécessite l'extra « gcal » : "
            "pipx inject myges-api google-auth (ou pip install 'myges-api[gcal]')."
        ) from exc
    info = json.loads(key_file.read_text(encoding="utf-8"))
    if info.get("type") != "service_account":
        raise GoogleCalendarError(f"{key_file} n'est pas une clé de compte de service.")
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


class _HttpxAuthRequest:
    """Adaptateur ``google.auth.transport.Request`` sur httpx (évite la dépendance requests)."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        from google.auth import exceptions

        try:
            response = self._http.request(
                method, url, content=body, headers=dict(headers or {}), timeout=timeout or 20.0
            )
        except httpx.HTTPError as exc:
            raise exceptions.TransportError(exc) from exc
        return _AuthResponse(response)


class _AuthResponse:
    def __init__(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self.headers = response.headers
        self.data = response.content


# ---------------------------------------------------------------- Synchro


@dataclass
class SyncReport:
    created_or_updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    deleted: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"{len(self.created_or_updated)} créé(s) ou mis à jour, {self.unchanged} inchangé(s), "
            f"{len(self.deleted)} supprimé(s)"
        )


def plan(
    events: Sequence[AgendaEvent],
    existing: Iterable[Mapping[str, Any]],
    now: datetime,
) -> tuple[dict[str, dict[str, Any]], list[str], int]:
    """(à écrire {id: corps}, ids à supprimer, nombre d'inchangés). Sans effet de bord."""
    wanted = {event_id(e.reservation_id): build_event(e) for e in events}
    current = {e["id"]: e for e in existing}
    to_write = {eid: body for eid, body in wanted.items() if not _same(current.get(eid, {}), body)}
    to_delete = [
        eid
        for eid, ev in current.items()
        if eid not in wanted
        and ev.get("status") != "cancelled"
        and _starts_after(ev, now)  # on ne réécrit pas le passé
    ]
    return to_write, to_delete, len(wanted) - len(to_write)


def _starts_after(event: Mapping[str, Any], now: datetime) -> bool:
    start = event.get("start", {}).get("dateTime")
    return start is not None and datetime.fromisoformat(start) >= now


def sync(
    calendar: GoogleCalendar,
    events: Sequence[AgendaEvent],
    now: datetime | None = None,
    days: int = 30,
    *,
    dry_run: bool = False,
) -> SyncReport:
    """Aligne l'agenda Google Calendar sur ``events`` (créneaux de ``now`` à ``now + days``)."""
    now = now or datetime.now(timezone.utc)
    existing = calendar.managed_events(now - timedelta(days=1), now + timedelta(days=days))
    to_write, to_delete, unchanged = plan(events, existing, now)
    report = SyncReport(unchanged=unchanged)
    for eid, body in to_write.items():
        if not dry_run:
            calendar.put(eid, body)
        report.created_or_updated.append(f"{body['summary']} ({body['start']['dateTime']})")
    titles = {e["id"]: e.get("summary", e["id"]) for e in existing}
    for eid in to_delete:
        if not dry_run:
            calendar.delete(eid)
        report.deleted.append(str(titles.get(eid, eid)))
    return report
