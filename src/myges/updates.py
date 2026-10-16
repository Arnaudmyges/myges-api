"""Signaler qu'une version plus récente de myges-api est publiée sur PyPI.

pipx ne prévient jamais : sans ce rappel, une installation reste figée. La
vérification a lieu au plus une fois par jour (résultat mis en cache), avec un
délai court : hors ligne, elle échoue en silence.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

PYPI_URL = "https://pypi.org/pypi/myges-api/json"
CHECK_EVERY = timedelta(days=1)
TIMEOUT = 1.5


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "myges" / "update-check.json"


def _parse(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return _parse(candidate) > _parse(current)


def _fetch_latest() -> str | None:
    response = httpx.get(PYPI_URL, timeout=TIMEOUT)
    response.raise_for_status()
    return str(response.json()["info"]["version"])


def newer_version(
    current: str,
    *,
    path: Path | None = None,
    now: datetime | None = None,
    fetch: Callable[[], str | None] = _fetch_latest,
) -> str | None:
    """Version plus récente publiée, ou None (à jour, hors ligne, ou déjà vérifié)."""
    path = path or cache_path()
    now = now or datetime.now(timezone.utc)
    latest: str | None = None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(cached["checked_at"])
        if now - checked < CHECK_EVERY:
            latest = cached.get("latest")
            return latest if latest and is_newer(latest, current) else None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        latest = fetch()
    except (httpx.HTTPError, ValueError, KeyError):
        latest = None  # hors ligne : on réessaiera demain, sans bruit
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"checked_at": now.isoformat(), "latest": latest}), encoding="utf-8"
        )
    except OSError:
        pass
    return latest if latest and is_newer(latest, current) else None
