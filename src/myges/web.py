"""Session sur le site ``myges.fr`` (JSF + CAS), pour ce que l'API mobile ne fait pas.

Seul usage pour l'instant : le **dépôt d'un livrable** sur une étape de projet. L'API
``api.kordis.fr`` n'a aucune route d'envoi (voir ``docs/api-notes.md``, 2026-11-10) ; le site
le fait en trois temps, reproduits ici tels que les fait le navigateur :

1. ouvrir le panneau « Ajouter un livrable » de l'étape (requête JSF partielle) ;
2. envoyer le fichier à ``/event/chunkupload`` (jQuery File Upload, morceaux de 2 Mo, clé =
   MD5 du fichier entier), qui répond ``{"files": [{"hash", "path", "type", "extension"}]}`` ;
3. rattacher le fichier au formulaire (``saveStepFile``), puis « Valider le livrable » avec
   l'intitulé et la description.

La connexion passe par le CAS ``ges-cas.kordis.fr`` : il faut le **mot de passe**, le token de
l'API n'y est pas accepté. Les identifiants JSF (``j_idt309``…) sont relus dans les pages, pas
codés en dur : ils changent quand l'école modifie ses vues.
"""

from __future__ import annotations

import hashlib
import html
import mimetypes
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .client import DEFAULT_USER_AGENT
from .errors import InvalidCredentialsError, MyGESError, WriteNotAllowedError

WEB_URL = "https://myges.fr"
CHUNK_SIZE = 2 * 1024 * 1024  # maxChunkSize du composant d'envoi
MAX_FILE_SIZE = 300 * 1024 * 1024  # « La taille maximale autorisée est de 300.0 Mo. »
TITLE_MAX = 70  # maxlength du champ « Intitulé »

FORM = "projectGroupGestionForm"
WIDGET = f"{FORM}:projectGroupGestionWidget"
TABS = f"{WIDGET}:tabListStep"
PANEL = f"{WIDGET}:panelAddStepFile"
TITLE_FIELD = f"{WIDGET}:projectStepFileTitle"
DESC_FIELD = f"{WIDGET}:projectStepFileDesc"

# encodeURI de JavaScript : ce qui reste tel quel dans Content-Disposition.
_ENCODE_URI_SAFE = ";,/?:@&=+$-_.!~*'()#"

Progress = Callable[[int, int], None]


class WebError(MyGESError):
    """Réponse inattendue du site myges.fr (page changée, session perdue, refus)."""


@dataclass(frozen=True)
class StepTab:
    """Onglet « Etape n°X » de la page de suivi du groupe."""

    index: int
    number: int | None
    label: str


@dataclass(frozen=True)
class UploadResult:
    """Livrable validé par le site."""

    md5: str
    size: int
    title: str
    messages: list[str]


def md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _text(fragment: str) -> str:
    fragment = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _view_state(page: str) -> str | None:
    found = re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]*)"', page)
    return html.unescape(found.group(1)) if found else None


def step_tabs(page: str) -> list[StepTab]:
    """Onglets des étapes, dans l'ordre de la page."""
    tabs = []
    for index, label in re.findall(
        rf'<a href="#{re.escape(TABS)}:(\d+):tab"[^>]*>(.*?)</a>', page, flags=re.S
    ):
        text = _text(label)
        number = re.search(r"(\d+)\s*$", text)
        tabs.append(StepTab(int(index), int(number.group(1)) if number else None, text))
    return tabs


def _deadline_text(when: datetime) -> str:
    return f"Date Limite : {when:%d/%m/%Y %Hh%M}"


@dataclass
class _Partial:
    """Réponse JSF partielle (``<partial-response>``) décodée."""

    updates: dict[str, str]
    redirect: str | None
    error: str | None

    @classmethod
    def parse(cls, body: str) -> _Partial:
        updates = dict(
            re.findall(r'<update id="([^"]+)"><!\[CDATA\[(.*?)\]\]></update>', body, flags=re.S)
        )
        redirect = re.search(r'<redirect url="([^"]+)"', body)
        error = re.search(r"<error-message><!\[CDATA\[(.*?)\]\]>", body, flags=re.S)
        return cls(
            updates,
            html.unescape(redirect.group(1)) if redirect else None,
            error.group(1).strip() if error else None,
        )

    @property
    def html(self) -> str:
        return "\n".join(self.updates.values())

    def messages(self) -> list[tuple[str, str]]:
        """(sévérité, texte) des messages JSF affichés (growl ou ``ui-messages``)."""
        found = []
        for severity, detail in re.findall(
            r'ui-messages-(info|warn|error|fatal)-(?:summary|detail)">(.*?)</span>',
            self.html,
            flags=re.S,
        ):
            text = _text(detail)
            if text and (severity, text) not in found:
                found.append((severity, text))
        # Growl : messages en JSON dans le script de création du widget.
        for msgs in re.findall(r"msgs:\[(.*?)\]\}", self.html, flags=re.S):
            for item in re.findall(r"\{(.*?)\}", msgs, flags=re.S):
                values = dict(re.findall(r"(\w+):[\"']((?:[^\"'\\]|\\.)*)[\"']", item))
                severity = values.get("severity", "info").lower()
                text = _text(values.get("detail") or values.get("summary") or "")
                if text and (severity, text) not in found:
                    found.append((severity, text))
        return found


