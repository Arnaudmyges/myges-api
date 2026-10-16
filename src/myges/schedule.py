"""Planification de ``myges watch`` : crontab (Linux, macOS, WSL) ou Planificateur de
tâches (Windows natif).

La ligne crontab est repérée par un commentaire marqueur : l'installation remplace une
ancienne ligne au lieu d'en ajouter une seconde, et la désinstallation ne touche à rien
d'autre dans la crontab de l'utilisateur.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

MARKER = "# myges-api: watch"
DEFAULT_EVERY = 5  # minutes : les supports de cours doivent arriver vite
MIN_EVERY = 5  # en dessous, la charge sur myGES deviendrait déraisonnable
WINDOWS_TASK = "myges-watch"


class ScheduleError(RuntimeError):
    """Planification impossible ; le message dit quoi faire."""


# ---------------------------------------------------------------- Texte crontab (pur)


def cron_line(executable: str, every: int) -> str:
    quoted = f'"{executable}"' if " " in executable else executable
    return f"*/{every} * * * * {quoted} watch --quiet {MARKER}"


SUMMARY_MARKER = "# myges-api: summary"
SUMMARY_TASK = "myges-summary"
DAYS = {  # jour cron (0 = dimanche) et code schtasks
    "lundi": (1, "MON"), "mardi": (2, "TUE"), "mercredi": (3, "WED"), "jeudi": (4, "THU"),
    "vendredi": (5, "FRI"), "samedi": (6, "SAT"), "dimanche": (0, "SUN"),
}  # fmt: skip


def with_entry(crontab: str, line: str, marker: str = MARKER) -> str:
    """Crontab avec notre ligne (remplace l'éventuelle précédente)."""
    base = without_entry(crontab, marker).rstrip("\n")
    return f"{base}\n{line}\n" if base else f"{line}\n"


def without_entry(crontab: str, marker: str = MARKER) -> str:
    kept = [entry for entry in crontab.splitlines() if marker not in entry]
    return "\n".join(kept) + ("\n" if kept else "")


def find_entry(crontab: str, marker: str = MARKER) -> str | None:
    return next((entry for entry in crontab.splitlines() if marker in entry), None)


def parse_weekly(spec: str) -> tuple[str, int, int]:
    """« lundi 07:00 », « dim 20h », « vendredi 18h30 » → (jour, heure, minute)."""
    import re

    match = re.fullmatch(r"\s*([a-zA-Zéû]+)\s+(\d{1,2})\s*(?:[:h]\s*(\d{2})?)?\s*", spec)
    if match is None:
        raise ScheduleError(f'Moment invalide : « {spec} ». Exemple : "lundi 07:00".')
    word, hour, minute = match.group(1).lower(), int(match.group(2)), int(match.group(3) or 0)
    day = next((d for d in DAYS if d.startswith(word) and len(word) >= 3), None)
    if day is None or hour > 23 or minute > 59:
        raise ScheduleError(f'Moment invalide : « {spec} ». Exemple : "lundi 07:00".')
    return day, hour, minute


def install_weekly_summary(spec: str) -> str:
    """Planifie `myges summary --send` chaque semaine ; renvoie une description."""
    day, hour, minute = parse_weekly(spec)
    exe = executable()
    shown = f"chaque {day} à {hour:02d}:{minute:02d}"
    if is_windows():
        result = _run(
            ["schtasks", "/Create", "/F", "/SC", "WEEKLY", "/D", DAYS[day][1],
             "/ST", f"{hour:02d}:{minute:02d}", "/TN", SUMMARY_TASK,
             "/TR", f'"{exe}" summary --send --quiet']
        )  # fmt: skip
        if result.returncode != 0:
            raise ScheduleError(f"schtasks a échoué : {result.stderr.strip() or result.stdout}")
        return shown
    quoted = f'"{exe}"' if " " in exe else exe
    line = f"{minute} {hour} * * {DAYS[day][0]} {quoted} summary --send --quiet {SUMMARY_MARKER}"
    write_crontab(with_entry(read_crontab(), line, SUMMARY_MARKER))
    return shown


def uninstall_weekly_summary() -> bool:
    if is_windows():
        return _run(["schtasks", "/Delete", "/F", "/TN", SUMMARY_TASK]).returncode == 0
    current = read_crontab()
    if find_entry(current, SUMMARY_MARKER) is None:
        return False
    write_crontab(without_entry(current, SUMMARY_MARKER))
    return True


# ---------------------------------------------------------------- Système


def executable() -> str:
    """Chemin absolu de la commande ``myges`` (cron n'a pas le PATH de l'utilisateur)."""
    found = shutil.which("myges")
    if found:
        return str(Path(found).resolve())
    return str(Path(sys.argv[0]).resolve())


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)


def read_crontab() -> str:
    if shutil.which("crontab") is None:
        raise ScheduleError(
            "La commande crontab est introuvable. Sous Debian/Ubuntu/WSL : sudo apt install cron"
        )
    result = _run(["crontab", "-l"])
    if result.returncode != 0:
        if "no crontab" in result.stderr.lower():
            return ""
        raise ScheduleError(f"crontab -l a échoué : {result.stderr.strip()}")
    return result.stdout


def write_crontab(content: str) -> None:
    result = _run(["crontab", "-"], stdin=content)
    if result.returncode != 0:
        raise ScheduleError(f"Écriture de la crontab impossible : {result.stderr.strip()}")


def is_windows() -> bool:
    return os.name == "nt"


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def cron_running() -> bool | None:
    """True / False si on sait le dire (Linux), None sinon (macOS : launchd s'en charge)."""
    if sys.platform != "linux" or shutil.which("pgrep") is None:
        return None
    return _run(["pgrep", "^(cron|crond)$"]).returncode == 0


def install(every: int) -> str:
    """Installe la planification ; renvoie une description de ce qui a été fait."""
    if not MIN_EVERY <= every <= 59:
        raise ScheduleError(f"--every doit être compris entre {MIN_EVERY} et 59 minutes.")
    exe = executable()
    if is_windows():
        command = f'"{exe}" watch --quiet'
        result = _run(
            ["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(every),
             "/TN", WINDOWS_TASK, "/TR", command]
        )  # fmt: skip
        if result.returncode != 0:
            raise ScheduleError(f"schtasks a échoué : {result.stderr.strip() or result.stdout}")
        return f"tâche planifiée Windows « {WINDOWS_TASK} », toutes les {every} min"
    line = cron_line(exe, every)
    write_crontab(with_entry(read_crontab(), line))
    return f"crontab : {line}"


def uninstall() -> bool:
    """Retire la planification ; False s'il n'y avait rien à retirer."""
    if is_windows():
        return _run(["schtasks", "/Delete", "/F", "/TN", WINDOWS_TASK]).returncode == 0
    current = read_crontab()
    if find_entry(current) is None:
        return False
    write_crontab(without_entry(current))
    return True


def installed() -> bool:
    """Vrai si une planification existe (faux en cas de doute : sert seulement d'indice)."""
    try:
        if is_windows():
            return _run(["schtasks", "/Query", "/TN", WINDOWS_TASK]).returncode == 0
        return shutil.which("crontab") is not None and find_entry(read_crontab()) is not None
    except (OSError, ScheduleError):
        return False
