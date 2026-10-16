from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from myges.client import API_URL

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
def no_shell_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """La complétion n'est jamais installée dans le vrai shell de la machine de test."""
    monkeypatch.setattr("myges.cli._install_completion", lambda: True)
    monkeypatch.setenv("MYGES_NO_UPDATE_CHECK", "1")  # pas d'appel à PyPI pendant les tests


@pytest.fixture(autouse=True)
def fake_system(monkeypatch: pytest.MonkeyPatch) -> FakeSystem:
    system = FakeSystem()
    monkeypatch.setattr("myges.schedule._run", system.run)
    monkeypatch.setattr("myges.schedule.shutil.which", lambda name: f"/usr/bin/{name}")
    return system


# ---------------------------------------------------------------- myges watch


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("MYGES_TOKEN", "MYGES_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MYGES_USERNAME", "jdupont")
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    config = tmp_path / "xdg_config_home" / "myges" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"notify": [{"type": "ntfy", "server": "https://ntfy.sh", "topic": "myges-t"}]})
    )
    # Token valide en cache : pas d'authentification nécessaire.
    cache = tmp_path / "xdg_cache_home" / "myges" / "tokens.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "jdupont": {
                    "access_token": "tok",
                    "expires_in": 600000,
                    "obtained_at": datetime.now(timezone.utc).isoformat(),
                }
            }
        )
    )
    return tmp_path


def mock_api(router: respx.MockRouter, grades: dict[str, Any]) -> None:
    def get(path: str, response: httpx.Response) -> None:
        router.get(f"{API_URL}{path}").mock(return_value=response)

    get("/me/years", httpx.Response(200, json=fixture("getYears")))
    get("/me/2026/courses", httpx.Response(200, json=fixture("getCourses")))
    get("/me/2026/grades", httpx.Response(200, json=grades))
    get("/me/2026/absences", httpx.Response(204))
    get("/me/500001/files", httpx.Response(200, json=fixture("getCourseFiles")))
    get("/me/agenda", httpx.Response(200, json=fixture("getAgenda")))
