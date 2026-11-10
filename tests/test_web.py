"""Dépôt de livrables par le site myges.fr (CAS + JSF), contre un faux site."""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import fixture
from myges.cli import app
from myges.client import API_URL
from myges.errors import InvalidCredentialsError, WriteNotAllowedError
from myges.web import CHUNK_SIZE, MyGESWeb, WebError, step_tabs

CAS = "https://ges-cas.kordis.fr/login?service=https%3A%2F%2Fmyges.fr%2Fj_spring_cas_security_check"
GROUP = 520001
W = "projectGroupGestionForm:projectGroupGestionWidget"
T = f"{W}:tabListStep"

LOGIN_PAGE = f"""<html><body><form id="fm1" class="fm-v clearfix"
 action="/login?service=https%3A%2F%2Fmyges.fr%2Fj_spring_cas_security_check" method="post">
<input id="username" name="username" type="text" value="" size="25"/>
<input id="password" name="password" type="password" value="" size="25"/>
<input type="hidden" name="lt" value="LT-1-abc-cas"/> <input type="hidden" name="execution"
 value="e1s1"/> <input type="hidden" name="_eventId" value="submit"/>
<input class="input_submit" name="submit" value="CONNEXION" type="submit"/></form>
<a href="{CAS}">x</a></body></html>"""


def _tab(index: int, deadline: str) -> str:
    return (
        f'<div id="{T}:{index}:tab" class="ui-tabs-panel ui-widget-content" role="tabpanel">'
        f"Description : Rendu. Date Limite : {deadline}"
        f'<button id="{T}:{index}:addStepFileButton" name="{T}:{index}:addStepFileButton">'
        "Ajouter un livrable</button></div>"
    )


def _group_page(view: str) -> str:
    return f"""<html><body>
<form id="projectGroupGestionForm" name="projectGroupGestionForm" method="post"
 enctype="multipart/form-data">
<ul><li role="tab"><a href="#{T}:0:tab"><span>Etape n°1</span></a></li>
<li role="tab"><a href="#{T}:1:tab"><span>Etape n°2</span></a></li></ul>
{_tab(0, "15/11/2026 07h00")}
<div id="{T}:1:tab" class="ui-tabs-panel ui-widget-content ui-helper-hidden" role="tabpanel"
 aria-hidden="true"></div>
<input type="hidden" name="javax.faces.ViewState" id="javax.faces.ViewState" value="{view}" />
</form></body></html>"""


PANEL = f"""<div id="{W}:panelAddStepFile"><input id="{W}:projectStepFileTitle"
 name="{W}:projectStepFileTitle" maxlength="70"/>
<script>saveStepFile = function() {{PrimeFaces.ab({{s:'{W}:j_idt309',f:'projectGroupGestionForm',
p:'{W}:j_idt309',u:'{W}:saveDocument',pa:arguments[0]}});}}</script>
<button id="{W}:saveDocument" name="{W}:saveDocument" disabled="disabled">Valider</button></div>"""


def _partial(updates: dict[str, str]) -> httpx.Response:
    body = "".join(f'<update id="{k}"><![CDATA[{v}]]></update>' for k, v in updates.items())
    return httpx.Response(
        200,
        text=f"<?xml version='1.0' encoding='UTF-8'?><partial-response><changes>{body}"
        "</changes></partial-response>",
        headers={"content-type": "text/xml"},
    )


