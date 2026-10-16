"""Clients synchrone (:class:`MyGES`) et asynchrone (:class:`AsyncMyGES`).

Les deux exposent exactement les mêmes méthodes (vérifié par les tests). La
logique commune (en-têtes, décodage de l'enveloppe, erreurs) est dans
:class:`_BaseClient` ; seules les entrées/sorties diffèrent.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime, time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter

from . import models as m
from ._version import __version__
from .auth import (
    FileTokenStore,
    MemoryTokenStore,
    Token,
    TokenStore,
    authenticate,
    authenticate_async,
)
from .errors import (
    APIError,
    BadRequestError,
    BlockedRequestError,
    NotFoundError,
    TokenExpiredError,
    WriteNotAllowedError,
)

API_URL = "https://api.kordis.fr"
# User-Agent explicite : le pare-feu Kordis rejette (403) celui de python-requests,
# et un UA identifiable est plus honnête qu'une imitation de navigateur.
DEFAULT_USER_AGENT = f"myges-api/{__version__} (+https://github.com/Arnaudmyges/myges-api)"

M = TypeVar("M", bound=BaseModel)

When = date | datetime | int

_PRIVATE = ConfigDict(hide_input_in_errors=True)


def to_millis(value: When) -> int:
    """Convertit une date, un datetime (naïf = heure locale) ou des ms en ms epoch."""
    if isinstance(value, bool):
        raise TypeError("booléen reçu au lieu d'une date")
    if isinstance(value, int):
        return value
    if not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if value.tzinfo is None:
        value = value.astimezone()
    return int(value.timestamp() * 1000)


def _parse_list(model: type[M], result: Any) -> list[M]:
    adapter = TypeAdapter(list[model], config=_PRIVATE)  # type: ignore[valid-type]
    return adapter.validate_python(result or [])


class _BaseClient:
    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        token: Token | str | None = None,
        token_store: TokenStore | None = None,
        base_url: str = API_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        allow_writes: bool = False,
    ) -> None:
        if isinstance(token, str):
            # Token fourni brut : durée de vie inconnue, on le considère valide
            # jusqu'à ce que l'API le refuse.
            token = Token(access_token=token, expires_in=10**9)
        self.username = username
        self._password = password
        self._token = token
        self._store = token_store or (FileTokenStore() if username else MemoryTokenStore())
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.allow_writes = allow_writes
        self._years: list[int] | None = None

    @classmethod
    def _env_kwargs(cls) -> dict[str, Any]:
        return {
            "username": os.environ.get("MYGES_USERNAME"),
            "password": os.environ.get("MYGES_PASSWORD"),
            "token": os.environ.get("MYGES_TOKEN"),
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(username={self.username!r})"

    # ------------------------------------------------------------ Token

    def _cached_token(self) -> Token | None:
        if self._token is None and self.username:
            self._token = self._store.load(self.username)
        if self._token is not None and not self._token.is_expired():
            return self._token
        return None

    def _can_login(self) -> bool:
        return bool(self.username and self._password)

    def _credentials(self) -> tuple[str, str]:
        if not (self.username and self._password):
            raise self._no_credentials()
        return self.username, self._password

    def _remember(self, token: Token) -> Token:
        self._token = token
        if self.username:
            self._store.save(self.username, token)
        return token

    def _no_credentials(self) -> TokenExpiredError:
        return TokenExpiredError(
            "Session expirée ou absente, et pas de mot de passe pour la renouveler : "
            "relancer `myges login` (ou définir MYGES_PASSWORD)."
        )

    # ------------------------------------------------------------ HTTP

    def _headers(self, token: Token) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token.access_token}",
            "User-Agent": self.user_agent,
            "Accept": "application/json, application/hal+json",
        }

    def _check_write(self, operation: str) -> None:
        if not self.allow_writes:
            raise WriteNotAllowedError(
                f"{operation} modifie des données côté myGES et n'a jamais été vérifié "
                "par ce projet. Créer le client avec allow_writes=True pour l'autoriser."
            )

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        """Renvoie ``result`` de l'enveloppe, ou None pour un 204."""
        path = response.request.url.path
        status = response.status_code
        if status == 204 or (status == 200 and not response.content):
            return None
        if status == 403 and "html" in response.headers.get("content-type", ""):
            raise BlockedRequestError(f"Requête bloquée par le pare-feu Kordis sur {path}.")
        if status >= 400:
            message = ""
            try:
                body = response.json()
                message = (body.get("result") or {}).get("error") or body.get("exception", "")
            except ValueError:
                message = response.text[:200]
            error = {400: BadRequestError, 404: NotFoundError}.get(status, APIError)
            raise error(status, path, message)
        return response.json().get("result")

    @classmethod
    def _content(cls, response: httpx.Response) -> bytes:
        """Corps brut d'un téléchargement, ou l'erreur décodée de l'enveloppe.

        L'API annonce ``application/json`` même pour un PDF : seul le code HTTP compte.
        """
        if response.status_code != 200:
            cls._decode(response)
            raise APIError(response.status_code, response.request.url.path)
        return response.content

    def _year_from(self, years: list[int] | None) -> int:
        if not years:
            raise APIError(200, "/me/years", "aucune année scolaire disponible")
        return max(years)


