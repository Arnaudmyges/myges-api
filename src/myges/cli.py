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
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, TypeVar
from zoneinfo import ZoneInfo

try:
    import typer
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    sys.exit("La CLI nécessite l'extra « cli » : pip install 'myges-api[cli]'")

from pydantic import BaseModel

from . import notify
from . import watch as watch_mod
from ._version import __version__
from .client import MyGES
from .errors import MyGESError, TokenExpiredError
from .ics import to_ics
from .models import AgendaEvent

app = typer.Typer(
    help="Client en ligne de commande pour l'API myGES / Kordis (non officielle).",
    no_args_is_help=True,
    rich_markup_mode="markdown",
)
console = Console()
err = Console(stderr=True)


def _show_version(value: bool) -> None:
    if value:
        console.print(f"myges-api {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_show_version, is_eager=True, help="Afficher la version."
        ),
    ] = False,
) -> None:
    """Client en ligne de commande pour l'API myGES / Kordis (non officielle)."""


T = TypeVar("T")

# Heure affichée : celle des campus (France), pas celle de la machine, qui peut être un
# serveur en UTC. Surchargeable avec MYGES_TZ (ex. « America/Montreal »).
_TZ = ZoneInfo(os.environ.get("MYGES_TZ") or "Europe/Paris")

JsonOpt = Annotated[bool, typer.Option("--json", help="Sortie JSON brute.")]
YearOpt = Annotated[
    int | None, typer.Option("--year", "-y", help="Année scolaire (défaut : la plus récente).")
]


# ---------------------------------------------------------------- Utilitaires


def _config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "myges" / "config.json"


def _load_config() -> dict[str, Any]:
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_config(data: dict[str, Any]) -> None:
    """Écrit la config en 0600 : elle peut contenir webhooks et mot de passe SMTP."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    path.chmod(0o600)


def _saved_username() -> str | None:
    value = _load_config().get("username")
    return value if isinstance(value, str) and value else None


def _save_username(username: str | None, password: str | None = None) -> None:
    """Retient le compte (et, sur demande explicite, son mot de passe) ; None l'oublie."""
    config = _load_config()
    config.pop("password", None)
    if username is None:
        config.pop("username", None)
    else:
        config["username"] = username
        if password is not None:
            config["password"] = password
    _store_config(config)


def _current_username() -> str | None:
    return os.environ.get("MYGES_USERNAME") or _saved_username()


def _current_password() -> str | None:
    if os.environ.get("MYGES_PASSWORD"):
        return os.environ["MYGES_PASSWORD"]
    saved = _load_config().get("password")
    # Le mot de passe enregistré ne vaut que pour le compte enregistré.
    if isinstance(saved, str) and _current_username() == _saved_username():
        return saved
    return None


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
            password or _current_password(),
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
    return value.astimezone(_TZ).strftime(fmt) if value else ""


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


_JOURS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")


def _fmt_day(value: datetime) -> str:
    """« ven. 16/10 », quelle que soit la locale du système."""
    local = value.astimezone(_TZ)
    return f"{_JOURS[local.weekday()]} {local:%d/%m}"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"date attendue au format AAAA-MM-JJ, reçu {value!r}") from exc


def _fold(text: str) -> str:
    """Minuscules sans accents, pour comparer des noms de matières."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


class _Courses:
    """Matières d'une année numérotées 1..N (tri par nom), pour désigner une matière
    par son numéro ou une partie de son nom plutôt que par son ``rc_id``."""

    def __init__(self, client: MyGES, year: int | None) -> None:
        self.items = sorted(client.courses(year), key=lambda c: _fold(c.name))
        self._numbers = {c.rc_id: n for n, c in enumerate(self.items, 1)}

    def number(self, rc_id: int | None) -> str:
        """Numéro affiché dans la colonne ``#`` (vide si la matière est inconnue)."""
        found = self._numbers.get(rc_id) if rc_id is not None else None
        return str(found) if found else ""

    def resolve(self, ref: str) -> tuple[int, str]:
        """``"3"``, ``"anglais"`` ou un ``rc_id`` → (rc_id, nom). Sort en erreur sinon."""
        ref = ref.strip()
        if ref.isdigit():
            value = int(ref)
            if 1 <= value <= len(self.items):
                chosen = self.items[value - 1]
                return chosen.rc_id, chosen.name
            known = next((c for c in self.items if c.rc_id == value), None)
            # rc_id d'une autre année : accepté tel quel (scripts, anciennes matières).
            return value, known.name if known else str(value)
        words = _fold(ref).split()
        matches = [c for c in self.items if all(w in _fold(c.name) for w in words)]
        if len(matches) == 1:
            return matches[0].rc_id, matches[0].name
        if not matches:
            err.print(f"[red]Aucune matière ne correspond à « {ref} ».[/] Voir `myges courses`.")
        else:
            err.print(f"[red]« {ref} » est ambigu[/], préciser parmi :")
            for c in matches:
                err.print(f"  {self.number(c.rc_id):>3}  {c.name}")
        raise typer.Exit(2)


