"""Pré-inscriptions : inscriptions à des événements en attente de l'ouverture.

Chaque processus ``myges events subscribe`` qui attend une ouverture s'y déclare (pid,
événements, heure d'ouverture) et s'en retire en terminant. On peut ainsi les lister,
détecter ceux qu'un arrêt de la machine a interrompus, et les annuler : le processus relit
le registre avant d'agir et abandonne les événements qui n'y figurent plus.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Pending:
    event_id: int
    opens: str  # ISO 8601
    pid: int
    since: str  # ISO 8601

    @property
    def opens_at(self) -> datetime:
        return datetime.fromisoformat(self.opens)


def registry_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "myges" / "pending-events.json"


def load(path: Path | None = None) -> list[Pending]:
    try:
        raw = json.loads((path or registry_path()).read_text(encoding="utf-8"))
        return [Pending(**item) for item in raw]
    except (OSError, ValueError, TypeError):
        return []


def _save(entries: list[Pending], path: Path | None = None) -> None:
    target = path or registry_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps([asdict(e) for e in entries]), encoding="utf-8")
    tmp.replace(target)


def register(entries: list[Pending], path: Path | None = None) -> None:
    """Ajoute (ou remplace, par événement) des pré-inscriptions."""
    ids = {e.event_id for e in entries}
    _save([e for e in load(path) if e.event_id not in ids] + entries, path)


def remove(event_ids: set[int], pid: int | None = None, path: Path | None = None) -> list[int]:
    """Retire des pré-inscriptions (seulement celles de ``pid`` s'il est donné)."""
    current = load(path)
    removed = [e.event_id for e in current if e.event_id in event_ids and pid in (None, e.pid)]
    _save([e for e in current if e.event_id not in removed], path)
    return removed


def still_wanted(event_ids: set[int], pid: int, path: Path | None = None) -> set[int]:
    """Événements de ``event_ids`` toujours pré-inscrits par ``pid`` (non annulés)."""
    return {e.event_id for e in load(path) if e.pid == pid and e.event_id in event_ids}


def alive(pid: int) -> bool | None:
    """Le processus tourne-t-il encore ? None si on ne peut pas le savoir (Windows)."""
    if sys.platform == "win32":
        return None  # os.kill(pid, 0) terminerait le processus sous Windows
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