class MyGESWeb:
    """Client du site ``myges.fr`` connecté par le CAS.

    >>> with MyGESWeb("p.nom", "mot de passe", allow_writes=True) as web:
    ...     web.upload_step_file(527020, Path("rendu.pdf"), step_number=1)
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        base_url: str = WEB_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        allow_writes: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.username = username
        self._password = password
        self.base_url = base_url.rstrip("/")
        self.allow_writes = allow_writes
        self._http = httpx.Client(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=httpx.Timeout(30, write=120),
            transport=transport,
        )
        self._logged_in = False

    def __enter__(self) -> MyGESWeb:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(username={self.username!r})"

    # ------------------------------------------------------------ Session

    @staticmethod
    def _is_login_page(response: httpx.Response) -> bool:
        return 'id="fm1"' in response.text and 'name="lt"' in response.text

    def _login(self, response: httpx.Response) -> httpx.Response:
        """Remplit le formulaire CAS reçu à la place de la page demandée."""
        form = re.search(r'<form[^>]*id="fm1".*?</form>', response.text, flags=re.S)
        if form is None:
            raise WebError("page de connexion CAS méconnaissable")
        fields = {
            name: html.unescape(value)
            for name, value in re.findall(
                r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form.group(0)
            )
        }
        fields.update(username=self.username, password=self._password)
        action = re.search(r'action="([^"]*)"', form.group(0))
        target = response.url.join(html.unescape(action.group(1)) if action else "")
        answer = self._http.post(target, data=fields)
        if self._is_login_page(answer):
            raise InvalidCredentialsError("identifiant ou mot de passe refusé par le CAS myGES")
        self._logged_in = True
        return answer

    def get_page(self, path: str) -> httpx.Response:
        """Page du site, en se connectant d'abord si le CAS le demande."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        response = self._http.get(url)
        if self._is_login_page(response):
            self._login(response)
            response = self._http.get(url)  # le CAS ramène à l'accueil, pas à la page
            if self._is_login_page(response):
                raise WebError("connexion CAS acceptée mais session web absente")
        if response.status_code >= 400:
            raise WebError(f"{response.status_code} sur {response.url.path}")
        return response

    # ------------------------------------------------------------ JSF

    def _ajax(self, url: str, state: dict[str, str], source: str, **fields: str) -> _Partial:
        """Requête JSF partielle (PrimeFaces), avec le ViewState courant de la vue."""
        data = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": source,
            "javax.faces.partial.execute": fields.pop("execute", source),
            FORM: FORM,
            "javax.faces.ViewState": state["view"],
        }
        if "render" in fields:
            data["javax.faces.partial.render"] = fields.pop("render")
        data.update(fields)
        response = self._http.post(
            url,
            data=data,
            headers={"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest"},
        )
        if response.status_code >= 400:
            raise WebError(f"{response.status_code} sur {response.url.path}")
        partial = _Partial.parse(response.text)
        if partial.redirect:
            raise WebError(f"session web perdue (redirigé vers {partial.redirect})")
        if partial.error:
            raise WebError(f"erreur du site : {partial.error}")
        for ident, content in partial.updates.items():
            if "javax.faces.ViewState" in ident:
                state["view"] = content.strip()
        return partial

    def _select_tab(
        self, url: str, page: str, state: dict[str, str], number: int, deadline: datetime | None
    ) -> tuple[int, str]:
        """Index et contenu de l'onglet de l'étape ``number`` (départagé par la date limite)."""
        tabs = [t for t in step_tabs(page) if t.number == number]
        if not tabs:
            found = ", ".join(t.label for t in step_tabs(page)) or "aucune"
            raise WebError(f"étape n°{number} absente de la page (étapes : {found})")
        for tab in tabs:
            content = self._tab_content(url, page, state, tab.index)
            if len(tabs) == 1 or deadline is None or _deadline_text(deadline) in _text(content):
                return tab.index, content
        raise WebError(f"plusieurs étapes n°{number}, aucune avec cette date limite")

    def _tab_content(self, url: str, page: str, state: dict[str, str], index: int) -> str:
        tab_id = f"{TABS}:{index}:tab"
        rendered = re.search(rf'<div id="{re.escape(tab_id)}"(.*?)role="tabpanel"', page)
        if rendered and "ui-helper-hidden" not in rendered.group(1):
            start = page.find(f'<div id="{tab_id}"')
            return page[start:]  # onglet actif : déjà dans la page
        partial = self._ajax(
            url, state, TABS,
            render=TABS,
            **{f"{TABS}_contentLoad": "true", f"{TABS}_newTab": tab_id,
               f"{TABS}_tabindex": str(index), f"{TABS}_activeIndex": str(index)},
        )  # fmt: skip
        return partial.html

    # ------------------------------------------------------------ Dépôt

    def upload_step_file(
        self,
        group_id: int,
        path: Path,
        *,
        step_number: int,
        deadline: datetime | None = None,
        title: str | None = None,
        description: str = "",
        progress: Progress | None = None,
    ) -> UploadResult:
        """Dépose ``path`` comme livrable de l'étape ``step_number`` du groupe ``group_id``.

        ``deadline`` (``ProjectStep.psp_limit_date``) départage deux étapes de même numéro.
        Le livrable est visible par l'intervenant et par tout le groupe ; il se supprime
        depuis le site.
        """
        if not self.allow_writes:
            raise WriteNotAllowedError(
                "upload_step_file dépose un livrable sur myGES : créer le client avec "
                "allow_writes=True pour l'autoriser."
            )
        path = Path(path)
        size = path.stat().st_size
        if "." not in path.name or len(path.suffix) > 10:
            raise ValueError("myGES exige un fichier avec une extension (ex. .pdf, .tar.gz)")
        if size > MAX_FILE_SIZE:
            raise ValueError("myGES refuse les fichiers de plus de 300 Mo")
        title = (title or path.name).strip()[:TITLE_MAX]

        url = f"{self.base_url}/common/project-group-gestion/{group_id}"
        page = self.get_page(f"/common/project-group-gestion/{group_id}").text
        view = _view_state(page)
        if view is None or FORM not in page:
            raise WebError(f"page de suivi du groupe {group_id} méconnaissable")
        state = {"view": view}

        index, content = self._select_tab(url, page, state, step_number, deadline)
        button = f"{TABS}:{index}:addStepFileButton"
        if button not in content:
            raise WebError(f"pas de bouton « Ajouter un livrable » sur l'étape n°{step_number}")
        fields = {TITLE_FIELD: title, DESC_FIELD: description}
        tab = {f"{TABS}_activeIndex": str(index)}
        panel = self._ajax(url, state, button, render=PANEL, **{button: button}, **tab)
        remote = re.search(
            r"saveStepFile = function\(\) \{PrimeFaces\.ab\(\{s:'([^']+)'", panel.html
        )
        save = re.search(rf'id="({re.escape(WIDGET)}:saveDocument)"', panel.html)
        if remote is None or save is None:
            raise WebError("panneau « Ajout d'un livrable » méconnaissable")

        md5 = md5_file(path)
        stored = self._send_file(path, size, md5, progress)

        attach = remote.group(1)
        self._ajax(
            url, state, attach, render=save.group(1),
            **{attach: attach, "hash": stored.get("hash", md5), "path": stored.get("path", ""),
               "type": stored.get("type", ""), "extension": stored.get("extension", "")},
            **fields, **tab,
        )  # fmt: skip
        done = self._ajax(
            url, state, save.group(1),
            execute=PANEL,
            render=f"{WIDGET}:panelStep {WIDGET}:uploadMessages {WIDGET}:outputPanelGroupLogs",
            **{save.group(1): save.group(1)}, **fields, **tab,
        )  # fmt: skip
        messages = done.messages()
        errors = [text for severity, text in messages if severity in ("error", "fatal")]
        if errors:
            raise WebError("livrable refusé : " + " ; ".join(errors))
        return UploadResult(md5, size, title, [text for _, text in messages])

    def _send_file(
        self, path: Path, size: int, md5: str, progress: Progress | None
    ) -> dict[str, Any]:
        """Envoi à ``/event/chunkupload`` comme jQuery File Upload 5.37.

        Fichier ≤ 2 Mo : une seule requête. Au-delà : morceaux de 2 Mo avec ``Content-Range``
        et ``Content-Disposition``, le dernier morceau renvoyant la description du fichier.
        """
        url = f"{self.base_url}/event/chunkupload"
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        result: dict[str, Any] = {}
        sent = 0
        with path.open("rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
                if size > CHUNK_SIZE:
                    headers["Content-Range"] = f"bytes {sent}-{sent + len(chunk) - 1}/{size}"
                    headers["Content-Disposition"] = (
                        f'attachment; filename="{quote(path.name, safe=_ENCODE_URI_SAFE)}"'
                    )
                    kind = "application/octet-stream"
                else:
                    kind = mime
                response = self._http.post(
                    url,
                    data={"hashes": md5},
                    files={"files[]": (path.name, chunk, kind)},
                    headers=headers,
                )
                if response.status_code >= 400:
                    raise WebError(f"envoi refusé ({response.status_code}) sur /event/chunkupload")
                sent += len(chunk)
                if progress:
                    progress(sent, size)
                if sent >= size:
                    try:
                        files = response.json().get("files") or []
                    except ValueError as exc:
                        raise WebError("réponse d'envoi illisible (session web perdue ?)") from exc
                    result = files[0] if files else {}
                    if result.get("error"):
                        raise WebError(f"envoi refusé : {result['error']}")
                    return result
