"""Détection des nouveautés entre deux passages de ``myges watch``.

Un *instantané* résume ce qui intéresse l'étudiant (notes, absences, supports de cours,
agenda proche) sous une forme JSON stable. :func:`diff` compare deux instantanés et
renvoie les changements à notifier. Aucune requête réseau ici : tout est testable sur
des données en mémoire.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import Absence, AgendaEvent, CourseFile, Grade

Snapshot = dict[str, Any]

AGENDA_HORIZON = timedelta(days=14)


@dataclass(frozen=True)
class Change:
    kind: str  # grade | absence | file | agenda_added | agenda_removed | agenda_moved
    text: str


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def _when(ms: int, tz: Any) -> str:
    moment = datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(tz)
    days = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
    return f"{days[moment.weekday()]} {moment:%d/%m %H:%M}"


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def snapshot(
    grades: Sequence[Grade],
    absences: Sequence[Absence],
    files: Mapping[int, tuple[str, Sequence[CourseFile]]],
    agenda: Sequence[AgendaEvent],
    now: datetime,
) -> Snapshot:
    """Instantané sérialisable. ``files`` : ``{rc_id: (nom de la matière, supports)}``."""
    horizon = now + AGENDA_HORIZON
    return {
        "taken_at": _ms(now),
        "grades": {
            str(g.rc_id): {
                "course": g.course,
                "grades": list(g.grades),
                "exam": g.exam,
                "average": g.average,
            }
            for g in grades
        },
        "absences": sorted(f"{_ms(a.date)}|{a.course_name}|{int(a.justified)}" for a in absences),
        "files": {
            str(rc_id): {"course": name, "items": {str(f.oc_id): f.filename for f in items}}
            for rc_id, (name, items) in files.items()
        },
        "agenda": {
            str(e.reservation_id): {
                "name": e.name,
                "start": _ms(e.start_date),
                "end": _ms(e.end_date),
                "room": e.location or e.modality or "",
            }
            for e in agenda
            if now <= e.start_date <= horizon
        },
    }


def diff(old: Snapshot, new: Snapshot, tz: Any = timezone.utc) -> list[Change]:
    """Changements de ``old`` vers ``new``, dans un ordre stable."""
    changes: list[Change] = []
    changes += _grade_changes(old.get("grades", {}), new.get("grades", {}))
    changes += _absence_changes(old.get("absences", []), new.get("absences", []), tz)
    changes += _file_changes(old.get("files", {}), new.get("files", {}))
    changes += _agenda_changes(old, new, tz)
    return changes


def _grade_changes(old: dict[str, Any], new: dict[str, Any]) -> Iterable[Change]:
    for rc_id, now in new.items():
        before = old.get(rc_id, {"grades": [], "exam": None, "average": None})
        added = Counter(now["grades"]) - Counter(before["grades"])
        for value in sorted(added.elements()):
            yield Change(
                "grade",
                f"Nouvelle note en {now['course']} : {_num(value)}"
                + (f" (moyenne {_num(now['average'])})" if now["average"] is not None else ""),
            )
        if now["exam"] is not None and now["exam"] != before["exam"]:
            yield Change("grade", f"Note d'examen en {now['course']} : {_num(now['exam'])}")
        elif now["average"] is not None and now["average"] != before["average"] and not added:
            yield Change(
                "grade",
                f"Moyenne modifiée en {now['course']} : "
                f"{_num(before['average'])} → {_num(now['average'])}",
            )


def _absence_changes(old: list[str], new: list[str], tz: Any) -> Iterable[Change]:
    for key in sorted(set(new) - set(old)):
        ms, course, justified = key.split("|", 2)
        state = "justifiée" if justified == "1" else "non justifiée"
        yield Change("absence", f"Absence {state} en {course} ({_when(int(ms), tz)})")


def _file_changes(old: dict[str, Any], new: dict[str, Any]) -> Iterable[Change]:
    for rc_id, now in new.items():
        known = old.get(rc_id, {}).get("items", {})
        for oc_id, filename in now["items"].items():
            if oc_id not in known:
                yield Change("file", f"Nouveau support en {now['course']} : {filename}")


def _agenda_changes(old: Snapshot, new: Snapshot, tz: Any) -> Iterable[Change]:
    before, after = old.get("agenda", {}), new.get("agenda", {})
    if not before:
        return
    # Un créneau qui sort de la fenêtre parce qu'il est passé n'est pas une annulation :
    # on ne compare que ce qui était encore à venir au moment du nouvel instantané.
    window_start = new.get("taken_at", 0)
    window_end = window_start + int(AGENDA_HORIZON.total_seconds() * 1000)
    for rid, event in after.items():
        if rid not in before:
            yield Change(
                "agenda_added",
                f"Cours ajouté : {event['name']}, {_when(event['start'], tz)} ({event['room']})",
            )
        else:
            was = before[rid]
            if (was["start"], was["end"], was["room"]) != (
                event["start"],
                event["end"],
                event["room"],
            ):
                yield Change(
                    "agenda_moved",
                    f"Cours modifié : {event['name']}, {_when(was['start'], tz)} ({was['room']})"
                    f" → {_when(event['start'], tz)} ({event['room']})",
                )
    for rid, was in before.items():
        if rid not in after and window_start <= was["start"] <= window_end:
            yield Change(
                "agenda_removed",
                f"Cours annulé : {was['name']}, {_when(was['start'], tz)}",
            )
