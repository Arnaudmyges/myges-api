"""Interface en ligne de commande ``myges``.

``myges login`` demande identifiant et mot de passe, met le token en cache et retient
le compte : les commandes suivantes fonctionnent directement, tant que le token est
valide (~6 jours). Pour un usage non interactif, ``MYGES_USERNAME`` / ``MYGES_PASSWORD``
(ou ``MYGES_TOKEN``) dans l'environnement priment sur le compte retenu.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, TypeVar
from zoneinfo import ZoneInfo

try:
    import typer
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    sys.exit("La CLI nécessite l'extra « cli » : pip install 'myges-api[cli]'")

import httpx
from pydantic import BaseModel

from . import notify, schedule
from . import pending as pending_mod
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
    _hint_new_version()


def _hint_new_version() -> None:
    """Rappel discret quand une version plus récente est publiée (pipx ne prévient pas)."""
    if (
        os.environ.get("MYGES_NO_UPDATE_CHECK")
        or os.environ.get("MYGES_BACKGROUND")
        or os.environ.get("_MYGES_COMPLETE")  # complétion Tab en cours
        or "--json" in sys.argv
        or not sys.stderr.isatty()  # tâche planifiée, script, sortie redirigée
    ):
        return
    from .updates import newer_version

    latest = newer_version(__version__)
    if latest:
        err.print(
            f"[dim]Nouvelle version {latest} disponible (vous avez {__version__}) : "
            "pipx upgrade myges-api[/]",
            highlight=False,
        )


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
def _client(password: str | None = None, *, allow_writes: bool = False) -> Iterator[MyGES]:
    token = os.environ.get("MYGES_TOKEN")
    username = _current_username()
    if not token and not username:
        raise _not_logged_in()
    try:
        with MyGES(
            username,
            password or _current_password(),
            token=token,
            allow_writes=allow_writes,
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
    no_completion: Annotated[
        bool,
        typer.Option("--no-completion", help="Ne pas activer la complétion des commandes (Tab)."),
    ] = False,
) -> None:
    """Se connecter : le compte est retenu pour les commandes suivantes.

    À la première connexion, active aussi la complétion des commandes avec Tab.
    """
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
    if not no_completion and not _load_config().get("completion") and _install_completion():
        config = _load_config()
        config["completion"] = True
        _store_config(config)
        console.print("[dim]Complétion avec Tab activée (dans les nouveaux terminaux).[/]")


def _install_completion() -> bool:
    """Active la complétion Tab du shell courant ; sans bruit si c'est impossible."""
    import subprocess

    try:
        result = subprocess.run(
            [sys.executable, "-m", "myges.cli", "--install-completion"],
            capture_output=True, text=True, timeout=20, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


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


events_app = typer.Typer(help="Événements du campus et inscriptions.")
app.add_typer(events_app, name="events")


@events_app.callback(invoke_without_command=True)
def events(
    ctx: typer.Context,
    inspect: Annotated[
        str | None,
        typer.Option(
            "--inspect",
            "-i",
            metavar="ÉVÉNEMENT",
            help="Détail d'un événement : description, intervenants, salle.",
        ),
    ] = None,
    subscribed: Annotated[
        bool, typer.Option("--subscribed", help="Seulement ceux où vous êtes inscrit.")
    ] = False,
    not_subscribed: Annotated[
        bool,
        typer.Option(
            "--not-subscribed", help="Seulement ceux où vous n'êtes pas (encore) inscrit."
        ),
    ] = False,
    json_: JsonOpt = False,
) -> None:
    """Événements à venir, avec l'ouverture des inscriptions et vos pré-inscriptions."""
    if ctx.invoked_subcommand is not None:
        return
    if subscribed and not_subscribed:
        err.print("[red]--subscribed et --not-subscribed s'excluent.[/]")
        raise typer.Exit(2)
    with _client() as client:
        data = client.events()
    if inspect is not None:
        return _inspect_event(data, _event_id(inspect), json_)
    if json_ and not (subscribed or not_subscribed):
        return _dump(data)  # sans filtre : tous les événements, comme avant
    now = datetime.now(_TZ)
    upcoming = [e for e in data if e.event_date is None or e.event_date >= now]
    if subscribed or not_subscribed:
        upcoming = [e for e in upcoming if bool(e.is_participant_subscribed) == subscribed]
    if json_:
        return _dump(upcoming)
    waiting = {p.event_id: p for p in pending_mod.load()}
    title = (
        "Événements où vous êtes inscrit" if subscribed
        else "Événements où vous n'êtes pas inscrit" if not_subscribed
        else "Événements à venir"
    )  # fmt: skip
    _table(
        title,
        sorted(upcoming, key=lambda e: e.event_date or now),
        [
            ("id", lambda e: e.event_id),
            ("Date", lambda e: _fmt_dt(e.event_date)),
            ("Événement", lambda e: e.event_title),
            ("Inscriptions", lambda e: _subscription_window(e, now)),
            ("Inscrit", lambda e: _subscription_state(e, waiting.get(e.event_id))),
        ],
    )


def _html_to_text(fragment: str) -> str:
    """Description myGES (HTML léger) → texte : un bloc par ligne, entités décodées."""
    import html

    # Un saut de ligne à l'ouverture d'un bloc (pas à la fermeture) : « </div><div> » ne
    # double pas l'interligne, et un bloc vide « <div><br></div> » reste une ligne vide.
    text = re.sub(r"(?i)<br\s*/?>|<(div|p|li)[^>]*>", "\n", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
    lines = [line.strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _inspect_event(data: list[Any], event_id: int, json_: bool) -> None:
    event = next((e for e in data if e.event_id == event_id), None)
    if event is None:
        err.print(f"[red]Événement {event_id} introuvable.[/] Voir `myges events`.")
        raise typer.Exit(2)
    if json_:
        return _dump(event)
    now = datetime.now(_TZ)
    waiting = {p.event_id: p for p in pending_mod.load()}.get(event_id)
    console.print(f"[bold]{event.event_title}[/]")
    console.print(
        f"{_fmt_dt(event.event_date)} · {event.location or '—'} · organisé par "
        f"{event.organizer or '—'}"
    )
    state = _subscription_state(event, waiting) or "non inscrit"
    console.print(f"Inscriptions : {_subscription_window(event, now)} · {state}")
    if event.description:
        console.print()
        console.print(_html_to_text(event.description))
    console.print(f"\n[dim]https://myges.fr/common/event/{event_id}[/]")


def _subscription_state(event: Any, waiting: pending_mod.Pending | None) -> str:
    if event.is_participant_subscribed:
        return "[green]oui[/]"
    if waiting is None:
        return ""
    if pending_mod.alive(waiting.pid) is False:
        return "[yellow]⚠ interrompu[/]"
    return "[cyan]⏳ pré-inscrit[/]"


def _subscription_window(event: Any, now: datetime) -> str:
    opens, closes = event.start_subscription_date, event.end_subscription_date
    if opens and opens > now:
        return f"ouvrent {_fmt_dt(opens)}"
    if closes and closes > now:
        return f"ouvertes → {_fmt_dt(closes, '%d/%m')}"
    return "[dim]closes[/]"


@events_app.command("subscribe")
def events_subscribe(
    events_ref: Annotated[
        list[str],
        typer.Argument(
            metavar="ÉVÉNEMENT...",
            help="Identifiant (colonne id) ou adresse myges.fr/common/event/4525.",
        ),
    ],
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Attendre l'ouverture dans ce terminal, sans rendre la main."),
    ] = False,
    background: Annotated[
        bool, typer.Option("--background", "-b", hidden=True)  # ancien comportement explicite
    ] = False,
) -> None:
    """S'inscrire à l'ouverture des inscriptions (ou tout de suite si elles sont ouvertes).

    Si l'ouverture est à venir, l'attente se fait en arrière-plan et la commande rend la
    main : suivre avec `myges events pending`, annuler avec `myges events cancel`. Chaque
    événement est tenté à son heure d'ouverture (horloge du serveur), toutes les
    demi-secondes pendant 3 minutes. Mise en veille sans effet ; un arrêt de la machine
    interrompt l'attente.
    """
    from .registration import subscribe_at_opening

    event_ids = [_event_id(ref) for ref in events_ref]
    if not wait and not os.environ.get("MYGES_BACKGROUND"):
        with _client() as client:
            now = datetime.now(_TZ)
            opens_later = [
                e.event_id
                for e in client.events()
                if e.event_id in event_ids
                and not e.is_participant_subscribed
                and e.start_subscription_date is not None
                and e.start_subscription_date > now
            ]
        if opens_later or background:
            return _subscribe_in_background(event_ids)

    pid = os.getpid()
    with _client(allow_writes=True) as client:
        now = datetime.now(_TZ)
        known = {e.event_id: e for e in client.events()}
        titles = {i: e.event_title for i, e in known.items()}
        waiting = [
            pending_mod.Pending(i, opens.isoformat(), pid, now.isoformat())
            for i in event_ids
            if (e := known.get(i)) is not None
            and not e.is_participant_subscribed
            and (opens := e.start_subscription_date) is not None
            and opens > now
        ]
        pending_mod.register(waiting)
        if waiting and not os.environ.get("MYGES_BACKGROUND"):
            console.print(
                "[dim]Attente dans ce terminal : le laisser ouvert jusqu'à l'ouverture.[/]"
            )
        try:
            outcome = subscribe_at_opening(
                client,
                event_ids,
                log=console.print,
                tz=_TZ,
                wanted=lambda ids: (
                    (ids - {w.event_id for w in waiting}) | pending_mod.still_wanted(ids, pid)
                ),
            )
        finally:
            pending_mod.remove({w.event_id for w in waiting}, pid=pid)
    for event_id in outcome.subscribed:
        console.print(f"[green]Inscrit[/] : {titles.get(event_id, event_id)}")
    for event_id in outcome.already:
        console.print(f"[dim]Déjà inscrit : {titles.get(event_id, event_id)}[/]")
    for event_id in outcome.cancelled:
        console.print(f"[dim]Annulé : {titles.get(event_id, event_id)}[/]")
    for event_id, reason in outcome.failed.items():
        err.print(f"[red]Échec[/] {event_id} ({titles.get(event_id, '?')}) : {reason}")
    _log_attempts(outcome)
    if outcome.subscribed or outcome.failed:
        lines = [f"✅ Inscrit : {titles.get(i, i)}" for i in outcome.subscribed]
        lines += [f"❌ {titles.get(i, i)} : {r}" for i, r in outcome.failed.items()]
        _broadcast("myGES : inscriptions", "\n".join(lines))
    if outcome.failed:
        raise typer.Exit(1)


def _subscribe_in_background(event_ids: list[int]) -> None:
    """Relance la commande, détachée du terminal, sortie dans le journal d'état."""
    import subprocess
    import time

    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    log_path = Path(base) / "myges" / "events-background.log"
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # --wait : le processus détaché attend lui-même au lieu de se relancer à son tour.
    command = [sys.executable, "-m", "myges.cli", "events", "subscribe", *map(str, event_ids),
               "--wait"]  # fmt: skip
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True  # survit à la fermeture du terminal
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"--- {datetime.now(_TZ).isoformat()} : {' '.join(map(str, event_ids))}\n")
        log.flush()
        child = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            env={**os.environ, "COLUMNS": "120", "MYGES_BACKGROUND": "1"}, **options,
        )  # fmt: skip
    # Attendre que le processus se soit déclaré (ou ait déjà fini : événement ouvert).
    for _ in range(40):
        if child.poll() is not None or any(p.pid == child.pid for p in pending_mod.load()):
            break
        time.sleep(0.25)
    if child.poll() not in (None, 0):
        err.print(f"[red]Échec[/] : voir {log_path}")
        raise typer.Exit(1)
    console.print(
        f"[green]Pré-inscription enregistrée[/] pour {', '.join(map(str, event_ids))} "
        f"(arrière-plan, pid {child.pid})."
    )
    console.print(
        "[dim]Suivre : `myges events pending` · annuler : `myges events cancel <id>` · "
        f"journal : {log_path}[/]"
    )


