"""Rappel de mise à jour : au plus une vérification par jour, silencieux hors ligne."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from myges.updates import is_newer, newer_version

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("0.4.0", "0.3.1", True),
        ("0.3.10", "0.3.9", True),
        ("0.3.1", "0.3.1", False),
        ("0.3.0", "0.3.1", False),
        ("1.0.0rc1", "0.9.0", True),
    ],
)
def test_is_newer(candidate: str, current: str, expected: bool) -> None:
    assert is_newer(candidate, current) is expected


def test_checks_at_most_once_a_day(tmp_path: Path) -> None:
    calls: list[int] = []

    def fetch() -> str:
        calls.append(1)
        return "0.4.0"

    path = tmp_path / "check.json"
    assert newer_version("0.3.1", path=path, now=NOW, fetch=fetch) == "0.4.0"
    assert newer_version("0.3.1", path=path, now=NOW + timedelta(hours=5), fetch=fetch) == "0.4.0"
    assert len(calls) == 1  # résultat du cache
    newer_version("0.3.1", path=path, now=NOW + timedelta(days=2), fetch=fetch)
    assert len(calls) == 2


def test_up_to_date_and_offline_are_silent(tmp_path: Path) -> None:
    assert newer_version("0.4.0", path=tmp_path / "a.json", now=NOW, fetch=lambda: "0.4.0") is None

    def offline() -> str:
        raise httpx.ConnectError("hors ligne")

    assert newer_version("0.3.1", path=tmp_path / "b.json", now=NOW, fetch=offline) is None
