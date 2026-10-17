"""Serveur MCP distant (HTTP + OAuth) : myGES dans Claude sur le web et le mobile.

claude.ai appelle les connecteurs depuis les serveurs d'Anthropic : le serveur doit donc
être joignable en HTTPS public (Tailscale Funnel, proxy avec nom de domaine…). Il écoute
en local et c'est le proxy qui l'expose.

Protections :

- **OAuth 2.1** (PKCE, enregistrement dynamique) : seul l'étudiant configuré sur la
  machine (`myges login`) peut se connecter, avec son identifiant et son mot de passe
  myGES, vérifiés en local quand le mot de passe est enregistré (sinon auprès de Kordis).
- **Adresses de retour** limitées à claude.ai / claude.com (et à la boucle locale, pour
  Claude Code) : un tiers ne peut pas faire envoyer un code d'accès chez lui.
- **5 échecs de connexion** en 15 minutes bloquent la page 15 minutes.
- **Jetons** : d'accès 1 h, de renouvellement 30 jours avec rotation, stockés hachés
  (SHA-256) dans un fichier 0600. `myges mcp --revoke` les invalide tous.
- **Outils** : lecture et brouillons d'e-mail uniquement. Pas d'inscription, pas de
  message de groupe, pas de fichier écrit sur le serveur.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import cli
from .auth import authenticate
from .client import DEFAULT_USER_AGENT
from .errors import AuthenticationError

log = logging.getLogger("myges.mcp")

SCOPE = "myges"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300
LOGIN_TTL = 600  # durée de vie d'une page de connexion ouverte
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60

CLAUDE_CALLBACKS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "myges" / "mcp-oauth.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def redirect_allowed(uri: str) -> bool:
    """claude.ai / claude.com, ou une adresse de boucle locale (Claude Code)."""
    if uri in CLAUDE_CALLBACKS:
        return True
    parts = urlsplit(uri)
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


class _Store:
    """État OAuth persistant : clients, jetons hachés. Fichier JSON 0600."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.clients: dict[str, Any] = data.get("clients", {})
        self.access: dict[str, Any] = data.get("access", {})
        self.refresh: dict[str, Any] = data.get("refresh", {})

    def save(self) -> None:
        now = time.time()
        with self._lock:
            self.access = {k: v for k, v in self.access.items() if v["expires_at"] > now}
            self.refresh = {k: v for k, v in self.refresh.items() if v["expires_at"] > now}
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(
                    {"clients": self.clients, "access": self.access, "refresh": self.refresh}, fh
                )
            tmp.replace(self.path)


def revoke_all(path: Path | None = None) -> int:
    """Tout oublier : jetons et clients enregistrés (chaque client devra se reconnecter).
    Renvoie le nombre de jetons invalidés."""
    store = _Store(path or state_path())
    count = len(store.access) + len(store.refresh)
    store.clients, store.access, store.refresh = {}, {}, {}
    store.save()
    return count


class _Throttle:
    """Échecs de connexion récents : au-delà de MAX_FAILURES, la page est bloquée."""

    def __init__(self) -> None:
        self.failures: deque[float] = deque()

    def blocked(self, now: float) -> bool:
        while self.failures and now - self.failures[0] > FAILURE_WINDOW:
            self.failures.popleft()
        return len(self.failures) >= MAX_FAILURES

    def fail(self, now: float) -> None:
        self.failures.append(now)