# ==================================================================== Synchrone


class MyGES(_BaseClient):
    """Client synchrone.

    >>> with MyGES("jdupont", "motdepasse") as ges:
    ...     for cours in ges.agenda(date.today(), date.today() + timedelta(days=7)):
    ...         print(cours.start_date, cours.name, cours.location)

    Sans argument, ``year`` vaut la dernière année scolaire disponible.
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        http: httpx.Client | None = None,
        timeout: float = 20.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(username, password, **kwargs)
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=timeout, transport=httpx.HTTPTransport(retries=2))

    @classmethod
    def from_env(cls, **kwargs: Any) -> MyGES:
        """Lit ``MYGES_USERNAME``, ``MYGES_PASSWORD`` et/ou ``MYGES_TOKEN``."""
        return cls(**{**cls._env_kwargs(), **kwargs})

    def __enter__(self) -> MyGES:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # ------------------------------------------------------------ Auth

    def login(self) -> Token:
        """Force une authentification et met le token en cache."""
        username, password = self._credentials()
        token = authenticate(self._http, username, password, self.user_agent)
        return self._remember(token)

    def logout(self) -> None:
        """Oublie le token (en mémoire et dans le cache). Kordis n'a pas de révocation."""
        self._token = None
        if self.username:
            self._store.clear(self.username)

    def get_token(self) -> Token:
        """Token valide, en s'authentifiant si nécessaire."""
        cached = self._cached_token()
        if cached is not None:
            return cached
        return self.login()

    # ------------------------------------------------------------ HTTP

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """Appel brut. Renvoie ``result`` de l'enveloppe (None pour un 204).

        Sur un 401, se ré-authentifie une fois si un mot de passe est disponible.
        """
        return self._decode(self._send(method, path, params=params, json=json))

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        response = self._http.request(
            method, url, params=params, json=json, headers=self._headers(self.get_token())
        )
        if response.status_code == 401:
            if not self._can_login():
                raise self._no_credentials()
            response = self._http.request(
                method, url, params=params, json=json, headers=self._headers(self.login())
            )
            if response.status_code == 401:
                raise TokenExpiredError("Token refusé juste après ré-authentification.")
        return response

    def _get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def _year(self, year: int | None) -> int:
        return year if year is not None else self._year_from(self.years())

    # ------------------------------------------------------------ Profil

    def profile(self) -> m.Profile:
        return m.Profile.model_validate(self._get("/me/profile"))

    def years(self) -> list[int]:
        """Années scolaires disponibles (mises en cache pour la durée du client)."""
        if self._years is None:
            self._years = list(self._get("/me/years") or [])
        return self._years

    # ------------------------------------------------------------ Agenda

    def agenda(self, start: When, end: When) -> list[m.AgendaEvent]:
        """Créneaux entre ``start`` et ``end`` (date, datetime ou ms epoch)."""
        result = self._get("/me/agenda", start=to_millis(start), end=to_millis(end))
        return _parse_list(m.AgendaEvent, result)

    # ------------------------------------------------------------ Scolarité

    def courses(self, year: int | None = None) -> list[m.Course]:
        return _parse_list(m.Course, self._get(f"/me/{self._year(year)}/courses"))

    def course(self, rc_id: int) -> m.CourseDetail:
        return m.CourseDetail.model_validate(self._get(f"/me/courses/{rc_id}"))

    def grades(self, year: int | None = None) -> list[m.Grade]:
        return _parse_list(m.Grade, self._get(f"/me/{self._year(year)}/grades"))

    def absences(self, year: int | None = None) -> list[m.Absence]:
        return _parse_list(m.Absence, self._get(f"/me/{self._year(year)}/absences"))

    def syllabus(self, rc_id: int) -> m.Syllabus | None:
        """Syllabus d'une matière, ou None si l'intervenant n'en a pas rempli."""
        found = _parse_list(m.Syllabus, self._get(f"/me/{rc_id}/syllabus"))
        return found[0] if found else None

    def course_files(self, rc_id: int) -> list[m.CourseFile]:
        """Supports de cours d'une matière (non vide ssi ``Course.has_documents``)."""
        return _parse_list(m.CourseFile, self._get(f"/me/{rc_id}/files"))

    def download_course_file(self, rc_id: int, file_id: int) -> bytes:
        """Contenu d'un support de cours (``CourseFile.oc_id``)."""
        return self._content(self._send("GET", f"/me/{rc_id}/files/{file_id}"))

    # ------------------------------------------------------------ Personnes

    def teachers(self, year: int | None = None) -> list[m.Person]:
        return _parse_list(m.Person, self._get(f"/me/{self._year(year)}/teachers"))

    def teacher(self, teacher_id: int) -> m.PersonDetail:
        return m.PersonDetail.model_validate(self._get(f"/me/teachers/{teacher_id}"))

    def classes(self, year: int | None = None) -> list[m.SchoolClass]:
        return _parse_list(m.SchoolClass, self._get(f"/me/{self._year(year)}/classes"))

    def school_class(self, class_id: int) -> m.SchoolClassDetail:
        return m.SchoolClassDetail.model_validate(self._get(f"/me/classes/{class_id}"))

    def class_students(self, class_id: int) -> list[m.Person]:
        return _parse_list(m.Person, self._get(f"/me/classes/{class_id}/students"))

    def student(self, student_id: int) -> m.PersonDetail:
        return m.PersonDetail.model_validate(self._get(f"/me/students/{student_id}"))

    # ------------------------------------------------------------ Projets

    def projects(self, year: int | None = None) -> list[m.Project]:
        return _parse_list(m.Project, self._get(f"/me/{self._year(year)}/projects"))

    def practicals(self, year: int | None = None) -> list[m.Project]:
        """Travaux pratiques (projets de type 2)."""
        return _parse_list(m.Project, self._get(f"/me/{self._year(year)}/practicals"))

    def project(self, project_id: int) -> m.Project:
        return m.Project.model_validate(self._get(f"/me/projects/{project_id}"))

    def next_project_steps(self) -> list[m.NextProjectStep]:
        return _parse_list(m.NextProjectStep, self._get("/me/nextProjectSteps"))

    def project_group_messages(self, group_id: int) -> list[dict[str, Any]]:
        """Format des messages non documenté : renvoyés tels quels."""
        return list(self._get(f"/me/projectGroups/{group_id}/messages") or [])

    def send_project_group_message(self, group_id: int, message: str) -> Any:
        """⚠️ Écriture non vérifiée. Nécessite ``allow_writes=True``."""
        self._check_write("send_project_group_message")
        return self.request(
            "POST",
            f"/me/projectGroups/{group_id}/messages",
            json={"projectGroupId": group_id, "message": message},
        )

    def join_project_group(self, rc_id: int, project_id: int, group_id: int) -> Any:
        """⚠️ Écriture non vérifiée. Nécessite ``allow_writes=True``."""
        self._check_write("join_project_group")
        return self.request("POST", f"/me/courses/{rc_id}/projects/{project_id}/groups/{group_id}")

    def leave_project_group(self, rc_id: int, project_id: int, group_id: int) -> Any:
        """⚠️ Écriture non vérifiée. Nécessite ``allow_writes=True``."""
        self._check_write("leave_project_group")
        return self.request(
            "DELETE", f"/me/courses/{rc_id}/projects/{project_id}/groups/{group_id}"
        )

    # ------------------------------------------------------------ Vie de campus

    def news(self, page: int = 0, size: int | None = None) -> m.Page[m.News]:
        params = {"page": page} | ({"size": size} if size else {})
        return m.Page[m.News].model_validate(self._get("/me/news", **params))

    def iter_news(self, size: int = 50) -> Iterator[m.News]:
        """Toutes les actualités, page après page."""
        page = 0
        while True:
            current = self.news(page, size)
            yield from current.content
            if current.last or not current.content:
                return
            page += 1

    def news_item(self, news_id: int) -> m.NewsDetail:
        return m.NewsDetail.model_validate(self._get(f"/me/news/{news_id}"))

    def banners(self, page: int = 0, size: int | None = None) -> m.Page[m.Banner]:
        params = {"page": page} | ({"size": size} if size else {})
        return m.Page[m.Banner].model_validate(self._get("/me/news/banners", **params))

    def events(self) -> list[m.CampusEvent]:
        return _parse_list(m.CampusEvent, self._get("/me/events"))

    def subscribe_event(self, event_id: int) -> Any:
        """S'inscrire à un événement (écriture). Nécessite ``allow_writes=True``."""
        self._check_write("subscribe_event")
        return self.request("POST", f"/me/events/{event_id}/subscribe")

    def unsubscribe_event(self, event_id: int) -> Any:
        """Se désinscrire d'un événement (écriture). Nécessite ``allow_writes=True``."""
        self._check_write("unsubscribe_event")
        return self.request("DELETE", f"/me/events/{event_id}/unsubscribe")

    def partners(self) -> list[m.Partner]:
        return _parse_list(m.Partner, self._get("/me/partners"))

    def schools(self) -> list[m.School]:
        return _parse_list(m.School, self._get("/me/schools"))

    def campuses(self) -> list[m.Campus]:
        return _parse_list(m.Campus, self._get("/me/campus"))


