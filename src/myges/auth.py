"""Authentification Kordis et stockage du token.

Kordis détourne le flow OAuth 2.0 *implicit grant* : on envoie les identifiants en
Basic Auth à l'endpoint d'autorisation, qui répond par une redirection ``302`` vers
``comreseaugesskolae:/oauth2redirect#access_token=...``. Le token est lu dans le
fragment de l'en-tête ``Location``, sans suivre la redirection.

Le token vit ~6,5 jours et il n'existe pas de refresh token : à l'expiration il faut
se ré-authentifier, d'où le stockage des identifiants côté appelant (variables
d'environnement) et le cache du token (:class:`FileTokenStore`) pour ne pas se
ré-authentifier à chaque exécution.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

import httpx

from .errors import AuthenticationError, BlockedRequestError, InvalidCredentialsError

AUTH_URL = "https://authentication.kordis.fr/oauth/authorize"
AUTH_PARAMS = {"response_type": "token", "client_id": "skolae-app"}

# Marge avant expiration en dessous de laquelle on considère le token comme périmé.
EXPIRY_MARGIN = timedelta(hours=1)


@dataclass(frozen=True)
class Token:
    access_token: str
    expires_in: int
    obtained_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    token_type: str = "bearer"
    scope: str = "account"

    @property
    def expires_at(self) -> datetime:
        return self.obtained_at + timedelta(seconds=self.expires_in)

    def is_expired(self, margin: timedelta = EXPIRY_MARGIN) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at - margin

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["obtained_at"] = self.obtained_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Token:
        return cls(
            access_token=str(data["access_token"]),
            expires_in=int(str(data["expires_in"])),
            obtained_at=datetime.fromisoformat(str(data["obtained_at"])),
            token_type=str(data.get("token_type", "bearer")),
            scope=str(data.get("scope", "account")),
        )

    def __repr__(self) -> str:  # ne jamais afficher le token dans un log
        return f"Token(expires_at={self.expires_at.isoformat()!r})"


# ---------------------------------------------------------------- Requête d'auth


def build_auth_request(username: str, password: str, user_agent: str) -> httpx.Request:
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return httpx.Request(
        "GET",
        AUTH_URL,
        params=AUTH_PARAMS,
        headers={"Authorization": f"Basic {credentials}", "User-Agent": user_agent},
    )


def parse_auth_response(response: httpx.Response) -> Token:
    """Extrait le token de la redirection, ou lève l'erreur adaptée."""
    if response.status_code == 401:
        raise InvalidCredentialsError("Identifiant ou mot de passe refusé par Kordis.")
    if response.status_code == 403:
        raise BlockedRequestError(
            "Authentification bloquée par le pare-feu Kordis (403). Vérifier le User-Agent envoyé."
        )
    location = response.headers.get("Location")
    if response.status_code != 302 or not location:
        raise AuthenticationError(
            f"Réponse inattendue de l'endpoint d'authentification ({response.status_code})."
        )
    params = dict(parse_qsl(urlsplit(location).fragment))
    if "access_token" not in params:
        raise AuthenticationError("Redirection reçue sans access_token dans le fragment.")
    return Token(
        access_token=params["access_token"],
        expires_in=int(params.get("expires_in", 0)),
        token_type=params.get("token_type", "bearer"),
        scope=params.get("scope", "account"),
    )


def authenticate(http: httpx.Client, username: str, password: str, user_agent: str) -> Token:
    request = build_auth_request(username, password, user_agent)
    return parse_auth_response(http.send(request, follow_redirects=False))


async def authenticate_async(
    http: httpx.AsyncClient, username: str, password: str, user_agent: str
) -> Token:
    request = build_auth_request(username, password, user_agent)
    return parse_auth_response(await http.send(request, follow_redirects=False))


# ---------------------------------------------------------------- Stockage


class TokenStore(Protocol):
    def load(self, username: str) -> Token | None: ...

    def save(self, username: str, token: Token) -> None: ...

    def clear(self, username: str) -> None: ...


class MemoryTokenStore:
    """Cache en mémoire, perdu à la fin du process."""

    def __init__(self) -> None:
        self._tokens: dict[str, Token] = {}

    def load(self, username: str) -> Token | None:
        return self._tokens.get(username)

    def save(self, username: str, token: Token) -> None:
        self._tokens[username] = token

    def clear(self, username: str) -> None:
        self._tokens.pop(username, None)


def default_token_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "myges" / "tokens.json"


class FileTokenStore:
    """Tokens persistés dans un fichier JSON en ``0600``, un par identifiant.

    Écriture atomique (fichier temporaire puis ``rename``) pour qu'un process
    interrompu ne laisse jamais un cache tronqué.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_token_path()

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def load(self, username: str) -> Token | None:
        entry = self._read().get(username)
        if entry is None:
            return None
        try:
            return Token.from_dict(entry)
        except (KeyError, ValueError):
            return None

    def save(self, username: str, token: Token) -> None:
        data = self._read()
        data[username] = token.to_dict()
        self._write(data)

    def clear(self, username: str) -> None:
        data = self._read()
        if data.pop(username, None) is not None:
            self._write(data)
