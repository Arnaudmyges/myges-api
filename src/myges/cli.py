"""Interface en ligne de commande ``myges``.

``myges login`` demande identifiant et mot de passe, met le token en cache et retient
le compte : les commandes suivantes fonctionnent directement, tant que le token est
valide (~6 jours). Pour un usage non interactif, ``MYGES_USERNAME`` / ``MYGES_PASSWORD``
(ou ``MYGES_TOKEN``) dans l'environnement priment sur le compte retenu.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, TypeVar

try:
    import typer
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    sys.exit("La CLI nécessite l'extra « cli » : pip install 'myges-api[cli]'")

from pydantic import BaseModel

from .client import MyGES
from .errors import MyGESError
from .ics import to_ics

app = typer.Typer(
    help="Client en ligne de commande pour l'API myGES / Kordis (non officielle).",
    no_args_is_help=True,
    rich_markup_mode="markdown",
)
console = Console()
err = Console(stderr=True)

T = TypeVar("T")

JsonOpt = Annotated[bool, typer.Option("--json", help="Sortie JSON brute.")]
YearOpt = Annotated[
    int | None, typer.Option("--year", "-y", help="Année scolaire (défaut : la plus récente).")
]


# ---------------------------------------------------------------- Utilitaires


def _config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "myges" / "config.json"


def _saved_username() -> str | None:
    try:
        value = json.loads(_config_path().read_text(encoding="utf-8")).get("username")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def _save_username(username: str | None) -> None:
    path = _config_path()
    if username is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"username": username}), encoding="utf-8")


def _current_username() -> str | None:
    return os.environ.get("MYGES_USERNAME") or _saved_username()


def _not_logged_in() -> typer.Exit:
    err.print("[red]Pas connecté.[/] Lancer d'abord `myges login`.")
    return typer.Exit(2)


@contextmanager
def _client(password: str | None = None) -> Iterator[MyGES]:
    token = os.environ.get("MYGES_TOKEN")
    username = _current_username()
    if not token and not username:
        raise _not_logged_in()
    try:
        with MyGES(
            username,
            password or os.environ.get("MYGES_PASSWORD"),
            token=token,
        ) as client:
            yield client
    except MyGESError as exc:
        err.print(f"[red]Erreur :[/] {exc}")
        raise typer.Exit(1) from exc


def _dump(data: Any) -> None:
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json", by_alias=True)
    elif isinstance(data, list):
        data = [
            d.model_dump(mode="json", by_alias=True) if isinstance(d, BaseModel) else d
            for d in data
        ]
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _fmt_dt(value: datetime | None, fmt: str = "%d/%m/%Y %H:%M") -> str:
    return value.astimezone().strftime(fmt) if value else ""


def _table(
    title: str,
    rows: Sequence[T],
    columns: Sequence[tuple[str, Callable[[T], Any]]],
) -> None:
    if not rows:
        console.print(f"[dim]{title} : aucun élément.[/]")
        return
    table = Table(title=title, title_justify="left", header_style="bold")
    for name, _ in columns:
        table.add_column(name)
    for row in rows:
        table.add_row(*("" if (v := getter(row)) is None else str(v) for _, getter in columns))
    console.print(table)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"date attendue au format AAAA-MM-JJ, reçu {value!r}") from exc


# ---------------------------------------------------------------- Auth


@app.command()
def login(
    username: Annotated[
        str | None, typer.Option("--username", "-u", envvar="MYGES_USERNAME")
    ] = None,
) -> None:
    """Se connecter : le compte est retenu pour les commandes suivantes."""
    username = username or typer.prompt("Identifiant myGES", default=_saved_username())
    password = os.environ.get("MYGES_PASSWORD") or typer.prompt("Mot de passe", hide_input=True)
    try:
        with MyGES(username, password) as client:
            token = client.login()
    except MyGESError as exc:
        err.print(f"[red]Échec :[/] {exc}")
        raise typer.Exit(1) from exc
    _save_username(username)
    console.print(
        f"[green]Connecté en tant que {username}.[/] "
        f"Session valide jusqu'au {_fmt_dt(token.expires_at)}."
    )


@app.command()
def logout() -> None:
    """Se déconnecter : oublie le compte et son token."""
    username = _current_username()
    if username is None:
        raise _not_logged_in()
    with MyGES(username) as client:
        client.logout()
    _save_username(None)
    console.print(f"Déconnecté ({username}).")


@app.command()
def token(json_: JsonOpt = False) -> None:
    """Afficher le token courant (à coller dans **Authorize** de la doc Swagger)."""
    with _client() as client:
        current = client.get_token()
    if json_:
        _dump(current.to_dict())
    else:
        sys.stdout.write(current.access_token + "\n")
        err.print(f"[dim]Expire le {_fmt_dt(current.expires_at)}.[/]")


# ---------------------------------------------------------------- Profil


@app.command()
def profile(json_: JsonOpt = False) -> None:
    """Profil de l'étudiant connecté."""
    with _client() as client:
        data = client.profile()
    if json_:
        return _dump(data)
    console.print(f"[bold]{data.firstname} {data.name}[/]  ({data.student_id})")
    console.print(f"{data.email}")