# ==================================================================== Asynchrone


class AsyncMyGES(_BaseClient):
    """Client asynchrone, mêmes méthodes que :class:`MyGES`.

    >>> async with AsyncMyGES.from_env() as ges:
    ...     notes = await ges.grades()
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(username, password, **kwargs)
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=timeout, transport=httpx.AsyncHTTPTransport(retries=2)
        )

    @classmethod
    def from_env(cls, **kwargs: Any) -> AsyncMyGES:
        """Lit ``MYGES_USERNAME``, ``MYGES_PASSWORD`` et/ou ``MYGES_TOKEN``."""
        return cls(**{**cls._env_kwargs(), **kwargs})

    async def __aenter__(self) -> AsyncMyGES:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------ Auth

    async def login(self) -> Token:
        username, password = self._credentials()
        token = await authenticate_async(self._http, username, password, self.user_agent)
        return self._remember(token)

    def logout(self) -> None:
        self._token = None
        if self.username:
            self._store.clear(self.username)

    async def get_token(self) -> Token:
        cached = self._cached_token()
        if cached is not None:
            return cached
        return await self.login()

    # ------------------------------------------------------------ HTTP

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        return self._decode(await self._send(method, path, params=params, json=json))

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        token = await self.get_token()
        response = await self._http.request(
            method, url, params=params, json=json, headers=self._headers(token)
        )
        if response.status_code == 401:
            if not self._can_login():
                raise self._no_credentials()
            token = await self.login()
            response = await self._http.request(
                method, url, params=params, json=json, headers=self._headers(token)
            )
            if response.status_code == 401:
                raise TokenExpiredError("Token refusé juste après ré-authentification.")
        return response

    async def _get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params or None)

    async def _year(self, year: int | None) -> int:
        return year if year is not None else self._year_from(await self.years())

    # ------------------------------------------------------------ Profil

    async def profile(self) -> m.Profile:
        return m.Profile.model_validate(await self._get("/me/profile"))

    async def years(self) -> list[int]:
        if self._years is None:
            self._years = list(await self._get("/me/years") or [])
        return self._years

    # ------------------------------------------------------------ Agenda

    async def agenda(self, start: When, end: When) -> list[m.AgendaEvent]:
        result = await self._get("/me/agenda", start=to_millis(start), end=to_millis(end))
        return _parse_list(m.AgendaEvent, result)

    # ------------------------------------------------------------ Scolarité

    async def courses(self, year: int | None = None) -> list[m.Course]:
        return _parse_list(m.Course, await self._get(f"/me/{await self._year(year)}/courses"))

    async def course(self, rc_id: int) -> m.CourseDetail:
        return m.CourseDetail.model_validate(await self._get(f"/me/courses/{rc_id}"))

    async def grades(self, year: int | None = None) -> list[m.Grade]:
        return _parse_list(m.Grade, await self._get(f"/me/{await self._year(year)}/grades"))

    async def absences(self, year: int | None = None) -> list[m.Absence]:
        return _parse_list(m.Absence, await self._get(f"/me/{await self._year(year)}/absences"))

    async def syllabus(self, rc_id: int) -> m.Syllabus | None:
        found = _parse_list(m.Syllabus, await self._get(f"/me/{rc_id}/syllabus"))
        return found[0] if found else None

    async def course_files(self, rc_id: int) -> list[m.CourseFile]:
        return _parse_list(m.CourseFile, await self._get(f"/me/{rc_id}/files"))

    async def download_course_file(self, rc_id: int, file_id: int) -> bytes:
        return self._content(await self._send("GET", f"/me/{rc_id}/files/{file_id}"))

    # ------------------------------------------------------------ Personnes

    async def teachers(self, year: int | None = None) -> list[m.Person]:
        return _parse_list(m.Person, await self._get(f"/me/{await self._year(year)}/teachers"))

    async def teacher(self, teacher_id: int) -> m.PersonDetail:
        return m.PersonDetail.model_validate(await self._get(f"/me/teachers/{teacher_id}"))

    async def classes(self, year: int | None = None) -> list[m.SchoolClass]:
        result = await self._get(f"/me/{await self._year(year)}/classes")
        return _parse_list(m.SchoolClass, result)

    async def school_class(self, class_id: int) -> m.SchoolClassDetail:
        return m.SchoolClassDetail.model_validate(await self._get(f"/me/classes/{class_id}"))

    async def class_students(self, class_id: int) -> list[m.Person]:
        return _parse_list(m.Person, await self._get(f"/me/classes/{class_id}/students"))

    async def student(self, student_id: int) -> m.PersonDetail:
        return m.PersonDetail.model_validate(await self._get(f"/me/students/{student_id}"))

    # ------------------------------------------------------------ Projets

    async def projects(self, year: int | None = None) -> list[m.Project]:
        return _parse_list(m.Project, await self._get(f"/me/{await self._year(year)}/projects"))

    async def practicals(self, year: int | None = None) -> list[m.Project]:
        result = await self._get(f"/me/{await self._year(year)}/practicals")
        return _parse_list(m.Project, result)

    async def project(self, project_id: int) -> m.Project:
        return m.Project.model_validate(await self._get(f"/me/projects/{project_id}"))

    async def next_project_steps(self) -> list[m.NextProjectStep]:
        return _parse_list(m.NextProjectStep, await self._get("/me/nextProjectSteps"))

    async def project_group_messages(self, group_id: int) -> list[dict[str, Any]]:
        return list(await self._get(f"/me/projectGroups/{group_id}/messages") or [])

    async def send_project_group_message(self, group_id: int, message: str) -> Any:
        self._check_write("send_project_group_message")
        return await self.request(
            "POST",
            f"/me/projectGroups/{group_id}/messages",
            json={"projectGroupId": group_id, "message": message},
        )

    async def join_project_group(self, rc_id: int, project_id: int, group_id: int) -> Any:
        self._check_write("join_project_group")
        return await self.request(
            "POST", f"/me/courses/{rc_id}/projects/{project_id}/groups/{group_id}"
        )

    async def leave_project_group(self, rc_id: int, project_id: int, group_id: int) -> Any:
        self._check_write("leave_project_group")
        return await self.request(
            "DELETE", f"/me/courses/{rc_id}/projects/{project_id}/groups/{group_id}"
        )

    # ------------------------------------------------------------ Vie de campus

    async def news(self, page: int = 0, size: int | None = None) -> m.Page[m.News]:
        params = {"page": page} | ({"size": size} if size else {})
        return m.Page[m.News].model_validate(await self._get("/me/news", **params))

    async def iter_news(self, size: int = 50) -> AsyncIterator[m.News]:
        page = 0
        while True:
            current = await self.news(page, size)
            for item in current.content:
                yield item
            if current.last or not current.content:
                return
            page += 1

    async def news_item(self, news_id: int) -> m.NewsDetail:
        return m.NewsDetail.model_validate(await self._get(f"/me/news/{news_id}"))

    async def banners(self, page: int = 0, size: int | None = None) -> m.Page[m.Banner]:
        params = {"page": page} | ({"size": size} if size else {})
        return m.Page[m.Banner].model_validate(await self._get("/me/news/banners", **params))

    async def events(self) -> list[m.CampusEvent]:
        return _parse_list(m.CampusEvent, await self._get("/me/events"))

    async def subscribe_event(self, event_id: int) -> Any:
        self._check_write("subscribe_event")
        return await self.request("POST", f"/me/events/{event_id}/subscribe")

    async def unsubscribe_event(self, event_id: int) -> Any:
        self._check_write("unsubscribe_event")
        return await self.request("DELETE", f"/me/events/{event_id}/unsubscribe")

    async def partners(self) -> list[m.Partner]:
        return _parse_list(m.Partner, await self._get("/me/partners"))

    async def schools(self) -> list[m.School]:
        return _parse_list(m.School, await self._get("/me/schools"))

    async def campuses(self) -> list[m.Campus]:
        return _parse_list(m.Campus, await self._get("/me/campus"))
