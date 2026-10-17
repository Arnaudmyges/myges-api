"""Serveur MCP distant : parcours OAuth complet (comme claude.ai), puis appels MCP."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp")

from starlette.testclient import TestClient

from myges import mcp_remote

PUBLIC = "https://myges.example.ts.net"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def server(home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("MYGES_PASSWORD", "secret")
    app = mcp_remote.build_app(PUBLIC, home / "oauth.json")
    with TestClient(app, base_url=PUBLIC) as client:
        yield client


def register(client: TestClient, redirect: str = CALLBACK) -> Any:
    return client.post(
        "/register",
        json={
            "redirect_uris": [redirect],
            "client_name": "Claude",
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
        },
    )


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def start_login(client: TestClient, client_id: str, challenge: str) -> str:
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "etat-123",
            "scope": "myges",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    login = response.headers["location"]
    assert login.startswith(f"{PUBLIC}/login?request=")
    return parse_qs(urlsplit(login).query)["request"][0]


def log_in(client: TestClient, request_id: str, password: str = "secret") -> Any:
    return client.post(
        "/login",
        data={"request": request_id, "username": "jdupont", "password": password},
        follow_redirects=False,
    )


def connect(client: TestClient) -> dict[str, Any]:
    """Parcours complet : enregistrement, connexion, échange du code. Renvoie les jetons."""
    client_id = register(client).json()["client_id"]
    verifier, challenge = pkce()
    request_id = start_login(client, client_id, challenge)
    redirect = log_in(client, request_id)
    assert redirect.status_code == 302, redirect.text
    target = urlsplit(redirect.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == CALLBACK
    query = parse_qs(target.query)
    assert query["state"] == ["etat-123"]
    tokens = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert tokens.status_code == 200, tokens.text
    return {**tokens.json(), "client_id": client_id, "code": query["code"][0], "verifier": verifier}


def rpc(
    client: TestClient, token: str | None, method: str, params: Any = None, **extra: str
) -> Any:
    headers = {**MCP_HEADERS, **extra}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers=headers)


def rpc_result(response: Any) -> Any:
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        data = re.findall(r"^data: (.+)$", response.text, re.M)
        return json.loads(data[-1])
    return response.json()


def initialize(client: TestClient, token: str) -> dict[str, str]:
    response = rpc(
        client,
        token,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert "result" in rpc_result(response), response.text
    session = {"mcp-session-id": response.headers["mcp-session-id"]}
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**MCP_HEADERS, **session, "Authorization": f"Bearer {token}"},
    )
    return session


# ---------------------------------------------------------------- Découverte


def test_metadata_advertises_oauth(server: TestClient) -> None:
    resource = server.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == f"{PUBLIC}/mcp"
    meta = server.get("/.well-known/oauth-authorization-server").json()
    assert meta["authorization_endpoint"] == f"{PUBLIC}/authorize"
    assert meta["registration_endpoint"] == f"{PUBLIC}/register"
    assert "S256" in meta["code_challenge_methods_supported"]


def test_mcp_requires_a_token(server: TestClient) -> None:
    response = rpc(server, None, "tools/list")
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]
    assert rpc(server, "faux-jeton", "tools/list").status_code == 401


# ---------------------------------------------------------------- Enregistrement


@pytest.mark.parametrize(
    "redirect",
    ["https://evil.example/callback", "https://claude.ai.evil.example/api/mcp/auth_callback"],
)
def test_foreign_redirects_are_refused(server: TestClient, redirect: str) -> None:
    response = register(server, redirect)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "redirect",
    [
        CALLBACK,
        "https://claude.com/api/mcp/auth_callback",
        "http://localhost:53682/callback",
        "http://127.0.0.1:8123/cb",
    ],
)
def test_claude_and_loopback_redirects_are_accepted(server: TestClient, redirect: str) -> None:
    assert register(server, redirect).status_code == 201


# ---------------------------------------------------------------- Connexion


def test_login_page_shows_client_and_target(server: TestClient) -> None:
    client_id = register(server).json()["client_id"]
    request_id = start_login(server, client_id, pkce()[1])
    page = server.get("/login", params={"request": request_id})
    assert page.status_code == 200
    assert "Claude" in page.text
    assert "claude.ai" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert server.get("/login", params={"request": "inconnu"}).status_code == 400


def test_wrong_credentials_then_lockout(server: TestClient) -> None:
    client_id = register(server).json()["client_id"]
    request_id = start_login(server, client_id, pkce()[1])
    for _ in range(mcp_remote.MAX_FAILURES):
        wrong = log_in(server, request_id, password="nope")
        assert wrong.status_code == 200
        assert "incorrect" in wrong.text
    # Même le bon mot de passe est refusé pendant le blocage.
    assert log_in(server, request_id).status_code == 429


def test_other_account_is_refused(server: TestClient) -> None:
    client_id = register(server).json()["client_id"]
    request_id = start_login(server, client_id, pkce()[1])
    other = server.post(
        "/login",
        data={"request": request_id, "username": "quelquun", "password": "secret"},
        follow_redirects=False,
    )
    assert other.status_code == 200
    assert "incorrect" in other.text


# ---------------------------------------------------------------- Jetons


def test_full_flow_then_tools(server: TestClient) -> None:
    tokens = connect(server)
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == mcp_remote.ACCESS_TTL
    session = initialize(server, tokens["access_token"])
    listed = rpc_result(rpc(server, tokens["access_token"], "tools/list", **session))
    names = {t["name"] for t in listed["result"]["tools"]}
    assert {"grades", "agenda", "next_class", "teachers", "draft_email"} <= names
    for hidden in (
        "subscribe_event",
        "unsubscribe_event",
        "send_project_message",
        "download_course_files",
        "export_agenda_ics",
        "cancel_pending_registration",
    ):
        assert hidden not in names


def test_code_is_single_use_and_needs_pkce(server: TestClient) -> None:
    tokens = connect(server)
    replay = server.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": tokens["code"],
            "redirect_uri": CALLBACK,
            "client_id": tokens["client_id"],
            "code_verifier": tokens["verifier"],
        },
    )
    assert replay.status_code == 400


def test_refresh_rotates_and_old_refresh_dies(server: TestClient) -> None:
    tokens = connect(server)

    def refresh(token: str) -> Any:
        return server.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": tokens["client_id"],
            },
        )

    fresh = refresh(tokens["refresh_token"])
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["refresh_token"] != tokens["refresh_token"]
    assert refresh(tokens["refresh_token"]).status_code == 400  # rotation


def test_tokens_are_stored_hashed_and_revocable(server: TestClient, home: Path) -> None:
    tokens = connect(server)
    stored = (home / "oauth.json").read_text()
    assert tokens["access_token"] not in stored
    assert tokens["refresh_token"] not in stored
    assert (home / "oauth.json").stat().st_mode & 0o077 == 0
    assert mcp_remote.revoke_all(home / "oauth.json") == 2


def test_revocation_cuts_access(server: TestClient) -> None:
    tokens = connect(server)
    revoked = server.post(
        "/revoke",
        # Le SDK exige le champ client_secret, vide pour un client public.
        data={
            "token": tokens["access_token"],
            "client_id": tokens["client_id"],
            "client_secret": "",
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert rpc(server, tokens["access_token"], "tools/list").status_code == 401


def test_unknown_host_is_rejected(server: TestClient) -> None:
    tokens = connect(server)
    response = rpc(server, tokens["access_token"], "tools/list", Host="evil.example")
    assert response.status_code == 421


def test_tool_call_over_http(server: TestClient) -> None:
    import httpx
    import respx

    from conftest import fixture
    from myges.client import API_URL

    tokens = connect(server)
    session = initialize(server, tokens["access_token"])
    with respx.mock(assert_all_called=False) as router:
        for path, name in [
            ("/me/years", "getYears"),
            ("/me/2026/courses", "getCourses"),
            ("/me/2026/grades", "getGrades"),
        ]:
            router.get(f"{API_URL}{path}").mock(
                return_value=httpx.Response(200, json=fixture(name))
            )
        called = rpc_result(
            rpc(
                server,
                tokens["access_token"],
                "tools/call",
                {"name": "grades", "arguments": {}},
                **session,
            )
        )
    assert called["result"]["structuredContent"]["overall_average"] == pytest.approx(15.25)