@events_app.command("pending")
def events_pending() -> None:
    """Pré-inscriptions en attente de l'ouverture des inscriptions."""
    entries = pending_mod.load()
    if not entries:
        console.print("[dim]Aucune pré-inscription. `myges events subscribe <id>`.[/]")
        return
    with _client() as client:
        titles = {e.event_id: e.event_title for e in client.events()}
    _table(
        "Pré-inscriptions",
        sorted(entries, key=lambda p: p.opens),
        [
            ("id", lambda p: p.event_id),
            ("Événement", lambda p: titles.get(p.event_id, "?")),
            ("Ouverture", lambda p: _fmt_dt(p.opens_at)),
            (
                "État",
                lambda p: (
                    "[yellow]⚠ interrompu : relancer subscribe[/]"
                    if pending_mod.alive(p.pid) is False
                    else "[cyan]⏳ en attente[/]"
                ),
            ),
        ],
    )


@events_app.command("cancel")
def events_cancel(
    events_ref: Annotated[
        list[str], typer.Argument(metavar="ÉVÉNEMENT...", help="Identifiant ou adresse.")
    ],
) -> None:
    """Annuler une pré-inscription (avant l'ouverture). Après : `unsubscribe`."""
    import signal

    ids = {_event_id(ref) for ref in events_ref}
    before = pending_mod.load()
    removed = pending_mod.remove(ids)
    for event_id in sorted(ids):
        if event_id in removed:
            console.print(f"Pré-inscription annulée : {event_id}.")
        else:
            err.print(f"[yellow]{event_id} : aucune pré-inscription.[/]")
    # Un processus d'attente qui n'a plus rien à faire est arrêté tout de suite.
    remaining = {p.pid for p in pending_mod.load()}
    for pid in {p.pid for p in before if p.event_id in removed} - remaining:
        if pid != os.getpid() and pending_mod.alive(pid) and sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)


