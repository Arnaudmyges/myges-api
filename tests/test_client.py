"""Comportement des clients, contre une API simulée (respx)."""

from __future__ import annotations

import inspect
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from conftest import fixture
from myges import (
    APIError,
    AsyncMyGES,
    BadRequestError,
    BlockedRequestError,
    FileTokenStore,
    InvalidCredentialsError,
    MemoryTokenStore,
    MyGES,
    NotFoundError,
    Token,
    TokenExpiredError,
    WriteNotAllowedError,
)
from myges.auth import AUTH_URL
from myges.client import API_URL, to_millis

REDIRECT = (
    "comreseaugesskolae:/oauth2redirect"
    "#access_token={tok}&token_type=bearer&expires_in=560063&scope=account"
)


def auth_ok(tok: str = "tok-1") -> httpx.Response:
    return httpx.Response(302, headers={"Location": REDIRECT.format(tok=tok)})


def ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=fixture(name))


@pytest.fixture
def api() -> respx.MockRouter:
    with respx.mock(base_url=API_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def auth(api: respx.MockRouter) -> respx.Route:
    return api.get(AUTH_URL).mock(return_value=auth_ok())


@pytest.fixture
def client(auth: respx.Route) -> MyGES:
    with MyGES("jdupont", "secret", token_store=MemoryTokenStore()) as c:
        yield c


# ---------------------------------------------------------------- Auth


def test_login_parses_redirect_fragment(client: MyGES, auth: respx.Route) -> None:
    token = client.login()
    assert token.access_token == "tok-1"
    assert token.expires_in == 560063
    sent = auth.calls.last.request
    assert sent.headers["Authorization"] == "Basic amR1cG9udDpzZWNyZXQ="  # jdupont:secret
    assert sent.url.params["client_id"] == "skolae-app"


def test_invalid_credentials(api: respx.MockRouter) -> None:
    api.get(AUTH_URL).mock(return_value=httpx.Response(401, text="<html></html>"))
    with pytest.raises(InvalidCredentialsError), MyGES("jdupont", "faux") as c:
        c.login()


def test_waf_block_is_reported(api: respx.MockRouter) -> None:
    api.get(AUTH_URL).mock(
        return_value=httpx.Response(403, text="<html/>", headers={"content-type": "text/html"})
    )
    with pytest.raises(BlockedRequestError), MyGES("jdupont", "secret") as c:
        c.login()


def test_user_agent_is_not_python_requests(client: MyGES, api: respx.MockRouter) -> None:
    route = api.get("/me/years").mock(return_value=ok("getYears"))
    client.years()
    ua = route.calls.last.request.headers["User-Agent"]
    assert ua.startswith("myges-api/")


def test_token_is_reused_across_calls(
    client: MyGES, api: respx.MockRouter, auth: respx.Route
) -> None:
    route = api.get("/me/years").mock(return_value=ok("getYears"))
    client._years = None
    client.years()
    client._years = None
    client.years()
    assert auth.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-1"


def test_401_triggers_single_relogin(api: respx.MockRouter) -> None:
    auth = api.get(AUTH_URL).mock(side_effect=[auth_ok("old"), auth_ok("new")])
    route = api.get("/me/profile").mock(
        side_effect=lambda req: (
            ok("getProfile")
            if req.headers["Authorization"] == "Bearer new"
            else httpx.Response(401, text="<InvalidTokenException/>")
        )
    )
    with MyGES("jdupont", "secret", token_store=MemoryTokenStore()) as c:
        assert c.profile().firstname == "Camille"
    assert auth.call_count == 2
    assert route.call_count == 2


def test_401_without_password_raises(api: respx.MockRouter) -> None:
    api.get("/me/profile").mock(return_value=httpx.Response(401))
    with pytest.raises(TokenExpiredError), MyGES(token="brut") as c:
        c.profile()


def test_expired_cached_token_is_replaced(tmp_path: Path, auth: respx.Route) -> None:
    store = FileTokenStore(tmp_path / "tokens.json")
    old = datetime.now(timezone.utc) - timedelta(days=10)
    store.save("jdupont", Token("perime", expires_in=560063, obtained_at=old))
    with MyGES("jdupont", "secret", token_store=store) as c:
        assert c.get_token().access_token == "tok-1"
    loaded = store.load("jdupont")
    assert loaded is not None
    assert loaded.access_token == "tok-1"


def test_file_store_permissions_and_isolation(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "sub" / "tokens.json")
    store.save("a", Token("ta", 100))
    store.save("b", Token("tb", 100))
    assert (store.path.stat().st_mode & 0o777) == 0o600
    store.clear("a")
    assert store.load("a") is None
    loaded = store.load("b")
    assert loaded is not None
    assert loaded.access_token == "tb"


def test_token_repr_hides_secret() -> None:
    assert "secret-value" not in repr(Token("secret-value", 100))


# ---------------------------------------------------------------- Requêtes


def test_year_defaults_to_latest(client: MyGES, api: respx.MockRouter) -> None:
    api.get("/me/years").mock(return_value=ok("getYears"))
    route = api.get("/me/2026/grades").mock(return_value=ok("getGrades"))
    grades = client.grades()
    assert route.called
    assert grades[0].average == 15.25


def test_explicit_year(client: MyGES, api: respx.MockRouter) -> None:
    route = api.get("/me/2024/courses").mock(return_value=ok("getCourses"))
    assert len(client.courses(2024)) == 2
    assert route.called


def test_204_means_empty_list(client: MyGES, api: respx.MockRouter) -> None:
    api.get("/me/2026/absences").mock(return_value=httpx.Response(204))
    assert client.absences(2026) == []


def test_agenda_sends_millis(client: MyGES, api: respx.MockRouter) -> None:
    route = api.get("/me/agenda").mock(return_value=ok("getAgenda"))
    start = datetime(2026, 10, 19, tzinfo=timezone.utc)
    events = client.agenda(start, start + timedelta(days=7))
    params = route.calls.last.request.url.params
    assert params["start"] == "1792368000000"
    assert int(params["end"]) - int(params["start"]) == 7 * 86_400_000
    assert [e.reservation_id for e in events] == [3000001, 3000002, 3000003]


def test_to_millis_accepts_dates_and_ints() -> None:
    assert to_millis(1234) == 1234
    naive = date(2026, 10, 19)
    assert to_millis(naive) == int(datetime(2026, 10, 19).astimezone().timestamp() * 1000)
    with pytest.raises(TypeError):
        to_millis(True)


def test_404_and_400_map_to_errors(client: MyGES, api: respx.MockRouter) -> None:
    api.get("/me/courses/1").mock(return_value=httpx.Response(404, json=fixture("error_404")))
    api.get("/me/agenda").mock(return_value=httpx.Response(400, json=fixture("error_400")))
    with pytest.raises(NotFoundError) as nf:
        client.course(1)
    assert nf.value.status_code == 404
    with pytest.raises(BadRequestError):
        client.request("GET", "/me/agenda")


def test_news_pagination(client: MyGES, api: respx.MockRouter) -> None:
    first = fixture("getNews")
    first["result"] = first["result"] | {"last": False}
    second = fixture("getNews")
    second["result"] = second["result"] | {"number": 1, "content": second["result"]["content"][:1]}
    route = api.get("/me/news").mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    titles = [n.title for n in client.iter_news(size=2)]
    assert len(titles) == 3
    assert [c.request.url.params["page"] for c in route.calls] == ["0", "1"]
    assert route.calls.last.request.url.params["size"] == "2"


def test_writes_are_refused_by_default(client: MyGES, api: respx.MockRouter) -> None:
    route = api.post("/me/projectGroups/1/messages")
    with pytest.raises(WriteNotAllowedError):
        client.send_project_group_message(1, "bonjour")
    with pytest.raises(WriteNotAllowedError):
        client.join_project_group(1, 2, 3)
    assert not route.called


def _chat(api: respx.MockRouter, posted: list[str], *, stored: bool) -> None:
    """Chat simulé : le message posté apparaît à la relecture si ``stored``."""
    base = fixture("getProjectGroupMessages")["result"]

    def messages(_: httpx.Request) -> httpx.Response:
        extra = [
            {**base[0], "project_message_id": 900 + i, "uid": 100001, "message": text}
            for i, text in enumerate(posted if stored else [])
        ]
        return httpx.Response(
            200, json={"response_code": 0, "version": "1", "result": base + extra}
        )

    api.get("/me/projectGroups/7/messages").mock(side_effect=messages)


def _myges_500() -> httpx.Response:
    return httpx.Response(
        500,
        json={
            "response_code": 0,
            "version": "1",
            "result": {
                "status": 500,
                "error": "Internal Server Error",
                "message": "",
                "path": "/x",
            },
        },
    )


def test_group_message_500_but_stored_is_a_success(
    api: respx.MockRouter, auth: respx.Route
) -> None:
    posted: list[str] = []
    _chat(api, posted, stored=True)

    def post(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content)["message"])
        return _myges_500()

    route = api.post("/me/projectGroups/7/messages").mock(side_effect=post)
    with MyGES("jdupont", "secret", token_store=MemoryTokenStore(), allow_writes=True) as c:
        sent = c.send_project_group_message(7, "bonjour")
    assert sent.message == "bonjour"
    assert sent.project_message_id == 900
    assert route.call_count == 1  # jamais renvoyé
    assert json.loads(route.calls.last.request.content) == {
        "projectGroupId": 7,
        "message": "bonjour",
    }