class MyGESOAuthProvider:
    """Serveur d'autorisation OAuth à un seul utilisateur : l'étudiant de la machine."""

    def __init__(self, public_url: str, path: Path | None = None) -> None:
        self.public_url = public_url.rstrip("/")
        self.store = _Store(path or state_path())
        self.codes: dict[str, AuthorizationCode] = {}
        self.pending: dict[str, tuple[float, str, AuthorizationParams]] = {}
        self.throttle = _Throttle()

    # ------------------------------------------------------------ Clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.clients.get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        refused = [u for u in uris if not redirect_allowed(u)]
        if not uris or refused:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="adresse de retour non autorisée : " + ", ".join(refused),
            )
        assert client_info.client_id is not None
        self.store.clients[client_info.client_id] = client_info.model_dump(mode="json")
        self.store.save()
        log.warning("client OAuth enregistré : %s", client_info.client_name or "?")

    # ------------------------------------------------------------ Autorisation

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if not redirect_allowed(str(params.redirect_uri)):
            raise AuthorizeError(error="invalid_request", error_description="adresse de retour")
        request_id = secrets.token_urlsafe(32)
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if now - v[0] < LOGIN_TTL}
        assert client.client_id is not None
        self.pending[request_id] = (now, client.client_id, params)
        return f"{self.public_url}/login?request={request_id}"

    def _issue_code(self, client_id: str, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=[SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=cli._current_username(),
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        if code is None or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:  # usage unique
            raise TokenError(error="invalid_grant", error_description="code déjà utilisé")
        assert client.client_id is not None
        return self._tokens(client.client_id, authorization_code.resource)

    # ------------------------------------------------------------ Jetons

    def _tokens(self, client_id: str, resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        common = {"client_id": client_id, "resource": resource}
        self.store.access[_digest(access)] = {**common, "expires_at": now + ACCESS_TTL}
        self.store.refresh[_digest(refresh)] = {**common, "expires_at": now + REFRESH_TTL}
        self.store.save()
        return OAuthToken(
            access_token=access, expires_in=ACCESS_TTL, scope=SCOPE, refresh_token=refresh
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        raw = self.store.refresh.get(_digest(refresh_token))
        if raw is None or raw["client_id"] != client.client_id or raw["expires_at"] < time.time():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=raw["client_id"],
            scopes=[SCOPE],
            expires_at=raw["expires_at"],
            resource=raw.get("resource"),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Rotation : l'ancien jeton de renouvellement ne sert qu'une fois.
        if self.store.refresh.pop(_digest(refresh_token.token), None) is None:
            raise TokenError(error="invalid_grant", error_description="jeton déjà utilisé")
        assert client.client_id is not None
        return self._tokens(client.client_id, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = self.store.access.get(_digest(token))
        if raw is None or raw["expires_at"] < time.time():
            return None
        return AccessToken(
            token=token,
            client_id=raw["client_id"],
            scopes=[SCOPE],
            expires_at=raw["expires_at"],
            resource=raw.get("resource"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Révoquer l'un révoque tout ce que ce client détient.
        for table in (self.store.access, self.store.refresh):
            for key in [k for k, v in table.items() if v["client_id"] == token.client_id]:
                del table[key]
        self.store.save()

    # ------------------------------------------------------------ Page de connexion

    def check_credentials(self, username: str, password: str) -> bool:
        expected = cli._current_username() or ""
        if not expected or not hmac.compare_digest(
            username.strip().casefold(), expected.casefold()
        ):
            return False
        saved = cli._current_password()
        if saved is not None:
            return hmac.compare_digest(password.encode(), saved.encode())
        try:
            with httpx.Client(timeout=20) as http:
                authenticate(http, expected, password, DEFAULT_USER_AGENT)
        except (AuthenticationError, httpx.HTTPError):
            return False
        return True

    async def login_page(self, request: Request) -> Response:
        request_id = (
            request.query_params.get("request")
            if request.method == "GET"
            else str((await request.form()).get("request") or "")
        )
        request_id = request_id or ""
        entry = self.pending.get(request_id)
        if entry is None or time.time() - entry[0] > LOGIN_TTL:
            return _page("Lien expiré", "<p>Relancez la connexion depuis Claude.</p>", 400)
        _, client_id, params = entry
        client = await self.get_client(client_id)
        name = html.escape((client.client_name if client else None) or "un client MCP")
        target = html.escape(urlsplit(str(params.redirect_uri)).netloc)
        error = ""
        if request.method == "POST":
            now = time.time()
            if self.throttle.blocked(now):
                return _page(
                    "Trop d'essais",
                    "<p>Trop de tentatives : réessayez dans 15 minutes.</p>",
                    429,
                )
            form = await request.form()
            username = str(form.get("username") or "")
            password = str(form.get("password") or "")
            if self.check_credentials(username, password):
                del self.pending[request_id]
                log.warning("connexion autorisée pour %s", target)
                return RedirectResponse(self._issue_code(client_id, params), status_code=302)
            self.throttle.fail(now)
            log.warning("échec de connexion (%d récents)", len(self.throttle.failures))
            error = '<p class="err">Identifiant ou mot de passe incorrect.</p>'
        body = f"""
<p><b>{name}</b> demande l'accès en lecture à votre espace myGES (emploi du temps, notes,
absences, projets…) et pourra préparer des brouillons d'e-mail.</p>
<p class="warn">Continuez seulement si <b>vous</b> venez de cliquer sur « Connecter » dans
Claude. Après connexion, vous serez renvoyé vers <b>{target}</b>.</p>
{error}
<form method="post" autocomplete="on">
  <input type="hidden" name="request" value="{html.escape(request_id or "")}">
  <label>Identifiant myGES <input name="username" autocomplete="username" required></label>
  <label>Mot de passe <input name="password" type="password"
    autocomplete="current-password" required></label>
  <button type="submit">Autoriser</button>
</form>"""
        return _page("Connexion à myGES", body, 200)


_STYLE = """
body{font:16px/1.5 system-ui,sans-serif;max-width:28rem;margin:2rem auto;padding:0 1rem;
color:#1b1b1f;background:#fafafa}
h1{font-size:1.4rem}label{display:block;margin:.8rem 0}
input{display:block;width:100%;padding:.5rem;font:inherit;box-sizing:border-box;
border:1px solid #bbb;border-radius:6px}
button{margin-top:1rem;padding:.6rem 1.2rem;font:inherit;border:0;border-radius:6px;
background:#2b59c3;color:#fff;cursor:pointer}
.warn{background:#fff6dd;padding:.6rem;border-radius:6px}.err{color:#b00020}
@media (prefers-color-scheme:dark){body{background:#17171a;color:#eee}
input{background:#222;color:#eee;border-color:#555}.warn{background:#3a3220}}
"""


def _page(title: str, body: str, status: int) -> HTMLResponse:
    page = (
        f'<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(
        page,
        status_code=status,
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
            "form-action 'self' https://claude.ai https://claude.com http://localhost:* "
            "http://127.0.0.1:*; frame-ancestors 'none'",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        },
    )


def build_app(public_url: str, state: Path | None = None) -> Any:
    """Application ASGI : MCP sur /mcp, OAuth (/authorize, /token, /register…), /login."""
    from .mcp_server import build_server

    public = public_url.rstrip("/")
    provider = MyGESOAuthProvider(public, state)
    auth = AuthSettings(
        issuer_url=public,
        resource_server_url=f"{public}/mcp",
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
        validate_token_resource=False,  # un seul serveur de ressources : celui-ci
    )
    server = build_server(remote=True, auth=auth, auth_server_provider=provider)

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    # Routes publiques (hors OAuth) : la page de connexion et une sonde.
    server.custom_route("/login", methods=["GET", "POST"])(provider.login_page)
    server.custom_route("/health", methods=["GET"])(health)

    return server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[urlsplit(public).netloc, "127.0.0.1:*", "localhost:*"],
            allowed_origins=["https://claude.ai", "https://claude.com"],
        ),
    )


def run(listen: str, public_url: str) -> None:
    """Lancer le serveur HTTP (bloquant). ``listen`` = hôte:port local."""
    import anyio
    import uvicorn

    host, _, port = listen.rpartition(":")
    config = uvicorn.Config(
        build_app(public_url), host=host, port=int(port), log_level="warning", proxy_headers=True
    )
    anyio.run(uvicorn.Server(config).serve)