@app.command()
def years(json_: JsonOpt = False) -> None:
    """Années scolaires disponibles."""
    with _client() as client:
        data = client.years()
    _dump(data) if json_ else console.print(", ".join(map(str, data)))


# ---------------------------------------------------------------- Agenda


@app.command()
def agenda(
    days: Annotated[
        int, typer.Option("--days", "-d", help="Nombre de jours à partir de --from.")
    ] = 7,
    start: Annotated[
        str | None, typer.Option("--from", help="Date de début AAAA-MM-JJ (défaut : aujourd'hui).")
    ] = None,
    end: Annotated[
        str | None, typer.Option("--to", help="Date de fin AAAA-MM-JJ (incluse).")
    ] = None,
    ics: Annotated[
        Path | None, typer.Option("--ics", help="Écrire un fichier iCalendar au lieu d'afficher.")
    ] = None,
    json_: JsonOpt = False,
) -> None:
    """Emploi du temps."""
    first = _parse_date(start) if start else date.today()
    last = _parse_date(end) + timedelta(days=1) if end else first + timedelta(days=days)
    with _client() as client:
        events = sorted(client.agenda(first, last), key=lambda e: e.start_date)
    if ics:
        ics.write_text(to_ics(events), encoding="utf-8")
        console.print(f"{len(events)} créneaux écrits dans {ics}")
        return
    if json_:
        return _dump(events)
    _table(
        f"Agenda du {first:%d/%m} au {last - timedelta(days=1):%d/%m}",
        events,
        [
            ("Jour", lambda e: _fmt_dt(e.start_date, "%a %d/%m")),
            (
                "Horaire",
                lambda e: f"{_fmt_dt(e.start_date, '%H:%M')}–{_fmt_dt(e.end_date, '%H:%M')}",
            ),
            ("Cours", lambda e: e.name),
            ("Salle", lambda e: e.location or e.modality),
            ("Intervenant", lambda e: e.teacher),
        ],
    )


# ---------------------------------------------------------------- Scolarité


@app.command()
def grades(year: YearOpt = None, json_: JsonOpt = False) -> None:
    """Notes par matière."""
    with _client() as client:
        data = client.grades(year)
    if json_:
        return _dump(data)
    _table(
        "Notes",
        data,
        [
            ("Matière", lambda g: g.course),
            ("Notes", lambda g: " ".join(f"{n:g}" for n in g.grades)),
            ("Exam", lambda g: g.exam),
            ("Moyenne", lambda g: g.average),
            ("Coef", lambda g: g.coef),
            ("ECTS", lambda g: g.ects),
            ("Abs.", lambda g: g.absences),
        ],
    )


@app.command()
def absences(year: YearOpt = None, json_: JsonOpt = False) -> None:
    """Absences."""
    with _client() as client:
        data = client.absences(year)
    if json_:
        return _dump(data)
    _table(
        "Absences",
        data,
        [
            ("Date", lambda a: _fmt_dt(a.date)),
            ("Matière", lambda a: a.course_name),
            ("Justifiée", lambda a: "oui" if a.justified else "non"),
        ],
    )