def _event_id(ref: str) -> int:
    """« 4525 » ou « https://myges.fr/common/event/4525 » → 4525."""
    match = re.search(r"(\d+)/?$", ref.strip())
    if match is None:
        err.print(f"[red]Événement invalide : « {ref} ».[/] Donner l'id, ex. 4525.")
        raise typer.Exit(2)
    return int(match.group(1))


def _log_attempts(outcome: Any) -> None:
    """Garde la trace brute des tentatives (diagnostic si l'école change ses règles)."""
    if not outcome.attempts:
        return
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    path = Path(base) / "myges" / "events-subscribe.log"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as fh:
        for a in outcome.attempts:
            fh.write(f"{a.at.isoformat()} event={a.event_id} HTTP {a.status} {a.body}\n")


@events_app.command("unsubscribe")
def events_unsubscribe(
    events_ref: Annotated[
        list[str], typer.Argument(metavar="ÉVÉNEMENT...", help="Identifiant ou adresse.")
    ],
) -> None:
    """Se désinscrire (possible tant que les inscriptions sont ouvertes)."""
    with _client(allow_writes=True) as client:
        for event_id in (_event_id(ref) for ref in events_ref):
            response = client._send("DELETE", f"/me/events/{event_id}/unsubscribe")
            if response.is_success:
                console.print(f"Désinscrit de {event_id}.")
            else:
                err.print(
                    f"[red]Échec[/] {event_id} : HTTP {response.status_code} {response.text[:200]}"
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


# ---------------------------------------------------------------- Blocs

blocs_app = typer.Typer(
    help="Blocs de compétences : moyennes par bloc et simulation de notes.",
)
app.add_typer(blocs_app, name="blocs")


def _year_of(client: MyGES, year: int | None) -> int:
    return year if year is not None else max(client.years())


def _stored_blocs(year: int) -> dict[str, list[int]]:
    blocs = (_load_config().get("blocs") or {}).get(str(year)) or {}
    return {str(k): [int(x) for x in v] for k, v in blocs.items()}


def _save_blocs(year: int, blocs: dict[str, list[int]]) -> None:
    config = _load_config()
    every = config.get("blocs") or {}
    if blocs:
        every[str(year)] = blocs
    else:
        every.pop(str(year), None)
    config["blocs"] = every
    _store_config(config)


def _parse_simulation(raw: str, catalog: _Courses) -> tuple[int, float]:
    ref, sep, value = raw.rpartition("=")
    try:
        grade = float(value.replace(",", "."))
    except ValueError:
        grade = -1.0
    if not sep or not ref or not 0 <= grade <= 20:
        err.print(f"[red]Simulation invalide : « {raw} ».[/] Format : MATIÈRE=NOTE, ex. 3=14.5")
        raise typer.Exit(2)
    rc_id, _ = catalog.resolve(ref)
    return rc_id, grade


def _fmt_avg(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


@blocs_app.callback(invoke_without_command=True)
def blocs_show(
    ctx: typer.Context,
    simulate: Annotated[
        list[str] | None,
        typer.Option(
            "--simulate",
            "-s",
            help="Note hypothétique MATIÈRE=NOTE (répétable), ex. -s 3=15 -s anglais=12.",
        ),
    ] = None,
    year: YearOpt = None,
) -> None:
    """Moyenne de chaque bloc et moyenne générale, avec simulation possible.

    Définir les blocs avec `myges blocs add`. Pondération : coefficient de la matière,
    ou 1 s'il n'est pas encore publié. Bloc validé si sa moyenne est ≥ 10.
    """
    if ctx.invoked_subcommand is not None:
        return
    from .blocs import compute, overall

    with _client() as client:
        y = _year_of(client, year)
        catalog = _Courses(client, y)
        simulated = dict(_parse_simulation(raw, catalog) for raw in simulate or [])
        grades = client.grades(y)
    blocs = _stored_blocs(y)
    results, others = compute(catalog.items, grades, blocs, simulated)
    if not blocs:
        console.print(
            "[dim]Aucun bloc défini pour cette année : toutes les matières sont « hors bloc ». "
            'Exemple : myges blocs add "Bloc 1" 1 3 anglais[/]'
        )
    marks = {"simulée": " *", "provisoire": " ~", "officielle": "", "aucune": ""}
    for result in [*results, *([others] if others.lines else [])]:
        verdict = {True: "[green]validé[/]", False: "[red]non validé[/]", None: "[dim]—[/]"}
        title = f"{result.name} : moyenne {_fmt_avg(result.average)}"
        if result is not others:
            title += f"  {verdict[result.validated]}"
        _table(
            title,
            result.lines,
            [
                ("#", lambda line: catalog.number(line.rc_id)),
                ("Matière", lambda line: line.name),
                (
                    "Coef",
                    lambda line: f"{line.weight:g}" + ("" if line.official_weight else " ¹"),
                ),
                ("Moyenne", lambda line: _fmt_avg(line.average) + marks[line.source]),
            ],
        )
    console.print(f"[bold]Moyenne générale : {_fmt_avg(overall(results, others))}[/]")
    console.print(
        "[dim]* simulée · ~ provisoire (contrôle continu, pas de moyenne officielle) · "
        "¹ coefficient non publié, compté 1[/]"
    )


@blocs_app.command("setup")
def blocs_setup(year: YearOpt = None) -> None:
    """Ranger chaque matière dans son bloc, une par une (à faire une fois par année).

    Pour chaque matière : taper le bloc (`1` donne « Bloc 1 », ou un nom libre), Entrée pour
    garder la valeur actuelle, `-` pour la laisser hors bloc.
    """
    with _client() as client:
        y = _year_of(client, year)
        catalog = _Courses(client, y)
    current = {rc: name for name, members in _stored_blocs(y).items() for rc in members}
    console.print(
        f"[bold]{len(catalog.items)} matières[/] ({y}-{y + 1}). Bloc : `1` → « Bloc 1 », "
        "Entrée = inchangé, `-` = hors bloc.\n"
    )
    chosen: dict[int, str | None] = {}
    for course in catalog.items:
        default = current.get(course.rc_id, "-")
        answer = typer.prompt(
            f"{catalog.number(course.rc_id):>3}  {course.name}", default=default
        ).strip()
        if answer in ("", "-"):
            chosen[course.rc_id] = None
        else:
            chosen[course.rc_id] = f"Bloc {answer}" if answer.isdigit() else answer
    blocs: dict[str, list[int]] = {}
    for course in catalog.items:  # ordre des matières conservé dans chaque bloc
        name = chosen[course.rc_id]
        if name is not None:
            blocs.setdefault(name, []).append(course.rc_id)
    _save_blocs(y, dict(sorted(blocs.items())))
    summary = ", ".join(f"{name} ({len(members)})" for name, members in sorted(blocs.items()))
    console.print(f"\n[green]Blocs enregistrés[/] : {summary or 'aucun'}. Voir `myges blocs`.")


@blocs_app.command("add")
def blocs_add(
    name: Annotated[str, typer.Argument(help='Nom du bloc, ex. "Bloc 1".')],
    courses: Annotated[
        list[str], typer.Argument(help="Matières : numéro, partie du nom ou rc_id.")
    ],
    year: YearOpt = None,
) -> None:
    """Ajouter des matières à un bloc (créé s'il n'existe pas)."""
    with _client() as client:
        y = _year_of(client, year)
        catalog = _Courses(client, y)
        resolved = [catalog.resolve(ref) for ref in courses]
    blocs = _stored_blocs(y)
    for rc_id, _ in resolved:  # une matière n'appartient qu'à un bloc
        for members in blocs.values():
            if rc_id in members:
                members.remove(rc_id)
    blocs.setdefault(name, [])
    blocs[name] += [rc_id for rc_id, _ in resolved if rc_id not in blocs[name]]
    _save_blocs(y, {k: v for k, v in blocs.items() if v})
    for _, course_name in resolved:
        console.print(f"[green]+[/] {course_name} → {name}")


@blocs_app.command("remove")
def blocs_remove(
    name: Annotated[str, typer.Argument(help="Nom du bloc.")],
    courses: Annotated[
        list[str] | None,
        typer.Argument(help="Matières à retirer (défaut : supprimer tout le bloc)."),
    ] = None,
    year: YearOpt = None,
) -> None:
    """Retirer des matières d'un bloc, ou supprimer le bloc."""
    with _client() as client:
        y = _year_of(client, year)
        catalog = _Courses(client, y)
        resolved = [catalog.resolve(ref) for ref in courses or []]
    blocs = _stored_blocs(y)
    if name not in blocs:
        err.print(f"[red]Bloc inconnu : « {name} ».[/] Blocs : {', '.join(blocs) or 'aucun'}.")
        raise typer.Exit(2)
    if resolved:
        blocs[name] = [rc for rc in blocs[name] if rc not in {r for r, _ in resolved}]
    else:
        blocs[name] = []
    _save_blocs(y, {k: v for k, v in blocs.items() if v})
    console.print(f"Bloc « {name} » mis à jour." if blocs[name] else f"Bloc « {name} » supprimé.")


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


# Rythme des vérifications. Les supports de cours sont prioritaires (vérifiés à chaque
# passage) ; notes, absences et agenda changent moins souvent ou sont moins urgents.
SLOW_EVERY = timedelta(minutes=30)
YEAR_TTL = timedelta(days=1)
OFFLINE_ALERT_AFTER = timedelta(hours=2)
OUTBOX_LIMIT = 100
LOG_LINES = 500


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    if "snapshot" not in state and "grades" in state:  # format v1 : l'instantané seul
        state = {"snapshot": {k: v for k, v in state.items() if k != "expired_notified"},
                 "expired_notified": state.get("expired_notified", False)}  # fmt: skip
    return state


def _fetch(client: MyGES, state: dict[str, Any], now: datetime, full: bool) -> dict[str, Any]:
    """Sections relues à ce passage (les autres sont reprises de l'instantané précédent)."""
    checked = state.setdefault("checked", {})
    year_at, year = checked.get("year", [0, None])
    if year is None or now - datetime.fromtimestamp(year_at / 1000, timezone.utc) > YEAR_TTL:
        year = max(client.years())
        checked["year"] = [_ms(now), year]
    courses = [c for c in client.courses(year) if c.has_documents]
    sections: dict[str, Any] = {
        "files": watch_mod.files_section(
            {c.rc_id: (c.name, client.course_files(c.rc_id)) for c in courses}
        )
    }
    if full:
        sections["grades"] = watch_mod.grades_section(client.grades(year))
        sections["absences"] = watch_mod.absences_section(client.absences(year))
        sections["agenda"] = watch_mod.agenda_section(
            client.agenda(now, now + watch_mod.AGENDA_HORIZON), now
        )
        sections["events"] = watch_mod.events_section(client.events(), now)
        checked["slow"] = _ms(now)
    return sections


HISTORY_DAYS = 14


def _remember_history(
    state: dict[str, Any], changes: list[watch_mod.Change], now: datetime
) -> None:
    """Garde 14 jours de nouveautés : c'est la matière du récapitulatif."""
    limit = _ms(now - timedelta(days=HISTORY_DAYS))
    history = [h for h in state.get("history", []) if h["at"] >= limit]
    history += [{"at": _ms(now), "kind": c.kind, "text": c.text} for c in changes]
    state["history"] = history[-500:]


def _remind_before() -> timedelta | None:
    minutes = (_load_config().get("remind") or {}).get("before_minutes")
    return timedelta(minutes=int(minutes)) if minutes else None


def _event_actions(changes: list[watch_mod.Change]) -> list[notify.Action]:
    """Boutons ntfy pour les nouveaux événements : s'inscrire (si `myges serve` tourne)
    ou voir la page myGES."""
    serve = _load_config().get("serve") or {}
    events = [c.event_id for c in changes if c.kind == "event_new" and c.event_id][:3]
    actions: list[notify.Action] = []
    for event_id in events:
        label = "S'inscrire" if len(events) == 1 else f"S'inscrire ({event_id})"
        if serve.get("url") and serve.get("token"):
            actions.append({
                "type": "http", "label": label,
                "url": f"{serve['url'].rstrip('/')}/events/{event_id}/subscribe",
                "headers": {"Authorization": f"Bearer {serve['token']}"},
            })  # fmt: skip
        else:
            actions.append({"type": "view", "label": f"Voir ({event_id})",
                            "url": f"https://myges.fr/common/event/{event_id}"})  # fmt: skip
    if len(events) == 1 and serve.get("url"):
        actions.append({"type": "view", "label": "Voir sur myGES",
                        "url": f"https://myges.fr/common/event/{events[0]}"})  # fmt: skip
    return actions


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[bool]:
    """Verrou entre passages : un passage lent ne doit pas en chevaucher un autre."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        import fcntl
    except ImportError:  # Windows : la tâche planifiée ne se chevauche pas d'elle-même
        yield True
        return
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _deliver(state: dict[str, Any], changes: list[watch_mod.Change]) -> int:
    """Envoie à chaque canal ses nouveautés + celles restées en souffrance ; renvoie le
    nombre de canaux en échec (leurs nouveautés sont gardées pour le passage suivant)."""
    outbox: dict[str, list[dict[str, Any]]] = state.setdefault("outbox", {})
    fresh = [{"kind": c.kind, "text": c.text, "event_id": c.event_id} for c in changes]
    failures = 0
    for channel in _channels():
        key = notify.describe(channel)
        queued = (outbox.get(key, []) + fresh)[-OUTBOX_LIMIT:]
        if not queued:
            continue
        pending = [watch_mod.Change(c["kind"], c["text"], c.get("event_id")) for c in queued]
        title, body = notify.format_changes(pending)
        try:
            notify.send(channel, title, body, actions=_event_actions(pending))
        except Exception as exc:  # un canal en panne ne doit pas bloquer les autres
            failures += 1
            outbox[key] = queued
            err.print(f"[red]Échec d'envoi[/] ({key}) : {exc} — nouvel essai au prochain passage")
        else:
            outbox.pop(key, None)
    known = {notify.describe(c) for c in _channels()}
    for key in [k for k in outbox if k not in known]:  # canal supprimé entre-temps
        del outbox[key]
    return failures


def _log(username: str, line: str) -> None:
    path = _state_path(username).with_name("watch.log")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-(LOG_LINES - 1) :]
    except OSError:
        lines = []
    lines.append(f"{datetime.now(_TZ):%Y-%m-%d %H:%M:%S} {line}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resume_interrupted_registrations(now: datetime) -> list[int]:
    """Relance les pré-inscriptions dont l'attente a été tuée (machine éteinte…)."""
    lost = [
        p.event_id
        for p in pending_mod.load()
        if pending_mod.alive(p.pid) is False and p.opens_at > now - timedelta(minutes=3)
    ]
    if lost:
        _subscribe_in_background(lost)
    return lost


def _offline(state: dict[str, Any], now: datetime, reason: str, notify_on: bool) -> None:
    failures = state.setdefault("failures", {})
    failures.setdefault("since", _ms(now))
    failures["count"] = failures.get("count", 0) + 1
    since = datetime.fromtimestamp(failures["since"] / 1000, timezone.utc)
    if now - since >= OFFLINE_ALERT_AFTER and not failures.get("alerted") and notify_on:
        _broadcast(
            "myGES : alertes interrompues",
            f"⚠️ myGES injoignable depuis {_fmt_dt(since, '%H:%M')} ({reason}). "
            "Les alertes reprendront toutes seules.",
        )
        failures["alerted"] = True


@app.command()
def watch(
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Ne rien afficher s'il n'y a rien de nouveau.")
    ] = False,
    no_notify: Annotated[
        bool, typer.Option("--no-notify", help="Afficher sans envoyer de notification.")
    ] = False,
    install: Annotated[
        bool,
        typer.Option("--install", help="Lancer automatiquement toutes les --every minutes."),
    ] = False,
    uninstall: Annotated[
        bool, typer.Option("--uninstall", help="Arrêter le lancement automatique.")
    ] = False,
    status: Annotated[
        bool, typer.Option("--status", help="État des alertes : planification, dernier passage.")
    ] = False,
    every: Annotated[
        int,
        typer.Option(help=f"Intervalle en minutes, avec --install (min. {schedule.MIN_EVERY})."),
    ] = schedule.DEFAULT_EVERY,
) -> None:
    """Signaler les nouveautés : supports de cours, notes, absences, agenda.

    Les nouveautés sont envoyées aux canaux configurés avec `myges notify add`. Le premier
    passage enregistre seulement l'état. `myges watch --install` le lance automatiquement.
    """
    if uninstall:
        return _watch_uninstall()
    username = _current_username()
    if username is None:
        raise _not_logged_in()
    if install:
        return _watch_install(every)
    if status:
        return _watch_status(username)
    path = _state_path(username)
    with _exclusive(path.with_suffix(".lock")) as acquired:
        if not acquired:
            return  # un autre passage est en cours : il fera le travail
        _watch_pass(username, path, quiet=quiet, no_notify=no_notify)


def _watch_pass(username: str, path: Path, *, quiet: bool, no_notify: bool) -> None:
    now = datetime.now(_TZ)
    state = _load_state(path) or {}
    previous: dict[str, Any] | None = state.get("snapshot")
    slow_at = state.get("checked", {}).get("slow", 0)
    # À la main (pas --quiet) on vérifie tout ; en tâche planifiée, selon le rythme.
    full = (
        previous is None
        or not quiet
        or now - datetime.fromtimestamp(slow_at / 1000, timezone.utc) >= SLOW_EVERY
    )

    reminders: list[tuple[str, watch_mod.Change]] = []
    try:
        with MyGES(username, _current_password()) as client:
            sections = _fetch(client, state, now, full)
            before = _remind_before()
            if before is not None and previous is not None:
                reminded = state.get("reminded", {})
                agenda = sections.get("agenda") or previous.get("agenda", {})
                reminders = watch_mod.due_reminders(agenda, now, before, reminded, _TZ)
                if reminders and "agenda" not in sections:
                    # Rappel imminent : agenda relu pour donner la salle à jour.
                    sections["agenda"] = watch_mod.agenda_section(
                        client.agenda(now, now + watch_mod.AGENDA_HORIZON), now
                    )
                    reminders = watch_mod.due_reminders(
                        sections["agenda"], now, before, reminded, _TZ
                    )
    except TokenExpiredError:
        # Une seule alerte par expiration, pas une à chaque passage.
        if previous is not None and not state.get("expired_notified"):
            if not no_notify:
                _broadcast(
                    "myGES : session expirée",
                    "ℹ️ La session myGES a expiré : relancer `myges login` "
                    "(ou `myges login --save-password` pour ne plus avoir à le faire).",
                )
            state["expired_notified"] = True
        _write_state(path, state)
        _log(username, "session expirée")
        err.print("[red]Session expirée.[/] Relancer `myges login`.")
        raise typer.Exit(1) from None
    except (MyGESError, httpx.HTTPError) as exc:
        _offline(state, now, type(exc).__name__, notify_on=not no_notify)
        _write_state(path, state)
        _log(username, f"échec : {exc}")
        err.print(f"[red]Erreur :[/] {exc}")
        raise typer.Exit(1) from exc

    state["expired_notified"] = False
    if state.get("failures", {}).get("alerted") and not no_notify:
        _broadcast("myGES : alertes rétablies", "✅ myGES est de nouveau joignable.")
    state.pop("failures", None)

    current = {**(previous or {}), **sections, "taken_at": _ms(now)}
    if previous is None:
        state["snapshot"] = current
        _write_state(path, state)
        _log(username, "état initial enregistré")
        console.print(
            f"État initial enregistré ({len(current.get('grades', {}))} matières notées, "
            f"{sum(len(f['items']) for f in current['files'].values())} supports, "
            f"{len(current.get('agenda', {}))} cours à venir). "
            "Les prochains passages signaleront les nouveautés."
        )
        if not _channels():
            console.print("[dim]Aucun canal de notification : voir `myges notify add --help`.[/]")
        return

    current, state["empty_streaks"] = watch_mod.stabilize(
        previous, current, state.get("empty_streaks", {})
    )
    changes = watch_mod.diff(previous, current, _TZ)
    state["snapshot"] = current
    _remember_history(state, changes, now)
    if reminders:
        if not no_notify:
            _broadcast(
                "myGES : rappel de cours" if len(reminders) == 1 else "myGES : rappels de cours",
                "\n".join(f"⏰ {change.text}" for _, change in reminders),
            )
        for _, change in reminders:
            console.print(f"⏰ {change.text}")
        limit = _ms(now - timedelta(days=1))
        state["reminded"] = {
            rid: start for rid, start in state.get("reminded", {}).items() if start >= limit
        } | {rid: current["agenda"][rid]["start"] for rid, _ in reminders}
    failed = 0 if no_notify else _deliver(state, changes)
    _write_state(path, state)  # état enregistré avant toute relance, quoi qu'il arrive
    resumed = _resume_interrupted_registrations(now)
    _log(
        username,
        f"{'complet' if full else 'supports'} : {len(changes)} nouveauté(s)"
        + (f", {failed} canal(aux) en échec" if failed else "")
        + (f", pré-inscriptions relancées : {resumed}" if resumed else ""),
    )
    if not changes:
        if not quiet:
            console.print("[dim]Rien de nouveau.[/]")
        return
    title, body = notify.format_changes(changes)
    console.print(f"[bold]{title}[/]\n{body}")
    if failed:
        raise typer.Exit(1)


def _watch_status(username: str) -> None:
    state = _load_state(_state_path(username)) or {}
    entry = None
    with contextlib.suppress(schedule.ScheduleError, OSError):
        entry = schedule.find_entry(schedule.read_crontab()) if not schedule.is_windows() else None
    if entry:
        every = entry.split()[0].removeprefix("*/")
        console.print(f"Lancement automatique : [green]actif[/], toutes les {every} min")
    elif schedule.installed():
        console.print("Lancement automatique : [green]actif[/]")
    else:
        console.print("Lancement automatique : [yellow]inactif[/] — `myges watch --install`")
    names = [notify.describe(c).split()[0] for c in _channels()]
    if names:
        console.print(f"Canaux : {', '.join(names)}")
    else:
        console.print("Canaux : [yellow]aucun[/] — `myges notify add`")
    if _current_password() is None:
        console.print("Session : [yellow]mot de passe non enregistré[/] (alertes coupées à "
                      "l'expiration, ~6 jours) — `myges login --save-password`")  # fmt: skip
    queued = sum(len(v) for v in state.get("outbox", {}).values())
    if queued:
        console.print(f"En attente d'envoi : [yellow]{queued}[/] nouveauté(s)")
    if state.get("failures"):
        since = datetime.fromtimestamp(state["failures"]["since"] / 1000, timezone.utc)
        console.print(f"[yellow]myGES injoignable depuis {_fmt_dt(since)}[/]")
    log = _state_path(username).with_name("watch.log")
    try:
        last = log.read_text(encoding="utf-8").splitlines()[-1]
        console.print(f"Dernier passage : {last}")
    except (OSError, IndexError):
        console.print("Dernier passage : [dim]aucun[/]")


def _watch_install(every: int) -> None:
    try:
        done = schedule.install(every)
    except schedule.ScheduleError as exc:
        err.print(f"[red]Échec :[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]Alertes automatiques activées[/] ({done}).")
    # Ce qui empêcherait les alertes d'arriver, avec la solution.
    if not _channels():
        console.print(
            "[yellow]![/] Aucun canal : les nouveautés ne seront envoyées nulle part. "
            "Ajouter `myges notify add ntfy` (ou discord, email)."
        )
    if _current_password() is None:
        console.print(
            "[yellow]![/] Mot de passe non enregistré : les alertes s'arrêteront quand la "
            "session expirera (~6 jours). `myges login --save-password` pour l'éviter."
        )
    if schedule.cron_running() is False:
        if schedule.is_wsl():
            console.print(
                "[yellow]![/] cron ne tourne pas (cas par défaut sous WSL) : "
                "`sudo service cron start`. Pour le démarrer avec WSL, ajouter dans "
                '/etc/wsl.conf : [boot] command="service cron start"'
            )
        else:
            console.print("[yellow]![/] cron ne tourne pas : `sudo systemctl enable --now cron`.")
    console.print("[dim]Arrêter : `myges watch --uninstall`.[/]")


def _watch_uninstall() -> None:
    try:
        removed = schedule.uninstall()
    except schedule.ScheduleError as exc:
        err.print(f"[red]Échec :[/] {exc}")
        raise typer.Exit(1) from exc
    console.print("Alertes automatiques désactivées." if removed else "[dim]Rien à désactiver.[/]")


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
    if not schedule.installed():
        console.print(
            "Prochaine étape : [bold]myges watch --install[/] pour recevoir les alertes "
            "automatiquement."
        )


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


# ---------------------------------------------------------------- Google Calendar

gcal_app = typer.Typer(
    help="Synchroniser l'agenda myGES vers un agenda Google Calendar dédié.",
    no_args_is_help=True,
)
app.add_typer(gcal_app, name="gcal")


def _gcal_settings() -> tuple[Path, str]:
    settings = _load_config().get("gcal") or {}
    if not settings.get("calendar_id"):
        err.print("[red]Google Calendar non configuré.[/] Voir `myges gcal setup --help`.")
        raise typer.Exit(2)
    return Path(settings["key_file"]), str(settings["calendar_id"])


@gcal_app.command("setup")
def gcal_setup(
    key_file: Annotated[Path, typer.Argument(help="Clé JSON du compte de service Google.")],
    calendar_id: Annotated[
        str, typer.Argument(help="ID de l'agenda (Paramètres de l'agenda > Intégrer l'agenda).")
    ],
) -> None:
    """Configurer la synchro (une fois). Guide pas à pas : docs/google-calendar.md du dépôt.

    1. Google Cloud : activer l'API Google Calendar, créer un compte de service et une clé JSON.
    2. Google Calendar : créer un agenda dédié, le partager avec l'adresse du compte de
       service, droit « Apporter des modifications aux événements ».
    3. `myges gcal setup cle.json <id>@group.calendar.google.com`
    """
    from .gcal import GoogleCalendar, GoogleCalendarError

    source = key_file.expanduser()
    dest = _config_path().parent / "gcal-service-account.json"
    try:
        with GoogleCalendar(source, calendar_id) as calendar:
            calendar.check_access()
            email = calendar.service_account_email
    except (GoogleCalendarError, OSError, ValueError) as exc:
        err.print(f"[red]Échec :[/] {exc}")
        raise typer.Exit(1) from exc
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(source.read_bytes())
    config = _load_config()
    config["gcal"] = {"calendar_id": calendar_id, "key_file": str(dest)}
    _store_config(config)
    console.print(f"[green]Agenda accessible[/] via {email}. Clé copiée dans {dest} (0600).")
    console.print("Lancer `myges gcal sync` (puis le planifier, voir le README).")


@gcal_app.command("sync")
def gcal_sync(
    days: Annotated[int, typer.Option(help="Fenêtre synchronisée, en jours.")] = 30,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Afficher sans rien modifier.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="N'afficher que les modifications.")
    ] = False,
) -> None:
    """Créer, mettre à jour et supprimer les cours dans l'agenda Google Calendar.

    Seuls les événements créés par cette commande sont touchés ; un cours annulé côté
    myGES disparaît de l'agenda.
    """
    from .gcal import GoogleCalendar, GoogleCalendarError, sync

    key_file, calendar_id = _gcal_settings()
    now = datetime.now(_TZ)
    with _client() as client:
        events = client.agenda(now - timedelta(days=1), now + timedelta(days=days))
    try:
        with GoogleCalendar(key_file, calendar_id) as calendar:
            report = sync(calendar, events, now, days, dry_run=dry_run)
    except GoogleCalendarError as exc:
        err.print(f"[red]Échec :[/] {exc}")
        raise typer.Exit(1) from exc
    for line in report.created_or_updated:
        console.print(f"[green]+[/] {line}")
    for line in report.deleted:
        console.print(f"[red]−[/] {line}")
    if not quiet or report.created_or_updated or report.deleted:
        console.print(report.summary + (" [dim](simulation)[/]" if dry_run else ""))


