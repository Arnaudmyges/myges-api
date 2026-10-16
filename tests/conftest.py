from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSystem:
    """Crontab et schtasks simulés : aucun test ne touche à la vraie planification."""

    def __init__(self) -> None:
        self.crontab: str | None = None  # None = pas de crontab
        self.calls: list[list[str]] = []
        self.cron_running = True

    def run(self, args: list[str], stdin: str | None = None) -> Any:
        import subprocess

        self.calls.append(args)
        if args[:2] == ["crontab", "-l"]:
            if self.crontab is None:
                return subprocess.CompletedProcess(args, 1, "", "no crontab for user\n")
            return subprocess.CompletedProcess(args, 0, self.crontab, "")
        if args[:2] == ["crontab", "-"]:
            self.crontab = stdin
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args, 0 if self.cron_running else 1, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture(autouse=True)
def fake_system(monkeypatch: pytest.MonkeyPatch) -> FakeSystem:
    system = FakeSystem()
    monkeypatch.setattr("myges.schedule._run", system.run)
    monkeypatch.setattr("myges.schedule.shutil.which", lambda name: f"/usr/bin/{name}")
    return system