@app.command()
def courses(year: YearOpt = None, json_: JsonOpt = False) -> None:
    """Matières suivies."""
    with _client() as client:
        data = client.courses(year)
    if json_:
        return _dump(data)
    _table(
        "Matières",
        data,
        [
            ("rc_id", lambda c: c.rc_id),
            ("Matière", lambda c: c.name),
            ("Intervenant", lambda c: c.teacher),
            ("Coef", lambda c: c.coef),
            ("ECTS", lambda c: c.ects),
            ("Docs", lambda c: "oui" if c.has_documents else ""),
        ],
    )


def _safe_name(name: str) -> str:
    """Nom utilisable comme fichier ou dossier sur tous les systèmes."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:150] or "_"


@app.command()
def files(
    rc_id: Annotated[
        int | None, typer.Argument(help="Matière (défaut : toutes celles de l'année).")
    ] = None,
    year: YearOpt = None,
    download: Annotated[
        Path | None,
        typer.Option(
            "--download", "-d", help="Télécharger dans ce dossier (un sous-dossier par matière)."
        ),
    ] = None,
    json_: JsonOpt = False,
) -> None:
    """Supports de cours : liste, ou téléchargement avec `--download DOSSIER`.

    Les fichiers déjà présents sont ignorés : relancer la commande ne télécharge que les
    nouveaux supports.
    """
    with _client() as client:
        if rc_id is not None:
            targets = [(rc_id, str(rc_id))]
        else:
            targets = [(c.rc_id, c.name) for c in client.courses(year) if c.has_documents]
        listing = [(name, rc, f) for rc, name in targets for f in client.course_files(rc)]
        if download is None:
            if json_:
                return _dump([f for _, _, f in listing])
            return _table(
                "Supports de cours",
                listing,
                [
                    ("Matière", lambda row: row[0]),
                    ("Fichier", lambda row: row[2].filename),
                    ("Ajouté le", lambda row: _fmt_dt(row[2].update_date, "%d/%m/%Y")),
                    ("rc_id/oc_id", lambda row: f"{row[1]}/{row[2].oc_id}"),
                ],
            )
        fetched = skipped = 0
        for name, rc, item in listing:
            dest = download / _safe_name(name) / _safe_name(item.filename)
            if dest.exists():
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(client.download_course_file(rc, item.oc_id))
            tmp.replace(dest)
            fetched += 1
            console.print(f"[green]↓[/] {dest}")
    console.print(f"{fetched} téléchargé(s), {skipped} déjà présent(s).")


@app.command()
def syllabus(rc_id: int, json_: JsonOpt = False) -> None:
    """Syllabus d'une matière (`rc_id` donné par `myges courses`)."""
    with _client() as client:
        data = client.syllabus(rc_id)
    if data is None:
        err.print("Pas de syllabus pour cette matière.")
        raise typer.Exit(1)
    if json_:
        return _dump(data)
    console.print(f"[bold]{data.syllabus_name}[/]  ({data.formation}, {data.tri_name})")
    console.print(
        f"Intervenant : {data.teacher_firstname} {data.teacher_lastname} · "
        f"coef {data.coef:g} · {data.ects:g} ECTS · évaluation : {data.evaluation_type}"
        if data.coef is not None and data.ects is not None
        else f"Intervenant : {data.teacher_firstname} {data.teacher_lastname}"
    )
    if data.teaching_goals:
        console.print(f"\n[bold]Objectifs[/]\n{data.teaching_goals}")
    _table(
        "Séances",
        sorted(data.seance_details or [], key=lambda x: x.number or 0),
        [
            ("N°", lambda x: x.number),
            ("Contenu", lambda x: x.content),
            ("Évaluation", lambda x: x.eval),
        ],
    )


# ---------------------------------------------------------------- Personnes


@app.command()
def teachers(year: YearOpt = None, json_: JsonOpt = False) -> None:
    """Intervenants de l'année."""
    with _client() as client:
        data = client.teachers(year)
    if json_:
        return _dump(data)
    _table(
        "Intervenants",
        data,
        [("Nom", lambda p: f"{p.firstname} {p.lastname}"), ("Email", lambda p: p.email)],
    )


@app.command()
def classes(year: YearOpt = None, json_: JsonOpt = False) -> None:
    """Classes de l'année (l'identifiant sert à `myges students`)."""
    with _client() as client:
        data = client.classes(year)
    if json_:
        return _dump(data)
    _table(
        "Classes",
        data,
        [
            ("puid", lambda c: c.puid),
            ("Nom", lambda c: c.name),
            ("Promotion", lambda c: c.promotion),
        ],
    )