class FakeSite:
    """Le strict nécessaire de myges.fr : CAS, page du groupe, requêtes JSF, envoi par morceaux."""

    def __init__(self, password: str = "secret", save_message: str | None = None) -> None:
        self.password = password
        self.save_message = save_message
        self.logged_in = False
        self.view = 0
        self.ajax: list[dict[str, str]] = []
        self.chunks: list[tuple[dict[str, str], bytes, dict[str, str]]] = []

    def install(self, router: respx.MockRouter) -> None:
        router.get(url__regex=r"https://ges-cas\.kordis\.fr/login.*").mock(
            side_effect=lambda r: httpx.Response(200, text=LOGIN_PAGE)
        )
        router.post(url__regex=r"https://ges-cas\.kordis\.fr/login.*").mock(side_effect=self.cas)
        router.get(f"https://myges.fr/common/project-group-gestion/{GROUP}").mock(
            side_effect=self.page
        )
        router.get("https://myges.fr/student/home").mock(
            return_value=httpx.Response(200, text="<html>accueil</html>")
        )
        router.post(f"https://myges.fr/common/project-group-gestion/{GROUP}").mock(
            side_effect=self.jsf
        )
        router.post("https://myges.fr/event/chunkupload").mock(side_effect=self.chunk)

    def cas(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["lt"] == "LT-1-abc-cas"
        assert form["execution"] == "e1s1"
        if form.get("password") != self.password:
            refused = LOGIN_PAGE.replace("<form", "Identifiants invalides<form")
            return httpx.Response(200, text=refused)
        self.logged_in = True
        return httpx.Response(302, headers={"location": "https://myges.fr/student/home"})

    def page(self, request: httpx.Request) -> httpx.Response:
        if not self.logged_in:
            return httpx.Response(302, headers={"location": CAS})
        self.view += 1
        return httpx.Response(200, text=_group_page(f"v{self.view}"))

    def jsf(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Faces-Request"] == "partial/ajax"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["javax.faces.ViewState"] == f"v{self.view}"
        self.ajax.append(form)
        source = form["javax.faces.source"]
        if source == T:
            index = int(form[f"{T}_tabindex"])
            return _partial({T: _tab(index, "20/12/2026 23h59")})
        if source.endswith("addStepFileButton"):
            return _partial({f"{W}:panelAddStepFile": PANEL})
        if source == f"{W}:j_idt309":
            return _partial({f"{W}:saveDocument": f'<button id="{W}:saveDocument">V</button>'})
        if source == f"{W}:saveDocument":
            msgs = ""
            if self.save_message:
                msgs = (
                    "<script>PrimeFaces.cw('Growl','g',{id:'g',msgs:[{summary:\"\","
                    f"detail:\"{self.save_message}\",severity:'error'}}]}});</script>"
                )
            return _partial({f"{W}:panelStep": "<div>ok</div>", f"{W}:uploadMessages": msgs})
        raise AssertionError(source)

    def chunk(self, request: httpx.Request) -> httpx.Response:
        boundary = request.headers["content-type"].split("boundary=")[1].encode()
        fields: dict[str, str] = {}
        content = b""
        for part in request.content.split(b"--" + boundary):
            if b"\r\n\r\n" not in part:
                continue
            head, body = part.split(b"\r\n\r\n", 1)
            body = body.rsplit(b"\r\n", 1)[0]
            name = re.search(rb'name="([^"]+)"', head).group(1).decode()  # type: ignore[union-attr]
            if name == "files[]":
                content = body
                fields["filename"] = re.search(rb'filename="([^"]+)"', head).group(1).decode()  # type: ignore[union-attr]
                fields["type"] = re.search(rb"Content-Type: (\S+)", head).group(1).decode()  # type: ignore[union-attr]
            else:
                fields[name] = body.decode()
        self.chunks.append((fields, content, dict(request.headers)))
        info = {"hash": fields["hashes"], "path": "/tmp/up/abc", "type": "x", "extension": ".pdf"}
        return httpx.Response(200, json={"files": [info]})

    def uploaded(self) -> bytes:
        return b"".join(content for _, content, _ in self.chunks)


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "rapport final.pdf"
    path.write_bytes(b"%PDF-1.7 rapport")
    return path


def test_upload_small_file(report: Path) -> None:
    site = FakeSite()
    with respx.mock(assert_all_called=False) as router:
        site.install(router)
        with MyGESWeb("jdupont", "secret", allow_writes=True) as web:
            result = web.upload_step_file(GROUP, report, step_number=1, description="v1")
    md5 = hashlib.md5(report.read_bytes()).hexdigest()
    assert result.md5 == md5
    assert result.title == "rapport final.pdf"
    # Fichier ≤ 2 Mo : une seule requête, sans Content-Range, avec le vrai type MIME.
    [(fields, content, headers)] = site.chunks
    assert content == report.read_bytes()
    assert fields == {"hashes": md5, "filename": "rapport final.pdf", "type": "application/pdf"}
    assert "content-range" not in headers
    add, attach, save = site.ajax
    assert add["javax.faces.source"] == f"{T}:0:addStepFileButton"
    assert attach["hash"] == md5
    assert attach["path"] == "/tmp/up/abc"
    assert attach["extension"] == ".pdf"
    assert save["javax.faces.partial.execute"] == f"{W}:panelAddStepFile"
    assert save[f"{W}:projectStepFileTitle"] == "rapport final.pdf"
    assert save[f"{W}:projectStepFileDesc"] == "v1"


def test_upload_in_chunks_on_second_step(tmp_path: Path) -> None:
    big = tmp_path / "configs.tar.gz"
    big.write_bytes(bytes(range(256)) * (CHUNK_SIZE // 256) + b"fin")
    site = FakeSite()
    seen: list[int] = []
    with respx.mock(assert_all_called=False) as router:
        site.install(router)
        with MyGESWeb("jdupont", "secret", allow_writes=True) as web:
            web.upload_step_file(
                GROUP, big, step_number=2, title="Configs", progress=lambda s, _t: seen.append(s)
            )
    assert site.uploaded() == big.read_bytes()
    size = big.stat().st_size
    ranges = [headers["content-range"] for _, _, headers in site.chunks]
    assert ranges == [f"bytes 0-{CHUNK_SIZE - 1}/{size}", f"bytes {CHUNK_SIZE}-{size - 1}/{size}"]
    assert all(f["type"] == "application/octet-stream" for f, _, _ in site.chunks)
    assert all('filename="configs.tar.gz"' in h["content-disposition"] for _, _, h in site.chunks)
    assert seen == [CHUNK_SIZE, size]
    # L'onglet de l'étape 2 est chargé avant d'ouvrir son panneau.
    assert site.ajax[0][f"{T}_newTab"] == f"{T}:1:tab"
    assert site.ajax[1]["javax.faces.source"] == f"{T}:1:addStepFileButton"
    assert site.ajax[-1][f"{W}:projectStepFileTitle"] == "Configs"


def test_upload_refused_by_site(report: Path) -> None:
    site = FakeSite(save_message="Fichier invalide")
    with respx.mock(assert_all_called=False) as router:
        site.install(router)
        with (
            MyGESWeb("jdupont", "secret", allow_writes=True) as web,
            pytest.raises(WebError, match="Fichier invalide"),
        ):
            web.upload_step_file(GROUP, report, step_number=1)


def test_upload_guards(report: Path, tmp_path: Path) -> None:
    site = FakeSite()
    noext = tmp_path / "README"
    noext.write_text("x")
    with respx.mock(assert_all_called=False) as router:
        site.install(router)
        with MyGESWeb("jdupont", "secret") as web, pytest.raises(WriteNotAllowedError):
            web.upload_step_file(GROUP, report, step_number=1)
        with MyGESWeb("jdupont", "faux", allow_writes=True) as web:
            with pytest.raises(InvalidCredentialsError):
                web.upload_step_file(GROUP, report, step_number=1)
            with pytest.raises(ValueError, match="extension"):
                web.upload_step_file(GROUP, noext, step_number=1)
        with (
            MyGESWeb("jdupont", "secret", allow_writes=True) as web,
            pytest.raises(WebError, match="étape n°3 absente"),
        ):
            web.upload_step_file(GROUP, report, step_number=3)
    assert site.chunks == []


def test_step_tabs() -> None:
    assert [(t.index, t.number) for t in step_tabs(_group_page("v"))] == [(0, 1), (1, 2)]


# ---------------------------------------------------------------- CLI


def _mock_api(router: respx.MockRouter, project: dict[str, Any]) -> None:
    from conftest import mock_api

    mock_api(router, fixture("getGrades"))
    router.get(f"{API_URL}/me/profile").mock(
        return_value=httpx.Response(200, json=fixture("getProfile"))
    )
    router.get(f"{API_URL}/me/2026/projects").mock(
        return_value=httpx.Response(200, json=fixture("getProjects"))
    )
    router.get(f"{API_URL}/me/projects/24001").mock(return_value=httpx.Response(200, json=project))
    router.get(f"{API_URL}/me/projectGroups/{GROUP}/messages").mock(
        return_value=httpx.Response(200, json=fixture("getProjectGroupMessages"))
    )


def _with_hash(md5: str) -> dict[str, Any]:
    project = copy.deepcopy(fixture("getProject"))
    step = project["result"]["steps"][0]
    step["files"].append({**step["files"][0], "psf_id": 2, "psf_file_hash": md5})
    return project


def test_cli_upload(home: Path, report: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYGES_PASSWORD", "secret")
    site = FakeSite()
    md5 = hashlib.md5(report.read_bytes()).hexdigest()
    with respx.mock(assert_all_called=False) as router:
        _mock_api(router, _with_hash(md5))
        site.install(router)
        result = CliRunner().invoke(
            app, ["projects", "--inspect", "24001", "--upload", str(report), "--yes"],
            env={"COLUMNS": "200"},
        )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "étape 1 · Rendu final" in result.output
    assert "Camille EXEMPLE" in result.output  # membres prévenus avant le dépôt
    assert "rapport final.pdf déposé" in result.output
    assert "Vérifié dans myGES" in result.output
    assert site.uploaded() == report.read_bytes()


def test_cli_upload_not_visible_and_declined(
    home: Path, report: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MYGES_PASSWORD", "secret")
    site = FakeSite()
    with respx.mock(assert_all_called=False) as router:
        _mock_api(router, fixture("getProject"))
        site.install(router)
        declined = CliRunner().invoke(
            app, ["projects", "--inspect", "24001", "--upload", str(report)], input="n\n"
        )
        assert site.chunks == []
        unseen = CliRunner().invoke(
            app, ["projects", "--inspect", "24001", "--upload", str(report)], input="y\n"
        )
    assert declined.exit_code == 1
    assert unseen.exit_code == 0, unseen.output
    assert "Pas encore visible" in unseen.output


def test_cli_upload_option_errors(home: Path, report: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_api(router, fixture("getProject"))
        alone = CliRunner().invoke(app, ["projects", "--upload", str(report)])
        titled = CliRunner().invoke(
            app,
            ["projects", "-i", "24001", "--upload", str(report), "--upload", str(report),
             "--title", "x"],
        )  # fmt: skip
        step_only = CliRunner().invoke(app, ["projects", "-i", "24001", "--step", "2"])
    assert alone.exit_code == titled.exit_code == step_only.exit_code == 2