# ---------------------------------------------------------------- Rappels, récapitulatif


def _parse_duration(text: str) -> timedelta:
    """« 15m », « 15 », « 1h », « 1h30 », « 90min » → durée."""
    match = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*(?:m|min)?)?\s*", text.lower())
    if match is None or not any(match.groups()):
        raise typer.BadParameter(f"durée invalide : « {text} » (ex. 15m, 1h, 1h30)")
    minutes = int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
    if not 1 <= minutes <= 24 * 60:
        raise typer.BadParameter("entre 1 minute et 24 heures")
    return timedelta(minutes=minutes)


def _fmt_duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return (
        f"{hours} h {minutes:02d}"
        if hours and minutes
        else f"{hours} h"
        if hours
        else f"{minutes} min"
    )


@app.command()
def remind(
    before: Annotated[
        str | None,
        typer.Option("--before", "-b", help="Délai avant le cours : 5m, 15m, 1h, 1h30…"),
    ] = None,
    off: Annotated[bool, typer.Option("--off", help="Désactiver les rappels.")] = False,
) -> None:
    """Rappel avant chaque cours (salle, intervenant), envoyé par `myges watch`.

    Sans option : affiche le réglage actuel. Le rappel part au passage de `myges watch`
    qui suit l'échéance (toutes les 5 min avec `myges watch --install`).
    """
    config = _load_config()
    if off:
        config.pop("remind", None)
        _store_config(config)
        console.print("Rappels de cours désactivés.")
        return
    if before is None:
        current = _remind_before()
        console.print(
            f"Rappels : {_fmt_duration(current)} avant chaque cours." if current
            else "Rappels désactivés. Exemple : `myges remind --before 15m`."
        )  # fmt: skip
        return
    delay = _parse_duration(before)
    config["remind"] = {"before_minutes": int(delay.total_seconds() // 60)}
    _store_config(config)
    console.print(f"[green]Rappels activés[/] : {_fmt_duration(delay)} avant chaque cours.")
    if not schedule.installed():
        console.print("[dim]Ils sont envoyés par `myges watch` : `myges watch --install`.[/]")


def _short_location(event: AgendaEvent) -> str:
    """Une salle, ou « première salle +N salles » pour les événements multi-salles."""
    rooms = event.rooms or []
    if len(rooms) <= 2:
        return event.location
    first = rooms[0]
    shown = f"{first.name} ({first.campus})" if first.campus else first.name or ""
    return f"{shown} +{len(rooms) - 1} salles"


def _summary_text(client: MyGES, username: str, days: int) -> tuple[str, str]:
    now = datetime.now(_TZ)
    end = now + timedelta(days=days)
    lines: list[str] = []
    catalog = _Courses(client, None)

    classes = sorted(client.agenda(now, end), key=lambda e: e.start_date)
    lines.append(f"📅 {len(classes)} cours d'ici le {_fmt_dt(end, '%d/%m')}")
    current_day = None
    for event in classes:
        day = _fmt_day(event.start_date)
        if day != current_day:
            lines.append(f"  {day}")
            current_day = day
        number = catalog.number(_rc_id(event))
        room = f" · {_short_location(event)}" if event.location else ""
        lines.append(
            f"    {_fmt_dt(event.start_date, '%H:%M')} {event.name.strip()}"
            + (f" (#{number})" if number else "")
            + room
        )

    steps = [
        s for s in client.next_project_steps()
        if s.psp_limit_date and now <= s.psp_limit_date <= now + timedelta(days=14)
    ]  # fmt: skip
    if steps:
        lines.append("📌 Échéances (14 jours)")
        lines += [
            f"  {_fmt_dt(s.psp_limit_date, '%d/%m %H:%M')} {s.pro_name} : {s.psp_type}"
            for s in sorted(steps, key=lambda s: s.psp_limit_date or now)
        ]

    events = [
        e for e in client.events()
        if e.is_participant_subscribed and e.event_date and now <= e.event_date <= end
    ]  # fmt: skip
    if events:
        lines.append("🎟️ Événements où tu es inscrit")
        lines += [f"  {_fmt_dt(e.event_date)} {e.event_title}" for e in events]

    state = _load_state(_state_path(username)) or {}
    since = _ms(now - timedelta(days=7))
    recent = [h for h in state.get("history", []) if h["at"] >= since
              and h["kind"] in ("grade", "file", "absence")]  # fmt: skip
    if recent:
        lines.append("🆕 Cette semaine")
        icons = {"grade": "📊", "file": "📄", "absence": "🚫"}
        lines += [f"  {icons[h['kind']]} {h['text']}" for h in recent]
    return f"myGES : ta semaine (jusqu'au {_fmt_dt(end, '%d/%m')})", "\n".join(lines)


@app.command()
def summary(
    send: Annotated[bool, typer.Option("--send", help="Envoyer sur les canaux d'alerte.")] = False,
    days: Annotated[int, typer.Option(help="Période couverte, en jours.")] = 7,
    install: Annotated[
        str | None,
        typer.Option(
            "--install",
            metavar="« JOUR HH:MM »",
            help='Envoi automatique chaque semaine, ex. --install "lundi 07:00".',
        ),
    ] = None,
    uninstall: Annotated[
        bool, typer.Option("--uninstall", help="Arrêter l'envoi automatique.")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", hidden=True)] = False,
) -> None:
    """Récapitulatif : cours à venir, échéances, événements, nouveautés de la semaine."""
    if uninstall:
        try:
            removed = schedule.uninstall_weekly_summary()
        except schedule.ScheduleError as exc:
            err.print(f"[red]Échec :[/] {exc}")
            raise typer.Exit(1) from exc
        console.print("Récapitulatif automatique arrêté." if removed else "[dim]Rien à arrêter.[/]")
        return
    if install is not None:
        try:
            when = schedule.install_weekly_summary(install)
        except schedule.ScheduleError as exc:
            err.print(f"[red]Échec :[/] {exc}")
            raise typer.Exit(1) from exc
        console.print(f"[green]Récapitulatif envoyé {when}[/] sur vos canaux d'alerte.")
        if not _channels():
            console.print("[yellow]![/] Aucun canal : `myges notify add ntfy` (ou discord, email).")
        console.print("[dim]Arrêter : `myges summary --uninstall`.[/]")
        return
    username = _current_username() or ""
    with _client() as client:
        title, body = _summary_text(client, username, days)
    if not quiet:
        console.print(f"[bold]{title}[/]\n{body}", highlight=False)
    if send and _broadcast(title, body):
        raise typer.Exit(1)


# ---------------------------------------------------------------- Boutons de notification


@app.command()
def serve(
    listen: Annotated[
        str,
        typer.Option(
            help="Adresse:port d'écoute, privée (tailnet, réseau local), ex. 100.x.x.x:2587."
        ),
    ],
    public_url: Annotated[
        str | None,
        typer.Option(help="Adresse vue par le téléphone, si différente de --listen."),
    ] = None,
) -> None:
    """Recevoir les boutons « S'inscrire » des notifications ntfy (à laisser tourner).

    Enregistre l'adresse et un jeton dans la config : les alertes « nouvel événement »
    portent alors un bouton qui lance la pré-inscription sur cette machine. N'écouter que
    sur une adresse privée.
    """
    import logging
    import secrets

    from .serve import make_server

    host, _, port = listen.rpartition(":")
    if not host or not port.isdigit():
        raise typer.BadParameter("format attendu : adresse:port, ex. 100.64.0.1:2587")
    if host in ("0.0.0.0", "::", ""):
        err.print("[yellow]![/] Écoute sur toutes les interfaces : préférer une adresse privée.")
    config = _load_config()
    settings = config.get("serve") or {}
    settings["url"] = public_url or f"http://{listen}"
    settings.setdefault("token", secrets.token_urlsafe(24))
    config["serve"] = settings
    _store_config(config)

    def on_subscribe(event_id: int) -> str:
        with _client() as client:
            event = next((e for e in client.events() if e.event_id == event_id), None)
        if event is None:
            raise LookupError(f"événement {event_id} introuvable sur myGES")
        if event.is_participant_subscribed:
            return f"déjà inscrit à {event.event_title}"
        try:
            _subscribe_in_background([event_id])
        except typer.Exit as exc:
            raise RuntimeError("échec du lancement, voir events-background.log") from exc
        return f"pré-inscription lancée : {event.event_title}"

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = make_server(host, int(port), settings["token"], on_subscribe)
    console.print(f"Boutons « S'inscrire » actifs : {settings['url']} (Ctrl+C pour arrêter)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


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
