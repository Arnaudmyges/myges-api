"""E-mails aux intervenants et étudiants, lecture des actualités, `notify serve`."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from typer.testing import CliRunner

from conftest import fixture
from myges import mail
from myges.cli import _html_links, _html_to_text, app
from myges.client import API_URL

runner = CliRunner()


@pytest.fixture
def no_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []
    monkeypatch.setattr(mail, "open_url", lambda url: opened.append(url) or False)
    return opened


def people_api(router: respx.MockRouter) -> None:
    def get(path: str, name: str) -> None:
        router.get(f"{API_URL}{path}").mock(return_value=httpx.Response(200, json=fixture(name)))

    teachers = copy.deepcopy(fixture("getTeachers"))
    grace = {
        **teachers["result"][0],
        "uid": 1000000002,
        "firstname": "Grace",
        "lastname": "HOPPER",
        "email": "ghopper@example.org",
    }
    teachers["result"].append(grace)
    router.get(f"{API_URL}/me/2026/teachers").mock(return_value=httpx.Response(200, json=teachers))
    get("/me/years", "getYears")
    get("/me/2026/courses", "getCourses")
    get("/me/2026/classes", "getClasses")
    get("/me/classes/20001/students", "getClassStudents")
    get("/me/profile", "getProfile")


# ---------------------------------------------------------------- Lien de brouillon


def test_compose_url_outlook() -> None:
    url = mail.compose_url(["a@x.fr", "b@x.fr"], "Question TP", "Bonjour,\nMerci & à bientôt")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == mail.OUTLOOK_COMPOSE
    assert "+" not in parts.query  # espaces en %20
    query = parse_qs(parts.query)
    assert query == {
        "to": ["a@x.fr,b@x.fr"],
        "subject": ["Question TP"],
        "body": ["Bonjour,\nMerci & à bientôt"],
    }


def test_compose_url_mailto() -> None:
    assert mail.compose_url(["a@x.fr"], client="mailto") == "mailto:a@x.fr"
    assert mail.compose_url(["a@x.fr"], "Objet ?", client="mailto") == (
        "mailto:a@x.fr?subject=Objet%20%3F"
    )


def test_open_url_headless_linux_prints_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail.platform, "system", lambda: "Linux")
    monkeypatch.setattr(mail, "_is_wsl", lambda: False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(mail.webbrowser, "open", lambda url: pytest.fail("navigateur lancé"))
    assert mail.open_url("https://x.test") is False


# ---------------------------------------------------------------- teachers / students


def test_teachers_lists_subjects(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        people_api(router)
        result = runner.invoke(app, ["teachers"])
    assert result.exit_code == 0, result.output
    assert "Matières" in result.output
    assert "Réseaux avancés" in result.output
    assert "Anglais" in result.output


def test_teachers_mail_by_subject(home: Path, no_browser: list[str]) -> None:
    with respx.mock(assert_all_called=False) as router:
        people_api(router)
        result = runner.invoke(app, ["teachers", "--mail", "anglais", "-s", "Absence"])
    assert result.exit_code == 0, result.output
    assert "Grace HOPPER" in result.output
    query = parse_qs(urlsplit(no_browser[0]).query)
    assert query["to"] == ["ghopper@example.org"]
    assert query["subject"] == ["Absence"]


def test_teachers_mail_several_and_ambiguous(home: Path, no_browser: list[str]) -> None:
    with respx.mock(assert_all_called=False) as router:
        people_api(router)
        several = runner.invoke(app, ["teachers", "--mail", "lovelace, hopper"])
        ambiguous = runner.invoke(app, ["teachers", "--mail", "t1"])
        unknown = runner.invoke(app, ["teachers", "--mail", "turing"])
    assert several.exit_code == 0, several.output
    assert parse_qs(urlsplit(no_browser[0]).query)["to"] == [
        "alovelace@example.org,ghopper@example.org"
    ]
    assert ambiguous.exit_code == 2
    assert "ambigu" in ambiguous.output
    assert unknown.exit_code == 2
    assert len(no_browser) == 1


def test_students_default_class_and_mail_all_without_me(home: Path, no_browser: list[str]) -> None:
    with respx.mock(assert_all_called=False) as router:
        people_api(router)
        listing = runner.invoke(app, ["students"])
        result = runner.invoke(app, ["students", "--mail", "tous"])
    assert listing.exit_code == 0, listing.output
    assert "EXEMPLE" in listing.output
    assert result.exit_code == 0, result.output
    me = fixture("getProfile")["result"]["uid"]
    others = [s["email"] for s in fixture("getClassStudents")["result"] if s["uid"] != me]
    assert parse_qs(urlsplit(no_browser[0]).query)["to"] == [",".join(others)]


def test_students_asks_for_class_when_several(home: Path) -> None:
    classes = copy.deepcopy(fixture("getClasses"))
    classes["result"].append({**classes["result"][0], "puid": 20002, "name": "Autre"})
    with respx.mock(assert_all_called=False) as router:
        people_api(router)
        router.get(f"{API_URL}/me/2026/classes").mock(
            return_value=httpx.Response(200, json=classes)
        )
        result = runner.invoke(app, ["students"])
    assert result.exit_code == 2
    assert "20002" in result.output


# ---------------------------------------------------------------- news --inspect


NEWS_HTML = (
    "<h2>Programme</h2>\n<ul>\n<li><span>Atelier &eacute;thique</span></li>\n"
    '<li>Conf&eacute;rence <a href="https://example.org/conf">d&eacute;tails</a></li>\n</ul>\n'
    '<p>Voir le <a href="https://example.org/">site</a>.</p>'
)


def test_html_to_text_lists_and_headings() -> None:
    assert _html_to_text(NEWS_HTML) == (
        "Programme\n• Atelier éthique\n• Conférence détails\nVoir le site."
    )
    assert _html_links(NEWS_HTML) == [
        ("détails", "https://example.org/conf"),
        ("site", "https://example.org/"),
    ]


def test_news_inspect_by_id_and_title(home: Path) -> None:
    item = copy.deepcopy(fixture("getNewsItem"))
    item["result"]["html"] = NEWS_HTML
    news = fixture("getNews")
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API_URL}/me/news/4001").mock(return_value=httpx.Response(200, json=item))
        router.get(f"{API_URL}/me/news").mock(return_value=httpx.Response(200, json=news))
        by_id = runner.invoke(app, ["news", "--inspect", "4001"])
        by_title = runner.invoke(app, ["news", "--inspect", "portes ouvertes"])
        as_json = runner.invoke(app, ["news", "--inspect", "4001", "--json"])
    assert by_id.exit_code == 0, by_id.output
    assert "• Atelier éthique" in by_id.output
    assert "https://example.org/conf" in by_id.output
    assert by_title.exit_code == 0, by_title.output
    assert news["result"]["content"][0]["title"] in by_title.output
    assert json.loads(as_json.output)["ne_id"] == 4001


def test_news_inspect_unknown(home: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API_URL}/me/news/9").mock(
            return_value=httpx.Response(404, json=fixture("error_404"))
        )
        result = runner.invoke(app, ["news", "--inspect", "9"])
    assert result.exit_code == 2
    assert "introuvable" in result.output


# ---------------------------------------------------------------- notify serve


def test_serve_is_under_notify_and_old_name_still_works() -> None:
    new = runner.invoke(app, ["notify", "serve", "--help"])
    old = runner.invoke(app, ["serve", "--help"])
    assert new.exit_code == 0
    assert old.exit_code == 0
    assert "--listen" in new.output
    assert "--listen" in old.output
    assert not any(
        line.strip("│ ").startswith("serve ")
        for line in runner.invoke(app, ["--help"]).output.splitlines()
    )


def test_notify_list_shows_buttons(home: Path) -> None:
    path = home / "xdg_config_home" / "myges" / "config.json"
    config = json.loads(path.read_text())
    config["serve"] = {"url": "http://100.64.0.1:2587", "token": "t"}
    path.write_text(json.dumps(config))
    result = runner.invoke(app, ["notify", "list"])
    assert result.exit_code == 0, result.output
    assert "http://100.64.0.1:2587" in result.output
    assert "token" not in result.output


def test_serve_bad_address_keeps_previous_config(home: Path) -> None:
    path = home / "xdg_config_home" / "myges" / "config.json"
    config = json.loads(path.read_text())
    config["serve"] = {"url": "http://100.64.0.1:2587", "token": "t"}
    path.write_text(json.dumps(config))
    result = runner.invoke(app, ["notify", "serve", "--listen", "@TAILNET_IP@:2587"])
    assert result.exit_code == 1
    assert "Impossible d'écouter" in result.output
    assert json.loads(path.read_text())["serve"] == {"url": "http://100.64.0.1:2587", "token": "t"}


def test_mail_by_email_address(home: Path, no_browser: list[str]) -> None:
    with respx.mock(assert_all_called=False) as router:
        people_api(router)
        exact = runner.invoke(app, ["teachers", "--mail", "GHopper@example.org, lovelace"])
        unknown = runner.invoke(app, ["teachers", "--mail", "inconnu@example.org"])
    assert exact.exit_code == 0, exact.output
    assert parse_qs(urlsplit(no_browser[0]).query)["to"] == [
        "ghopper@example.org,alovelace@example.org"
    ]
    assert unknown.exit_code == 2
