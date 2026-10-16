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


def grades_section(grades: Sequence[Grade]) -> dict[str, Any]:
    return {
        str(g.rc_id): {
            "course": g.course,
            "grades": list(g.grades),
            "exam": g.exam,
            "average": g.average,
        }
        for g in grades
    }


def absences_section(absences: Sequence[Absence]) -> list[str]:
    return sorted(f"{_ms(a.date)}|{a.course_name}|{int(a.justified)}" for a in absences)


def files_section(files: Mapping[int, tuple[str, Sequence[CourseFile]]]) -> dict[str, Any]:
    return {
        str(rc_id): {"course": name, "items": {str(f.oc_id): f.filename for f in items}}
        for rc_id, (name, items) in files.items()
    }


def agenda_section(agenda: Sequence[AgendaEvent], now: datetime) -> dict[str, Any]:
    horizon = now + AGENDA_HORIZON
    return {
        str(e.reservation_id): {
            "name": e.name,
            "start": _ms(e.start_date),
            "end": _ms(e.end_date),
            "room": e.location or e.modality or "",
        }
        for e in agenda
        if now <= e.start_date <= horizon
    }


def snapshot(
    grades: Sequence[Grade],
    absences: Sequence[Absence],
    files: Mapping[int, tuple[str, Sequence[CourseFile]]],
    agenda: Sequence[AgendaEvent],
    now: datetime,
) -> Snapshot:
    """Instantané complet. ``files`` : ``{rc_id: (nom de la matière, supports)}``."""
    return {
        "taken_at": _ms(now),
        "grades": grades_section(grades),
        "absences": absences_section(absences),
        "files": files_section(files),
        "agenda": agenda_section(agenda, now),
    }


EMPTY_CONFIRM = 3  # passages consécutifs avant de croire qu'une section s'est vidée


def stabilize(
    previous: Snapshot, current: Snapshot, streaks: dict[str, int]
) -> tuple[Snapshot, dict[str, int]]:
    """Ignore une section qui se vide brusquement tant que ce n'est pas confirmé.

    myGES renvoie parfois une liste vide (ou un 204) le temps d'un incident : sans cette
    précaution, tout ce qu'elle contenait serait signalé « nouveau » au passage suivant.
    Une section réellement vidée (vacances, nouvelle année) est acceptée après
    ``EMPTY_CONFIRM`` passages consécutifs.
    """
    result = {**current}
    streaks = dict(streaks)
    pairs: list[tuple[str, Any, Any, Any]] = [
        (key, previous.get(key), current.get(key), None) for key in ("grades", "absences", "agenda")
    ]
    old_files, new_files = previous.get("files", {}), dict(current.get("files", {}))
    for rc_id, old in old_files.items():
        pairs.append(
            (f"files:{rc_id}", old.get("items"), (new_files.get(rc_id) or {}).get("items"), rc_id)
        )
    for key, before, after, rc_id in pairs:
        if before and not after:
            streaks[key] = streaks.get(key, 0) + 1
            if streaks[key] < EMPTY_CONFIRM:
                if rc_id is None:
                    result[key] = before
                else:
                    new_files[rc_id] = old_files[rc_id]
                continue
        streaks.pop(key, None)
    result["files"] = new_files
    return result, streaks


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