@app.command()
def students(class_id: int, json_: JsonOpt = False) -> None:
    """Étudiants d'une classe (identifiant donné par `myges classes`)."""
    with _client() as client:
        data = client.class_students(class_id)
    if json_:
        return _dump(data)
    _table(
        "Étudiants",
        data,
        [("Nom", lambda p: f"{p.firstname} {p.lastname}"), ("Email", lambda p: p.email)],
    )


# ---------------------------------------------------------------- Projets


@app.command()
def projects(year: YearOpt = None, json_: JsonOpt = False) -> None:
    """Projets de groupe."""
    with _client() as client:
        data = client.projects(year)
    if json_:
        return _dump(data)
    _table(
        "Projets",
        data,
        [
            ("id", lambda p: p.project_id),
            ("Projet", lambda p: p.name),
            ("Matière", lambda p: p.course_name),
            ("Groupes", lambda p: len(p.groups)),
        ],
    )


@app.command()
def practicals(year: YearOpt = None, json_: JsonOpt = False) -> None:
    """Travaux pratiques (TP)."""
    with _client() as client:
        data = client.practicals(year)
    if json_:
        return _dump(data)
    _table(
        "TP",
        data,
        [
            ("id", lambda p: p.project_id),
            ("TP", lambda p: p.name),
            ("Matière", lambda p: p.course_name),
            (
                "Échéance",
                lambda p: _fmt_dt(
                    min((s.psp_limit_date for s in p.steps or [] if s.psp_limit_date), default=None)
                ),
            ),
        ],
    )


@app.command()
def steps(json_: JsonOpt = False) -> None:
    """Prochaines échéances de projet."""
    with _client() as client:
        data = client.next_project_steps()
    if json_:
        return _dump(data)
    _table(
        "Échéances",
        data,
        [
            ("Date limite", lambda s: _fmt_dt(s.psp_limit_date)),
            ("Projet", lambda s: s.pro_name),
            ("Étape", lambda s: s.psp_type),
        ],
    )


# ---------------------------------------------------------------- Vie de campus


@app.command()
def news(
    page: Annotated[int, typer.Option(help="Page (à partir de 0).")] = 0,
    size: Annotated[int, typer.Option(help="Taille de page.")] = 10,
    json_: JsonOpt = False,
) -> None:
    """Actualités de l'école."""
    with _client() as client:
        data = client.news(page, size)
    if json_:
        return _dump(data)
    _table(
        f"Actualités (page {data.number + 1}/{data.total_pages})",
        data.content,
        [("Date", lambda n: _fmt_dt(n.date, "%d/%m/%Y")), ("Titre", lambda n: n.title)],
    )


@app.command()
def events(json_: JsonOpt = False) -> None:
    """Événements du campus."""
    with _client() as client:
        data = client.events()
    if json_:
        return _dump(data)
    _table(
        "Événements",
        sorted(data, key=lambda e: e.event_date or datetime.min.astimezone()),
        [
            ("Date", lambda e: _fmt_dt(e.event_date)),
            ("Événement", lambda e: e.event_title),
            ("Lieu", lambda e: e.location),
            ("Inscrit", lambda e: "oui" if e.is_participant_subscribed else ""),
        ],
    )


@app.command()
def campus(json_: JsonOpt = False) -> None:
    """Campus du réseau, avec adresse."""
    with _client() as client:
        data = client.campuses()
    if json_:
        return _dump(data)
    _table(
        "Campus",
        data,
        [
            ("Campus", lambda c: c.name),
            ("Adresse", lambda c: f"{c.address}, {c.zipcode} {c.city}"),
        ],
    )


# ---------------------------------------------------------------- Exploration


@app.command()
def raw(
    path: Annotated[str, typer.Argument(help="Chemin, ex. /me/2026/courses")],
    param: Annotated[
        list[str] | None, typer.Option("--param", "-p", help="Paramètre clé=valeur (répétable).")
    ] = None,
) -> None:
    """GET brut sur n'importe quel chemin de l'API, affiche `result` en JSON."""
    params = dict(p.split("=", 1) for p in param or [])
    with _client() as client:
        _dump(client.request("GET", path, params=params or None))


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