CourseArg = Annotated[
    str,
    typer.Argument(
        metavar="MATIÈRE",
        help="Numéro (colonne # de `myges courses`), partie du nom (ex. anglais) ou rc_id.",
    ),
]


# ---------------------------------------------------------------- Auth


@app.command()
def login(
    username: Annotated[
        str | None, typer.Option("--username", "-u", envvar="MYGES_USERNAME")
    ] = None,
    save_password: Annotated[
        bool,
        typer.Option(
            "--save-password",
            help="Retenir aussi le mot de passe (fichier 0600) pour renouveler la session "
            "sans intervention, utile pour `myges watch` en tâche planifiée.",
        ),
    ] = False,
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
    _save_username(username, password if save_password else None)
    console.print(
        f"[green]Connecté en tant que {username}.[/] "
        f"Session valide jusqu'au {_fmt_dt(token.expires_at)}."
    )
    if save_password:
        console.print(
            f"[dim]Mot de passe enregistré dans {_config_path()} (lisible par vous seul).[/]"
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
            ics = ics.expanduser()  # PowerShell ne développe pas ~ pour les programmes externes
            ics.write_text(to_ics(events), encoding="utf-8")
            console.print(f"{len(events)} créneaux écrits dans {ics}")
            return
        if json_:
            return _dump(events)
        catalog = _Courses(client, None)
    _agenda_table(f"Agenda du {first:%d/%m} au {last - timedelta(days=1):%d/%m}", events, catalog)


def _rc_id(event: AgendaEvent) -> int | None:
    return event.discipline.rc_id if event.discipline else None


def _agenda_table(title: str, events: Sequence[AgendaEvent], catalog: _Courses) -> None:
    _table(
        title,
        events,
        [
            ("Jour", lambda e: _fmt_day(e.start_date)),
            ("Horaire", _fmt_slot),
            ("#", lambda e: catalog.number(_rc_id(e))),
            ("Cours", lambda e: e.name),
            ("Salle", lambda e: e.location or e.modality),
            ("Intervenant", lambda e: e.teacher),
        ],
    )


def _fmt_slot(event: AgendaEvent) -> str:
    return f"{_fmt_dt(event.start_date, '%H:%M')}–{_fmt_dt(event.end_date, '%H:%M')}"


def _fmt_delay(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes:02d}" if minutes else f"{hours} h"
    return f"{hours // 24} j"


@app.command()
def today(json_: JsonOpt = False) -> None:
    """Cours d'aujourd'hui (et le prochain s'il n'y en a pas)."""
    now = datetime.now(_TZ)
    with _client() as client:
        events = sorted(
            client.agenda(now.date(), now.date() + timedelta(days=1)), key=lambda e: e.start_date
        )
        if json_:
            return _dump(events)
        catalog = _Courses(client, None)
        if not events:
            console.print("[dim]Pas de cours aujourd'hui.[/]")
            upcoming = _next_event(client, now)
            if upcoming is not None:
                _print_next(upcoming, now, catalog)
            return
    _agenda_table(f"Aujourd'hui, {_fmt_day(now)}", events, catalog)


def _next_event(client: MyGES, now: datetime, horizon_days: int = 30) -> AgendaEvent | None:
    """Créneau en cours, sinon le prochain, dans les ``horizon_days`` jours."""
    events = client.agenda(now - timedelta(hours=12), now + timedelta(days=horizon_days))
    candidates = [e for e in events if e.end_date > now]
    return min(candidates, key=lambda e: e.start_date, default=None)


def _print_next(event: AgendaEvent, now: datetime, catalog: _Courses) -> None:
    number = catalog.number(_rc_id(event))
    label = f"{event.name}" + (f" [dim](#{number})[/]" if number else "")
    if event.start_date <= now:
        when = f"[green]En cours[/] jusqu'à {_fmt_dt(event.end_date, '%H:%M')}"
    else:
        when = (
            f"[bold]Dans {_fmt_delay(event.start_date - now)}[/] · "
            f"{_fmt_day(event.start_date)} {_fmt_slot(event)}"
        )
    console.print(f"{when}\n{label}")
    details = " · ".join(x for x in (event.location or event.modality, event.teacher) if x)
    if details:
        console.print(f"[dim]{details}[/]")


@app.command("next")
def next_(json_: JsonOpt = False) -> None:
    """Prochain cours (ou celui en cours)."""
    now = datetime.now(_TZ)
    with _client() as client:
        event = _next_event(client, now)
        if json_:
            return _dump(event) if event else _dump(None)
        if event is None:
            console.print("[dim]Aucun cours dans les 30 prochains jours.[/]")
            return
        catalog = _Courses(client, None)
    _print_next(event, now, catalog)


# ---------------------------------------------------------------- Scolarité


@app.command()
def grades(year: YearOpt = None, json_: JsonOpt = False) -> None:
    """Notes par matière."""
    with _client() as client:
        data = client.grades(year)
        if json_:
            return _dump(data)
        catalog = _Courses(client, year)
    _table(
        "Notes",
        sorted(data, key=lambda g: int(catalog.number(g.rc_id) or 10**6)),
        [
            ("#", lambda g: catalog.number(g.rc_id)),
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
    """Matières suivies. Le numéro # sert à désigner la matière dans les autres commandes."""
    with _client() as client:
        catalog = _Courses(client, year)
    if json_:
        return _dump(catalog.items)
    _table(
        "Matières",
        catalog.items,
        [
            ("#", lambda c: catalog.number(c.rc_id)),
            ("Matière", lambda c: c.name),
            ("Intervenant", lambda c: c.teacher),
            ("Coef", lambda c: c.coef),
            ("ECTS", lambda c: c.ects),
            ("Docs", lambda c: "oui" if c.has_documents else ""),
            ("rc_id", lambda c: c.rc_id),
        ],
    )


def _safe_name(name: str) -> str:
    """Nom utilisable comme fichier ou dossier sur tous les systèmes."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:150] or "_"


@app.command()
def files(
    course: Annotated[
        str | None,
        typer.Argument(
            metavar="[MATIÈRE]",
            help="Numéro, partie du nom ou rc_id (défaut : toutes les matières de l'année).",
        ),
    ] = None,
    year: YearOpt = None,
    download: Annotated[
        Path | None,
        typer.Option(
            "--download",
            "-d",
            metavar="[DOSSIER]",
            help="Télécharger (un sous-dossier par matière). Sans dossier : dossier courant.",
        ),
    ] = None,
    json_: JsonOpt = False,
) -> None:
    """Supports de cours : liste, ou téléchargement avec `--download [DOSSIER]`.

    Les fichiers déjà présents sont ignorés : relancer la commande ne télécharge que les
    nouveaux supports.
    """
    with _client() as client:
        catalog = _Courses(client, year)
        if course is not None:
            targets = [catalog.resolve(course)]
        else:
            targets = [(c.rc_id, c.name) for c in catalog.items if c.has_documents]
        listing = [(name, rc, f) for rc, name in targets for f in client.course_files(rc)]
        if download is None:
            if json_:
                return _dump([f for _, _, f in listing])
            return _table(
                "Supports de cours",
                listing,
                [
                    ("#", lambda row: catalog.number(row[1])),
                    ("Matière", lambda row: row[0]),
                    ("Fichier", lambda row: row[2].filename),
                    ("Ajouté le", lambda row: _fmt_dt(row[2].update_date, "%d/%m/%Y")),
                ],
            )
        fetched = skipped = 0
        for name, rc, item in listing:
            dest = download.expanduser() / _safe_name(name) / _safe_name(item.filename)
            if dest.exists():
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(client.download_course_file(rc, item.oc_id))
            tmp.replace(dest)
            fetched += 1
            console.print(f"[green]↓[/] {dest}")
    where = download.expanduser().resolve()
    console.print(f"{fetched} téléchargé(s), {skipped} déjà présent(s), dans {where}.")


@app.command()
def syllabus(course: CourseArg, year: YearOpt = None, json_: JsonOpt = False) -> None:
    """Syllabus d'une matière : `myges syllabus 3` ou `myges syllabus anglais`."""
    with _client() as client:
        rc_id, _ = _Courses(client, year).resolve(course)
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
        catalog = _Courses(client, year)
    _table(
        "Projets",
        data,
        [
            ("#", lambda p: catalog.number(p.rc_id)),
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
        catalog = _Courses(client, year)
    _table(
        "TP",
        data,
        [
            ("#", lambda p: catalog.number(p.rc_id)),
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
        sorted(data, key=lambda e: e.event_date or datetime.min.replace(tzinfo=_TZ)),
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


# ---------------------------------------------------------------- Alertes


def _state_path(username: str) -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", username)
    return Path(base) / "myges" / f"watch-{safe}.json"


def _channels() -> list[notify.Channel]:
    channels = _load_config().get("notify", [])
    return channels if isinstance(channels, list) else []


def _broadcast(title: str, body: str) -> int:
    """Envoie à tous les canaux ; renvoie le nombre d'échecs (signalés sur stderr)."""
    failures = 0
    for channel in _channels():
        try:
            notify.send(channel, title, body)
        except Exception as exc:  # un canal en panne ne doit pas bloquer les autres
            failures += 1
            err.print(f"[red]Échec d'envoi[/] ({notify.describe(channel)}) : {exc}")
    return failures


def _take_snapshot(client: MyGES) -> watch_mod.Snapshot:
    now = datetime.now(_TZ)
    catalog = _Courses(client, None)
    files = {
        c.rc_id: (c.name, client.course_files(c.rc_id)) for c in catalog.items if c.has_documents
    }
    return watch_mod.snapshot(
        grades=client.grades(),
        absences=client.absences(),
        files=files,
        agenda=client.agenda(now, now + watch_mod.AGENDA_HORIZON),
        now=now,
    )


@app.command()
def watch(
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Ne rien afficher s'il n'y a rien de nouveau.")
    ] = False,
    no_notify: Annotated[
        bool, typer.Option("--no-notify", help="Afficher sans envoyer de notification.")
    ] = False,
) -> None:
    """Signaler les nouveautés depuis le dernier passage : notes, absences, supports, agenda.

    À lancer régulièrement (cron, tâche planifiée). Les nouveautés sont envoyées aux canaux
    configurés avec `myges notify add`. Le premier passage enregistre seulement l'état.
    """
    username = _current_username()
    if username is None:
        raise _not_logged_in()
    path = _state_path(username)
    try:
        previous: dict[str, Any] | None = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None

    try:
        with MyGES(username, _current_password()) as client:
            current = _take_snapshot(client)
    except TokenExpiredError:
        # Une seule alerte par expiration, pas une toutes les 30 minutes.
        if previous is not None and not previous.get("expired_notified"):
            if not no_notify:
                _broadcast(
                    "myGES : session expirée",
                    "ℹ️ La session myGES a expiré : relancer `myges login` "
                    "(ou `myges login --save-password` pour ne plus avoir à le faire).",
                )
            previous["expired_notified"] = True
            _write_state(path, previous)
        err.print("[red]Session expirée.[/] Relancer `myges login`.")
        raise typer.Exit(1) from None
    except MyGESError as exc:
        err.print(f"[red]Erreur :[/] {exc}")
        raise typer.Exit(1) from exc

    _write_state(path, current)
    if previous is None:
        console.print(
            f"État initial enregistré ({len(current['grades'])} matières notées, "
            f"{sum(len(f['items']) for f in current['files'].values())} supports, "
            f"{len(current['agenda'])} cours à venir). "
            "Les prochains passages signaleront les nouveautés."
        )
        if not _channels():
            console.print("[dim]Aucun canal de notification : voir `myges notify add --help`.[/]")
        return

    changes = watch_mod.diff(previous, current, _TZ)
    if not changes:
        if not quiet:
            console.print("[dim]Rien de nouveau.[/]")
        return
    title, body = notify.format_changes(changes)
    console.print(f"[bold]{title}[/]\n{body}")
    if not no_notify and _broadcast(title, body):
        raise typer.Exit(1)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)


notify_app = typer.Typer(
    help="Canaux de notification de `myges watch` (ntfy, Discord, e-mail).",
    no_args_is_help=True,
)
add_app = typer.Typer(help="Ajouter un canal de notification.", no_args_is_help=True)
notify_app.add_typer(add_app, name="add")
app.add_typer(notify_app, name="notify")


def _add_channel(channel: notify.Channel) -> None:
    config = _load_config()
    config["notify"] = [*_channels(), channel]
    _store_config(config)
    console.print(f"[green]Canal ajouté :[/] {notify.describe(channel)}")
    console.print("[dim]Vérifier avec `myges notify test`.[/]")


@add_app.command("ntfy")
def notify_add_ntfy(
    topic: Annotated[
        str | None,
        typer.Argument(help="Sujet ntfy (défaut : un sujet aléatoire, impossible à deviner)."),
    ] = None,
    server: Annotated[str, typer.Option(help="Serveur ntfy.")] = notify.NTFY_SERVER,
    token: Annotated[str | None, typer.Option(help="Jeton d'accès, pour un sujet protégé.")] = None,
) -> None:
    """Notifications push sur téléphone avec l'appli ntfy (Android, iOS, web).

    Sur ntfy.sh, quiconque connaît le sujet peut lire les messages : garder le sujet
    aléatoire proposé par défaut, et s'y abonner dans l'appli ntfy.
    """
    channel: notify.Channel = {
        "type": "ntfy",
        "server": server,
        "topic": topic or notify.random_topic(),
    }
    if token:
        channel["token"] = token
    _add_channel(channel)
    console.print(
        f"Dans l'appli ntfy, s'abonner au sujet [bold]{channel['topic']}[/]"
        + (f" sur {server}." if server != notify.NTFY_SERVER else ".")
    )


@add_app.command("discord")
def notify_add_discord(
    webhook: Annotated[
        str, typer.Argument(help="URL du webhook (salon > Paramètres > Intégrations > Webhooks).")
    ],
) -> None:
    """Messages dans un salon Discord, via un webhook.

    Les notes et absences sont des données personnelles : préférer un serveur ou un salon
    privé.
    """
    if not webhook.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/")):
        err.print("[yellow]Cette URL ne ressemble pas à un webhook Discord.[/]")
    _add_channel({"type": "discord", "webhook": webhook})


@add_app.command("email")
def notify_add_email(
    to: Annotated[str, typer.Argument(help="Destinataire.")],
    smtp: Annotated[
        str, typer.Option(help="Serveur SMTP hôte[:port] (587 STARTTLS par défaut, 465 SSL).")
    ],
    user: Annotated[str | None, typer.Option(help="Identifiant SMTP (souvent l'adresse).")] = None,
    sender: Annotated[
        str | None, typer.Option("--from", help="Expéditeur (défaut : --user).")
    ] = None,
) -> None:
    """E-mail via un serveur SMTP. Le mot de passe est demandé puis stocké en 0600.

    Gmail : activer la validation en deux étapes et utiliser un « mot de passe
    d'application » (`--smtp smtp.gmail.com --user vous@gmail.com`).
    """
    host, _, port = smtp.partition(":")
    channel: notify.Channel = {"type": "email", "to": to, "host": host, "port": int(port or 587)}
    if user:
        channel["user"] = user
        channel["password"] = typer.prompt("Mot de passe SMTP", hide_input=True)
    if sender:
        channel["sender"] = sender
    _add_channel(channel)


@notify_app.command("list")
def notify_list() -> None:
    """Canaux configurés."""
    channels = _channels()
    if not channels:
        console.print("[dim]Aucun canal. Voir `myges notify add --help`.[/]")
    for number, channel in enumerate(channels, 1):
        console.print(f"{number}. {notify.describe(channel)}")


@notify_app.command("remove")
def notify_remove(
    number: Annotated[int, typer.Argument(help="Numéro donné par `myges notify list`.")],
) -> None:
    """Supprimer un canal."""
    channels = _channels()
    if not 1 <= number <= len(channels):
        err.print("[red]Numéro inconnu.[/] Voir `myges notify list`.")
        raise typer.Exit(2)
    removed = channels.pop(number - 1)
    config = _load_config()
    config["notify"] = channels
    _store_config(config)
    console.print(f"Canal supprimé : {notify.describe(removed)}")


@notify_app.command("test")
def notify_test() -> None:
    """Envoyer un message de test à tous les canaux."""
    if not _channels():
        err.print("[red]Aucun canal configuré.[/] Voir `myges notify add --help`.")
        raise typer.Exit(2)
    failures = _broadcast("myGES : test", "✅ Les notifications de myges watch arrivent bien ici.")
    if failures:
        raise typer.Exit(1)
    console.print(f"[green]Message de test envoyé à {len(_channels())} canal(aux).[/]")


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


def _default_download_dir(args: list[str]) -> list[str]:
    """``myges files --download`` sans valeur → dossier courant.

    Typer ne gère pas les options à valeur facultative : on complète la ligne de
    commande avant de la lui passer. Limité à ``files`` (``-d`` vaut ``--days`` ailleurs).
    """
    if not args or args[0] != "files":
        return args
    out: list[str] = []
    for i, arg in enumerate(args):
        out.append(arg)
        if arg in ("--download", "-d"):
            following = args[i + 1] if i + 1 < len(args) else None
            if following is None or following.startswith("-"):
                out.append(".")
    return out


def main() -> None:
    app(args=_default_download_dir(sys.argv[1:]), prog_name="myges")


if __name__ == "__main__":  # pragma: no cover
    main()
