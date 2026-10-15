"""Export de l'agenda au format iCalendar (RFC 5545), sans dépendance externe."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from .models import AgendaEvent

PRODID = "-//myges-api//FR"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """Replie à 75 octets comme l'exige la RFC, sans couper un caractère UTF-8."""
    out: list[str] = []
    current, size = "", 0
    for char in line:
        width = len(char.encode())
        if size + width > (75 if not out else 74):
            out.append(current)
            current, size = "", 0
        current += char
        size += width
    out.append(current)
    return [out[0]] + [" " + part for part in out[1:]]


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def to_ics(events: Iterable[AgendaEvent], calendar_name: str = "myGES") -> str:
    """Calendrier iCalendar. L'UID dérive de ``reservation_id`` : stable d'un export à l'autre."""
    now = _stamp(datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
    ]
    for event in events:
        description = [
            part
            for part in (
                event.teacher and f"Intervenant : {event.teacher}",
                event.modality and f"Modalité : {event.modality}",
                event.type and f"Type : {event.type}",
            )
            if part
        ]
        lines += [
            "BEGIN:VEVENT",
            f"UID:{event.reservation_id}@myges",
            f"DTSTAMP:{now}",
            f"DTSTART:{_stamp(event.start_date)}",
            f"DTEND:{_stamp(event.end_date)}",
            f"SUMMARY:{_escape(event.name)}",
        ]
        if event.location:
            lines.append(f"LOCATION:{_escape(event.location)}")
        if description:
            lines.append(f"DESCRIPTION:{_escape(chr(10).join(description))}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(folded for line in lines for folded in _fold(line)) + "\r\n"