def test_group_message_500_not_stored_raises(api: respx.MockRouter, auth: respx.Route) -> None:
    _chat(api, [], stored=False)
    route = api.post("/me/projectGroups/7/messages").mock(return_value=_myges_500())
    client = MyGES("jdupont", "secret", token_store=MemoryTokenStore(), allow_writes=True)
    with client as c, pytest.raises(APIError) as caught:
        c.send_project_group_message(7, "bonjour")
    assert caught.value.status_code == 500
    assert route.call_count == 1


def test_group_message_other_errors_propagate(api: respx.MockRouter, auth: respx.Route) -> None:
    _chat(api, [], stored=False)
    api.post("/me/projectGroups/7/messages").mock(
        return_value=httpx.Response(400, json=fixture("error_400"))
    )
    client = MyGES("jdupont", "secret", token_store=MemoryTokenStore(), allow_writes=True)
    with client as c, pytest.raises(APIError):
        c.send_project_group_message(7, "bonjour")


def test_group_messages_parsed(api: respx.MockRouter, auth: respx.Route) -> None:
    _chat(api, [], stored=False)
    with MyGES("jdupont", "secret", token_store=MemoryTokenStore()) as c:
        messages = c.project_group_messages(7)
    assert messages[0].firstname == "Alex"
    assert messages[0].date is not None


