"""myges watch --install / --uninstall (crontab simulée, voir conftest.FakeSystem)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FakeSystem
from myges import schedule
from myges.cli import app

OTHER = "0 5 * * * /home/me/backup.sh\n"


def test_install_keeps_other_entries_and_replaces_ours() -> None:
    first = schedule.with_entry(OTHER, schedule.cron_line("/x/myges", 30))
    again = schedule.with_entry(first, schedule.cron_line("/x/myges", 10))
    assert again.splitlines() == [
        "0 5 * * * /home/me/backup.sh",
        f"*/10 * * * * /x/myges watch --quiet {schedule.MARKER}",
    ]


def test_uninstall_removes_only_ours() -> None:
    crontab = schedule.with_entry(OTHER, schedule.cron_line("/x/myges", 30))
    assert schedule.without_entry(crontab) == OTHER
    assert schedule.without_entry(schedule.cron_line("/x/myges", 30)) == ""


def test_paths_with_spaces_are_quoted() -> None:
    assert '"/home/a b/myges" watch' in schedule.cron_line("/home/a b/myges", 30)


@pytest.fixture
def logged_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("MYGES_TOKEN", "MYGES_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MYGES_USERNAME", "jdupont")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_cli_install_warns_about_what_would_block_alerts(
    logged_in: Path, fake_system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_system.crontab = OTHER
    fake_system.cron_running = False
    monkeypatch.setattr("myges.schedule.sys.platform", "linux")
    monkeypatch.setattr("myges.schedule.is_wsl", lambda: True)
    result = CliRunner().invoke(app, ["watch", "--install", "--every", "15"])
    assert result.exit_code == 0, result.output
    assert fake_system.crontab is not None
    assert fake_system.crontab.startswith(OTHER)
    assert "*/15 * * * *" in fake_system.crontab
    assert "Aucun canal" in result.output
    assert "--save-password" in result.output
    assert "sudo service cron start" in result.output


def test_cli_install_is_quiet_when_everything_is_ready(
    logged_in: Path, fake_system: FakeSystem
) -> None:
    (logged_in / "myges").mkdir()
    (logged_in / "myges" / "config.json").write_text(
        json.dumps(
            {
                "username": "jdupont",
                "password": "pw",
                "notify": [{"type": "ntfy", "server": "https://ntfy.sh", "topic": "t"}],
            }
        )
    )
    result = CliRunner().invoke(app, ["watch", "--install"])
    assert result.exit_code == 0, result.output
    assert "!" not in result.output


def test_cli_uninstall(logged_in: Path, fake_system: FakeSystem) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["watch", "--install"]).exit_code == 0
    removed = runner.invoke(app, ["watch", "--uninstall"])
    assert "désactivées" in removed.output
    assert fake_system.crontab == ""
    assert "Rien à désactiver" in runner.invoke(app, ["watch", "--uninstall"]).output


def test_cli_install_requires_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MYGES_TOKEN", "MYGES_USERNAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert CliRunner().invoke(app, ["watch", "--install"]).exit_code == 2


def test_notify_add_suggests_install_until_done(logged_in: Path) -> None:
    runner = CliRunner()
    assert "myges watch --install" in runner.invoke(app, ["notify", "add", "ntfy"]).output
    runner.invoke(app, ["watch", "--install"])
    assert "myges watch --install" not in runner.invoke(app, ["notify", "add", "ntfy"]).output


def test_missing_crontab_command_is_explained(
    logged_in: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("myges.schedule.shutil.which", lambda name: None)
    result = CliRunner().invoke(app, ["watch", "--install"])
    assert result.exit_code == 1
    assert "apt install cron" in " ".join(result.output.split())


def test_windows_uses_task_scheduler(
    logged_in: Path, fake_system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("myges.schedule.is_windows", lambda: True)
    result = CliRunner().invoke(app, ["watch", "--install", "--every", "20"])
    assert result.exit_code == 0, result.output
    create = next(c for c in fake_system.calls if c[0] == "schtasks")
    assert create[:2] == ["schtasks", "/Create"]
    assert create[create.index("/MO") + 1] == "20"
    assert fake_system.crontab is None  # la crontab n'est pas utilisée


def test_install_preserves_blank_lines_and_comments() -> None:
    crontab = "# sauvegardes\n0 5 * * * backup.sh\n\n# autre\n0 6 * * * other.sh\n"
    installed = schedule.with_entry(crontab, schedule.cron_line("/x/myges", 30))
    assert installed.startswith(crontab)
    assert schedule.without_entry(installed) == crontab
