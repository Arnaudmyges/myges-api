"""Préparer un e-mail à des intervenants ou des étudiants, sans rien envoyer soi-même.

Les adresses myGES (``@myges.fr``, ``@myskolae.fr``) sont des boîtes Microsoft 365 :
le lien ouvre un brouillon dans Outlook sur le web, déjà connecté au compte de l'école,
destinataires remplis. L'envoi reste un clic de l'utilisateur, dans Outlook.

``mailto:`` est l'alternative pour le client de messagerie par défaut (Outlook bureau,
Thunderbird…).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import webbrowser
from collections.abc import Sequence
from typing import Literal
from urllib.parse import quote

OUTLOOK_COMPOSE = "https://outlook.office.com/mail/deeplink/compose"

Client = Literal["outlook", "mailto"]


def _q(value: str) -> str:
    # %20 plutôt que « + » : Outlook et les clients mailto n'interprètent pas tous « + ».
    return quote(value, safe="")


def compose_url(
    to: Sequence[str], subject: str = "", body: str = "", *, client: Client = "outlook"
) -> str:
    """Lien qui ouvre un brouillon adressé à ``to`` (virgules entre les adresses)."""
    recipients = ",".join(quote(address, safe="@") for address in to)
    extra = [f"subject={_q(subject)}"] if subject else []
    extra += [f"body={_q(body)}"] if body else []
    if client == "mailto":
        return f"mailto:{recipients}" + (f"?{'&'.join(extra)}" if extra else "")
    return f"{OUTLOOK_COMPOSE}?" + "&".join([f"to={recipients}", *extra])


def _is_wsl() -> bool:
    return "microsoft" in platform.release().lower()


def open_url(url: str) -> bool:
    """Ouvrir ``url`` dans le navigateur ; False si la machine n'en a pas (SSH, VPS…).

    Sous Linux sans affichage, ``webbrowser`` se rabattrait sur un navigateur texte
    (lynx, w3m) qui prendrait le terminal : on s'abstient et l'appelant affiche le lien.
    """
    system = platform.system()
    if system == "Linux":
        if _is_wsl():
            opener = shutil.which("wslview")
            if opener is None:
                return False
            return subprocess.run([opener, url], check=False).returncode == 0
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
    try:
        return webbrowser.open(url)
    except webbrowser.Error:
        return False