# ---------------------------------------------------------------- Asynchrone


def _public(cls: type) -> set[str]:
    return {n for n, _ in inspect.getmembers(cls) if not n.startswith("_")}


def test_sync_and_async_expose_the_same_api() -> None:
    assert _public(MyGES) - {"__enter__", "__exit__"} == _public(AsyncMyGES)


@pytest.mark.anyio
async def test_async_client(api: respx.MockRouter, auth: respx.Route) -> None:
    api.get("/me/years").mock(return_value=ok("getYears"))
    api.get("/me/2026/courses").mock(return_value=ok("getCourses"))
    api.get("/me/2026/absences").mock(return_value=httpx.Response(204))
    async with AsyncMyGES("jdupont", "secret", token_store=MemoryTokenStore()) as c:
        courses = await c.courses()
        assert courses[0].has_documents is True
        assert await c.absences() == []
    assert auth.call_count == 1


@pytest.mark.anyio
async def test_async_relogin(api: respx.MockRouter) -> None:
    api.get(AUTH_URL).mock(side_effect=[auth_ok("old"), auth_ok("new")])
    api.get("/me/events").mock(
        side_effect=lambda req: (
            ok("getEvents") if req.headers["Authorization"] == "Bearer new" else httpx.Response(401)
        )
    )
    async with AsyncMyGES("jdupont", "secret", token_store=MemoryTokenStore()) as c:
        events = await c.events()
    assert events[0].event_title == "Afterwork de rentrée"


# ---------------------------------------------------------------- Supports et syllabus


def test_course_files_and_download(client: MyGES, api: respx.MockRouter) -> None:
    api.get("/me/500001/files").mock(return_value=ok("getCourseFiles"))
    pdf = b"%PDF-1.7\n\xff\xd8 binaire"
    # L'API annonce application/json pour un PDF : le client ne doit pas tenter de le décoder.
    route = api.get("/me/500001/files/610001").mock(
        return_value=httpx.Response(200, content=pdf, headers={"content-type": "application/json"})
    )
    files = client.course_files(500001)
    assert [f.filename for f in files] == ["Chapitre 1 - Introduction.pdf", "TP1_sources.zip"]
    assert files[0].web_url == "https://ges-dl.kordis.fr/private/exemple-jeton-1"
    assert client.download_course_file(500001, files[0].oc_id) == pdf
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok-1"


def test_download_errors(client: MyGES, api: respx.MockRouter) -> None:
    error = fixture("error_404") | {"result": fixture("error_404")["result"] | {"status": 500}}
    api.get("/me/500001/files/1").mock(return_value=httpx.Response(500, json=error))
    api.get("/me/1/files/2").mock(return_value=httpx.Response(400, json=fixture("error_400")))
    with pytest.raises(APIError) as server:
        client.download_course_file(500001, 1)
    assert server.value.status_code == 500
    with pytest.raises(BadRequestError):
        client.download_course_file(1, 2)


def test_syllabus(client: MyGES, api: respx.MockRouter) -> None:
    api.get("/me/500001/syllabus").mock(return_value=ok("getSyllabus"))
    api.get("/me/500002/syllabus").mock(
        return_value=httpx.Response(200, json={"response_code": 0, "version": "1", "result": []})
    )
    syllabus = client.syllabus(500001)
    assert syllabus is not None
    assert [s.number for s in syllabus.seance_details or []] == [1, 2]
    assert client.syllabus(500002) is None


def test_subscribe_event_is_a_guarded_write(client: MyGES, api: respx.MockRouter) -> None:
    route = api.post("/me/events/1/subscribe")
    with pytest.raises(WriteNotAllowedError):
        client.subscribe_event(1)
    assert not route.called


@pytest.mark.anyio
async def test_async_download(api: respx.MockRouter, auth: respx.Route) -> None:
    api.get("/me/500001/files/610002").mock(return_value=httpx.Response(200, content=b"PK\x03\x04"))
    async with AsyncMyGES("jdupont", "secret", token_store=MemoryTokenStore()) as c:
        assert await c.download_course_file(500001, 610002) == b"PK\x03\x04"
