"""Envoi des nouveautés vers ntfy, Discord ou e-mail.

Chaque canal est décrit par un dictionnaire sérialisable (stocké dans la config de la
CLI) et envoyé par :func:`send`. Les messages contiennent des notes et des absences :
ce sont des données personnelles, d'où le sujet ntfy aléatoire par défaut.
"""

from __future__ import annotations

import secrets
import smtplib
import ssl
from collections.abc import Sequence
from email.message import EmailMessage
from typing import Any

import httpx

from ._version import __version__
from .watch import Change

Channel = dict[str, Any]

NTFY_SERVER = "https://ntfy.sh"
DISCORD_LIMIT = 2000  # caractères par message Discord

_ICONS = {
    "grade": "📊",
    "absence": "🚫",
    "file": "📄",
    "agenda_added": "📅",
    "agenda_removed": "❌",
    "agenda_moved": "🔁",
    "info": "ℹ️",
}


def random_topic() -> str:
    """Sujet ntfy difficile à deviner : sur ntfy.sh, connaître le sujet suffit à le lire."""
    return f"myges-{secrets.token_urlsafe(12)}"


def describe(channel: Channel) -> str:
    kind = channel["type"]
    if kind == "ntfy":
        return f"ntfy  {channel['server'].rstrip('/')}/{channel['topic']}"
    if kind == "discord":
        return f"discord  …{channel['webhook'][-8:]}"
    if kind == "email":
        return f"email  {channel['to']} via {channel['host']}:{channel['port']}"
    return str(kind)


def format_changes(changes: Sequence[Change]) -> tuple[str, str]:
    """(titre, corps) d'une notification regroupant plusieurs changements."""
    count = len(changes)
    title = f"myGES : {count} nouveauté{'s' if count > 1 else ''}"
    body = "\n".join(f"{_ICONS.get(c.kind, '•')} {c.text}" for c in changes)
    return title, body


def send(channel: Channel, title: str, body: str, *, timeout: float = 15.0) -> None:
    """Envoie une notification ; lève une exception si le canal la refuse."""
    kind = channel["type"]
    if kind == "ntfy":
        _send_ntfy(channel, title, body, timeout)
    elif kind == "discord":
        _send_discord(channel, title, body, timeout)
    elif kind == "email":
        _send_email(channel, title, body, timeout)
    else:
        raise ValueError(f"type de canal inconnu : {kind}")


def _send_ntfy(channel: Channel, title: str, body: str, timeout: float) -> None:
    url = f"{channel['server'].rstrip('/')}/{channel['topic']}"
    headers = {
        # En-têtes HTTP : ntfy décode le RFC 2047 pour les caractères non ASCII.
        "Title": _rfc2047(title),
        "Tags": "school",
        "User-Agent": f"myges-api/{__version__}",
    }
    if channel.get("token"):
        headers["Authorization"] = f"Bearer {channel['token']}"
    _check(httpx.post(url, content=body.encode(), headers=headers, timeout=timeout))


class DeliveryError(RuntimeError):
    """Le service a refusé la notification ; le message reprend son explication."""


def _check(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        detail = response.json()
        reason = detail.get("error") or detail.get("message") or response.text
    except ValueError:
        reason = response.text
    raise DeliveryError(f"HTTP {response.status_code} : {str(reason).strip()[:200]}")


def _rfc2047(text: str) -> str:
    if text.isascii():
        return text
    from email.header import Header

    return Header(text, "utf-8").encode()


def _send_discord(channel: Channel, title: str, body: str, timeout: float) -> None:
    content = f"**{title}**\n{body}"
    # Découpe en plusieurs messages si besoin, sans couper une ligne.
    chunks: list[str] = []
    current = ""
    for line in content.splitlines():
        if current and len(current) + len(line) + 1 > DISCORD_LIMIT:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line[:DISCORD_LIMIT]
    chunks.append(current)
    for chunk in chunks:
        _check(
            httpx.post(
                channel["webhook"],
                json={"content": chunk, "username": "myGES"},
                timeout=timeout,
            )
        )


def _send_email(channel: Channel, title: str, body: str, timeout: float) -> None:
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = channel.get("sender") or channel.get("user") or channel["to"]
    message["To"] = channel["to"]
    message.set_content(body)
    port = int(channel["port"])
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(channel["host"], port, timeout=timeout, context=context) as smtp:
            _login_and_send(smtp, channel, message)
    else:
        with smtplib.SMTP(channel["host"], port, timeout=timeout) as smtp:
            smtp.starttls(context=context)
            _login_and_send(smtp, channel, message)


def _login_and_send(smtp: smtplib.SMTP, channel: Channel, message: EmailMessage) -> None:
    if channel.get("user"):
        smtp.login(channel["user"], channel.get("password") or "")
    smtp.send_message(message)
