"""Sections du menu interactif : chargement des données et mise en forme.

Indépendant de Textual. Chaque section charge ses données avec un client :class:`MyGES`
(dans un thread de l'application), puis les met en forme en :class:`View` : un titre, un
résumé et un tableau. Entrée sur une ligne ouvre son détail (:attr:`Section.detail`),
calculé lui aussi dans un thread puisqu'il peut appeler l'API.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal, Union

from rich.console import Group, RenderableType
from rich.text import Text

from .. import blocs as blocs_mod
from .. import cli
from .. import pending as pending_mod
from ..client import MyGES
from ..models import AgendaEvent, Course, Grade, NextProjectStep

Cell = Union[str, Text]  # noqa: UP007 : alias évalué à l'exécution (Python 3.10)

SOON = timedelta(days=3)  # échéance proche : affichée en couleur


# Couleur d'accent du thème courant, fixée par l'application (les styles Rich ne lisent
# pas les variables Textual).
ACCENT = "#5fafd7"


def accent() -> str:
    return f"bold {ACCENT}"


def _now() -> datetime:
    return datetime.now(cli._TZ)


def _dim(text: str) -> Text:
    return Text(text, style="dim")


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


@dataclass
class State:
    """Réglages de l'utilisateur pendant la session (semaine affichée, simulations…)."""

    uid: int | None = None
    week: int = 0  # décalage en semaines depuis la semaine courante
    simulated: dict[int, float] = field(default_factory=dict)  # rc_id → note simulée


@dataclass
class View:
    title: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Cell, ...]] = field(default_factory=list)
    items: list[Any] = field(default_factory=list)  # objet derrière chaque ligne
    summary: RenderableType | None = None
    empty: str = "Rien à afficher."
    flex: tuple[int, ...] = ()  # colonnes de texte long, réduites si la place manque


MIN_FLEX = 14  # largeur en dessous de laquelle une colonne n'est plus réduite


def fit_widths(widths: list[int], flex: Sequence[int], available: int, padding: int) -> list[int]:
    """Réduit les colonnes ``flex`` (la plus large d'abord) pour tenir dans ``available``.

    ``padding`` : marge ajoutée à chaque colonne par le tableau. Sans colonne désignée,
    toutes peuvent être réduites.
    """
    fitted = list(widths)
    candidates = list(flex) or list(range(len(fitted)))
    excess = sum(w + padding for w in fitted) - available
    while excess > 0:
        shrinkable = [i for i in candidates if fitted[i] > MIN_FLEX]
        if not shrinkable:
            break
        widest = max(shrinkable, key=lambda i: fitted[i])
        fitted[widest] -= 1
        excess -= 1
    return fitted


Select = Literal["detail", "simulate", "copy", "none"]


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    load: Callable[[MyGES, State], Any]
    render: Callable[[Any, State], View]
    select: Select = "none"  # effet d'Entrée sur une ligne
    detail: Callable[[MyGES, Any, State], RenderableType] | None = None
    hint: str = ""  # aide affichée sous le tableau
    week_key: bool = False  # les données dépendent de State.week

    def cache_key(self, state: State) -> str:
        return f"{self.key}:{state.week}" if self.week_key else self.key


# ---------------------------------------------------------------- Formats


def fmt_delay(target: datetime, now: datetime) -> str:
    """« dans 2 h 05 », « dans 3 j », « dépassée »."""
    if target <= now:
        return "dépassée"
    return f"dans {cli._fmt_delay(target - now)}"


def _slot_style(event: AgendaEvent, now: datetime) -> str:
    if event.end_date <= now:
        return "dim"
    if event.start_date <= now:
        return "bold green"
    return ""


def _room(event: AgendaEvent) -> str:
    return event.location or event.modality or ""


def _event_row(event: AgendaEvent, now: datetime, day: str | None = None) -> tuple[Cell, ...]:
    style = _slot_style(event, now)
    marker = "● " if event.start_date <= now < event.end_date else ""
    cells: list[Cell] = [Text(marker + cli._fmt_slot(event), style=style)]
    cells += [
        Text(_clean(event.name), style=style),
        Text(_room(event), style=style),
        Text(event.teacher or "", style=style),
    ]
    if day is not None:
        cells.insert(0, Text(day, style="bold" if day else ""))
    return tuple(cells)


def next_class_line(event: AgendaEvent | None, now: datetime) -> Text:
    """Une ligne : cours en cours, ou prochain cours avec le délai."""
    if event is None:
        return _dim("Aucun cours dans les prochains jours.")
    where = " · ".join(x for x in (_room(event), event.teacher) if x)
    line = Text()
    if event.start_date <= now:
        line.append("● En cours ", style="bold green")
        line.append(_clean(event.name), style="bold")
        line.append(f"  jusqu'à {cli._fmt_dt(event.end_date, '%H:%M')}")
    else:
        line.append("Prochain cours ", style="dim")
        line.append(fmt_delay(event.start_date, now), style="bold")
        line.append(f"  {cli._fmt_day(event.start_date)} {cli._fmt_slot(event)}  ")
        line.append(_clean(event.name), style="bold")
    if where:
        line.append(f"  {where}", style="dim")
    return line


def _avg(value: float | None) -> Text:
    if value is None:
        return _dim("—")
    return Text(f"{value:.2f}", style="red" if value < blocs_mod.PASSING else "")


def _deadline(when: datetime | None, now: datetime) -> Text:
    if when is None:
        return _dim("—")
    style = "red" if when <= now else "yellow" if when - now <= SOON else ""
    return Text(f"{cli._fmt_day(when)} {cli._fmt_dt(when, '%H:%M')}", style=style)


# ---------------------------------------------------------------- En-tête


@dataclass
class Header:
    name: str
    classes: list[str]
    events: list[AgendaEvent]
    overall: float | None
    provisional: bool
    steps: list[NextProjectStep]


@dataclass
class GradesData:
    year: int
    courses: list[Course]
    grades: list[Grade]
    blocs: dict[str, list[int]]


def load_grades(client: MyGES, state: State) -> GradesData:
    year = max(client.years())
    return GradesData(year, client.courses(year), client.grades(year), cli._stored_blocs(year))


def _compute(data: GradesData, state: State) -> tuple[list[blocs_mod.BlocResult], Any]:
    return blocs_mod.compute(data.courses, data.grades, data.blocs, state.simulated)


def load_header(client: MyGES, state: State) -> tuple[Header, GradesData]:
    """Données de l'en-tête ; les notes servent aussi à la section « Notes »."""
    now = _now()
    profile = client.profile()
    state.uid = profile.uid
    grades = load_grades(client, state)
    results, others = blocs_mod.compute(grades.courses, grades.grades, grades.blocs)
    lines = [line for r in [*results, others] for line in r.lines]
    header = Header(
        name=f"{profile.firstname} {profile.name}",
        classes=[c.name for c in client.classes(grades.year)],
        events=client.agenda(now - timedelta(hours=12), now + timedelta(days=30)),
        overall=blocs_mod.overall(results, others),
        provisional=any(line.source == "provisoire" for line in lines),
        steps=client.next_project_steps(),
    )
    return header, grades


def render_header(header: Header | None, now: datetime) -> Text:
    title = Text("myGES", style=accent())
    if header is None:
        title.append("  chargement…", style="dim")
        return title
    title.append(f"  {header.name}", style="bold")
    if header.classes:
        title.append(f" · {', '.join(header.classes)}", style="dim")
    upcoming = [e for e in header.events if e.end_date > now]
    current = min(upcoming, key=lambda e: e.start_date, default=None)
    lines = [title, next_class_line(current, now)]

    status = Text()
    status.append("Moyenne générale ", style="dim")
    status.append_text(_avg(header.overall))
    if header.provisional and header.overall is not None:
        status.append(" (avec des notes provisoires)", style="dim")
    future = sorted(
        (s for s in header.steps if s.psp_limit_date and s.psp_limit_date > now),
        key=lambda s: s.psp_limit_date or now,
    )
    if future:
        step = future[0]
        when = step.psp_limit_date or now
        status.append("   Prochaine échéance ", style="dim")
        status.append(f"{_clean(step.pro_name)}", style="bold")
        status.append(f" ({step.psp_type}) ")
        status.append_text(_deadline(when, now))
        status.append(f" · {fmt_delay(when, now)}", style="dim")
    lines.append(status)
    return Text("\n").join(lines)


# ---------------------------------------------------------------- Agenda


def load_today(client: MyGES, state: State) -> tuple[list[AgendaEvent], AgendaEvent | None]:
    now = _now()
    events = sorted(
        client.agenda(now.date(), now.date() + timedelta(days=1)), key=lambda e: e.start_date
    )
    return events, None if events else cli._next_event(client, now)


def render_today(data: tuple[list[AgendaEvent], AgendaEvent | None], state: State) -> View:
    events, upcoming = data
    now = _now()
    view = View(
        f"Aujourd'hui, {cli._fmt_day(now)}",
        ["Horaire", "Cours", "Salle", "Intervenant"],
        [_event_row(e, now) for e in events],
        list(events),
        empty="Pas de cours aujourd'hui.",
        flex=(1, 2, 3),
    )
    if not events:
        view.summary = next_class_line(upcoming, now)
    else:
        left = [e for e in events if e.end_date > now]
        view.summary = _dim(
            f"{len(events)} cours" + (f", dont {len(left)} encore à venir" if left else ", finis")
        )
    return view


def week_bounds(state: State) -> tuple[date, date]:
    today = _now().date()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=state.week)
    return monday, monday + timedelta(days=7)


def load_week(client: MyGES, state: State) -> list[AgendaEvent]:
    first, last = week_bounds(state)
    return sorted(client.agenda(first, last), key=lambda e: e.start_date)


def render_week(events: list[AgendaEvent], state: State) -> View:
    first, last = week_bounds(state)
    now = _now()
    rows: list[tuple[Cell, ...]] = []
    previous = None
    for event in events:
        day = cli._fmt_day(event.start_date)
        rows.append(_event_row(event, now, day if day != previous else ""))
        previous = day
    label = {0: "cette semaine", 1: "semaine prochaine", -1: "semaine dernière"}.get(state.week)
    hours = sum((e.end_date - e.start_date).total_seconds() for e in events) / 3600
    title = f"Semaine du {first:%d/%m} au {last - timedelta(days=1):%d/%m}"
    return View(
        title + (f" ({label})" if label else ""),
        ["Jour", "Horaire", "Cours", "Salle", "Intervenant"],
        rows,
        list(events),
        summary=_dim(f"{len(events)} cours · {hours:g} h") if events else None,
        empty="Aucun cours cette semaine.",
        flex=(2, 3, 4),
    )


def detail_event(client: MyGES, event: AgendaEvent, state: State) -> RenderableType:
    parts: list[RenderableType] = [Text(_clean(event.name), style="bold")]
    parts.append(Text(f"{cli._fmt_day(event.start_date)} {cli._fmt_slot(event)}"))
    for label, value in (
        ("Salle", _room(event)),
        ("Intervenant", event.teacher),
        ("Type", event.type),
        ("Modalité", event.modality),
        ("Promotion", event.promotion),
    ):
        if value:
            parts.append(Text.assemble((f"{label} : ", "dim"), value))
    if event.comment and event.comment.strip():
        parts += [Text(), Text(cli._html_to_text(event.comment))]
    return Group(*parts)


# ---------------------------------------------------------------- Notes


_MARKS = {"simulée": " *", "provisoire": " ~", "officielle": "", "aucune": ""}


def render_grades(data: GradesData, state: State) -> View:
    results, others = _compute(data, state)
    lines = {line.rc_id: line for r in [*results, others] for line in r.lines}
    by_id = {g.rc_id: g for g in data.grades}
    courses = sorted(data.courses, key=lambda c: cli._fold(c.name))
    rows: list[tuple[Cell, ...]] = []
    for number, course in enumerate(courses, 1):
        line, grade = lines[course.rc_id], by_id.get(course.rc_id)
        average = _avg(line.average)
        average.append(_MARKS[line.source], style="cyan" if line.source == "simulée" else "dim")
        coef = f"{line.weight:g}" if line.official_weight else "—"
        rows.append(
            (
                str(number),
                course.name,
                " ".join(f"{n:g}" for n in grade.grades) if grade else "",
                f"{grade.exam:g}" if grade and grade.exam is not None else "",
                average,
                coef,
                f"{course.ects:g}" if course.ects is not None else "",
                str(grade.absences) if grade and grade.absences else "",
            )
        )
    summary = Text()
    summary.append("Moyenne générale ", style="bold")
    summary.append_text(_avg(blocs_mod.overall(results, others)))
    for result in results:
        verdict = {True: (" validé", "green"), False: (" non validé", "red"), None: ("", "")}
        text, style = verdict[result.validated]
        summary.append(f"   {result.name} ", style="dim")
        summary.append_text(_avg(result.average))
        summary.append(text, style=style)
    if state.simulated:
        summary.append(f"\n{len(state.simulated)} note(s) simulée(s) (*)", style="cyan")
    summary.append("\n~ provisoire (contrôle continu) · — coefficient non publié, compté 1",
                   style="dim")  # fmt: skip
    return View(
        f"Notes {data.year}-{data.year + 1}",
        ["#", "Matière", "Notes", "Exam", "Moyenne", "Coef", "ECTS", "Abs."],
        rows,
        courses,
        summary=summary,
        empty="Aucune matière cette année.",
        flex=(1,),
    )


def parse_grade(raw: str) -> float | None:
    """« 14 », « 12,5 » → note sur 20 ; vide → None. ValueError si invalide."""
    raw = raw.strip().replace(",", ".")
    if not raw:
        return None
    value = float(raw)
    if not 0 <= value <= 20:
        raise ValueError("la note doit être entre 0 et 20")
    return value


# ---------------------------------------------------------------- Scolarité


def load_absences(client: MyGES, state: State) -> list[Any]:
    return sorted(client.absences(), key=lambda a: a.date, reverse=True)


def render_absences(data: list[Any], state: State) -> View:
    unjustified = sum(1 for a in data if not a.justified)
    return View(
        "Absences",
        ["Date", "Matière", "Type", "Justifiée"],
        [
            (
                cli._fmt_dt(a.date),
                a.course_name,
                a.type or "",
                Text("oui", style="green") if a.justified else Text("non", style="red"),
            )
            for a in data
        ],
        list(data),
        summary=_dim(f"{len(data)} absence(s), dont {unjustified} non justifiée(s)")
        if data
        else None,
        empty="Aucune absence cette année.",
        flex=(1,),
    )


def load_courses(client: MyGES, state: State) -> list[Course]:
    return sorted(client.courses(), key=lambda c: cli._fold(c.name))


def render_courses(data: list[Course], state: State) -> View:
    return View(
        "Matières",
        ["#", "Matière", "Intervenant", "Coef", "ECTS", "Trimestre", "Docs"],
        [
            (
                str(n),
                c.name,
                c.teacher or "",
                f"{c.coef:g}" if c.coef is not None else "",
                f"{c.ects:g}" if c.ects is not None else "",
                c.trimester or "",
                "oui" if c.has_documents else "",
            )
            for n, c in enumerate(data, 1)
        ],
        list(data),
        summary=_dim("Le numéro # est celui des commandes : myges syllabus 3, myges files 3…"),
        flex=(1, 2),
    )


def detail_course(client: MyGES, course: Course, state: State) -> RenderableType:
    parts: list[RenderableType] = [Text(course.name, style="bold")]
    facts = " · ".join(
        x
        for x in (
            course.teacher,
            f"coef {course.coef:g}" if course.coef is not None else None,
            f"{course.ects:g} ECTS" if course.ects is not None else None,
            course.trimester,
        )
        if x
    )
    parts.append(_dim(facts))
    syllabus = client.syllabus(course.rc_id)
    if syllabus is None:
        parts += [Text(), _dim("Pas de syllabus publié pour cette matière.")]
        return Group(*parts)
    for label, value in (
        ("Objectifs", syllabus.teaching_goals),
        ("Prérequis", syllabus.prerequisite),
        ("Plan", syllabus.detail_plan),
        ("Méthode", syllabus.teaching_method),
        ("Outils", syllabus.computing_tools),
        ("Évaluation", syllabus.evaluation_type),
        ("Critères", syllabus.evaluation_criteria),
        ("Références", syllabus.books_reference),
    ):
        if value and value.strip():
            parts += [Text(), Text(label, style=accent()), Text(cli._html_to_text(value))]
    sessions = [s for s in syllabus.seance_details or [] if s.content and s.content.strip()]
    if sessions:
        parts += [Text(), Text("Séances", style=accent())]
        for s in sessions:
            parts.append(Text.assemble((f"{s.number or '?':>2}. ", "dim"),
                                       cli._html_to_text(s.content or "")))  # fmt: skip
    return Group(*parts)


# ---------------------------------------------------------------- Projets


@dataclass
class ProjectRow:
    kind: str  # « Projet » ou « TP »
    project: Any


def load_projects(client: MyGES, state: State) -> list[ProjectRow]:
    if state.uid is None:
        state.uid = client.profile().uid
    rows = [ProjectRow("Projet", p) for p in client.projects()]
    rows += [ProjectRow("TP", p) for p in client.practicals()]
    now = _now()
    far = now + timedelta(days=36500)
    return sorted(rows, key=lambda r: cli._next_deadline(r.project, now) or far)


def render_projects(data: list[ProjectRow], state: State) -> View:
    now = _now()
    rows: list[tuple[Cell, ...]] = []
    for row in data:
        p = row.project
        group = cli._my_group(p, state.uid) if state.uid is not None else None
        deadline = cli._next_deadline(p, now)
        rows.append(
            (
                row.kind,
                _clean(p.name),
                p.course_name or "",
                group.group_name if group else _dim("—"),
                _deadline(deadline, now),
            )
        )
    return View(
        "Projets et TP",
        ["Type", "Nom", "Matière", "Mon groupe", "Prochaine échéance"],
        rows,
        list(data),
        empty="Aucun projet ni TP cette année.",
        flex=(1, 2, 3),
    )


def detail_project(client: MyGES, row: ProjectRow, state: State) -> RenderableType:
    p = row.project
    now = _now()
    parts: list[RenderableType] = [
        Text(_clean(p.name), style="bold"),
        _dim(f"{row.kind} · {p.course_name or '—'} · {p.author or '—'}"),
    ]
    group = cli._my_group(p, state.uid) if state.uid is not None else None
    parts.append(Text())
    if group is None:
        parts.append(Text("Vous n'êtes dans aucun groupe pour ce projet.", style="yellow"))
    else:
        members = ", ".join(
            f"{st.firstname} {st.name}" for st in group.project_group_students or []
        )
        parts.append(Text.assemble(("Mon groupe : ", "bold"), f"{group.group_name} — {members}"))
        if group.date_presentation:
            parts.append(Text(f"Soutenance : {cli._fmt_dt(group.date_presentation)}"))
        parts.append(
            Text("Sujet validé", style="green")
            if group.subject_validated
            else Text("Sujet en attente de validation", style="dim")
        )
        if group.teacher_comment:
            parts.append(Text(f"Commentaire : {cli._html_to_text(group.teacher_comment)}"))
    for step in sorted(
        p.steps or [], key=lambda s: s.psp_limit_date or datetime.max.replace(tzinfo=cli._TZ)
    ):
        title = Text(f"Étape {step.psp_number} · {step.psp_type}", style=accent())
        if step.psp_limit_date:
            title.append("  à rendre avant le ", style="dim")
            title.append_text(_deadline(step.psp_limit_date, now))
        parts += [Text(), title]
        if step.psp_desc and step.psp_desc.strip():
            parts.append(Text(cli._html_to_text(step.psp_desc)))
        for f in step.files or []:
            if group and f.pgr_id == group.project_group_id:
                parts.append(Text(f"  déposé : {f.psf_name} ({cli._fmt_dt(f.psf_end_upload)})",
                                  style="green"))  # fmt: skip
    files = p.project_files or []
    if files:
        parts += [Text(), Text("Pièces jointes", style=accent())]
        parts += [Text(f"  {f.filename}") for f in files]
        parts.append(_dim(f"  myges {'practicals' if row.kind == 'TP' else 'projects'} "
                          f"--inspect {p.project_id} --download pour les récupérer"))  # fmt: skip
    if group is not None:
        chat = [m for m in client.project_group_messages(group.project_group_id)
                if not m.is_deleted][-cli.CHAT_LINES:]  # fmt: skip
        if chat:
            parts += [Text(), Text("Chat du groupe", style=accent())]
            for m in chat:
                author = f"{m.firstname or ''} {m.name or ''}".strip() or "?"
                parts.append(Text.assemble((f"{cli._fmt_dt(m.date, '%d/%m %H:%M')} ", "dim"),
                                           (f"{author} : ", "bold"), m.message))  # fmt: skip
    return Group(*parts)


def load_steps(client: MyGES, state: State) -> list[NextProjectStep]:
    now = _now()
    return sorted(client.next_project_steps(), key=lambda s: s.psp_limit_date or now)


def render_steps(data: list[NextProjectStep], state: State) -> View:
    now = _now()
    return View(
        "Prochaines échéances",
        ["Date limite", "Délai", "Projet", "Matière", "Étape"],
        [
            (
                _deadline(s.psp_limit_date, now),
                fmt_delay(s.psp_limit_date, now) if s.psp_limit_date else "",
                _clean(s.pro_name),
                s.course_name or "",
                s.psp_type or "",
            )
            for s in data
        ],
        list(data),
        empty="Aucune échéance à venir.",
        flex=(2, 3, 4),
    )


# ---------------------------------------------------------------- Vie de campus


NEWS_COUNT = 30


def load_news(client: MyGES, state: State) -> list[Any]:
    return list(client.news(0, NEWS_COUNT).content)


def render_news(data: list[Any], state: State) -> View:
    return View(
        "Actualités",
        ["Date", "Titre", "Auteur"],
        [(cli._fmt_dt(n.date, "%d/%m/%Y"), _clean(n.title), n.author or "") for n in data],
        list(data),
        flex=(1, 2),
    )


def detail_news(client: MyGES, item: Any, state: State) -> RenderableType:
    if not (item.html or item.text):
        item = client.news_item(item.ne_id)
    parts: list[RenderableType] = [
        Text(_clean(item.title), style="bold"),
        _dim(f"{cli._fmt_dt(item.date, '%d/%m/%Y')} · {item.author or '—'}"),
    ]
    content = item.html or item.text or item.summary or ""
    if content:
        parts += [Text(), Text(cli._html_to_text(content))]
    links = cli._html_links(item.html or "")
    if links:
        parts += [Text(), Text("Liens", style=accent())]
        parts += [Text(f"  {label} : {url}" if label != url else f"  {url}")
                  for label, url in links]  # fmt: skip
    return Group(*parts)


def load_events(client: MyGES, state: State) -> list[Any]:
    now = _now()
    upcoming = [e for e in client.events() if e.event_date is None or e.event_date >= now]
    return sorted(upcoming, key=lambda e: e.event_date or now)


def render_events(data: list[Any], state: State) -> View:
    now = _now()
    waiting = {p.event_id: p for p in pending_mod.load()}
    return View(
        "Événements à venir",
        ["Date", "Événement", "Lieu", "Inscriptions", "Inscrit"],
        [
            (
                cli._fmt_dt(e.event_date),
                _clean(e.event_title),
                e.location or "",
                Text.from_markup(cli._subscription_window(e, now)),
                Text.from_markup(cli._subscription_state(e, waiting.get(e.event_id))),
            )
            for e in data
        ],
        list(data),
        empty="Aucun événement à venir.",
        flex=(1, 2),
    )


def detail_campus_event(client: MyGES, event: Any, state: State) -> RenderableType:
    now = _now()
    waiting = {p.event_id: p for p in pending_mod.load()}.get(event.event_id)
    subscribed = cli._subscription_state(event, waiting) or "non inscrit"
    parts: list[RenderableType] = [
        Text(_clean(event.event_title), style="bold"),
        _dim(
            f"{cli._fmt_dt(event.event_date)} · {event.location or '—'} · "
            f"organisé par {event.organizer or '—'}"
        ),
        Text.from_markup(f"Inscriptions : {cli._subscription_window(event, now)} · {subscribed}"),
    ]
    if event.description:
        parts += [Text(), Text(cli._html_to_text(event.description))]
    parts += [
        Text(),
        _dim(f"S'inscrire : myges events subscribe {event.event_id}"),
        _dim(f"Sur myGES : https://myges.fr/common/event/{event.event_id}"),
    ]
    return Group(*parts)


@dataclass
class TeacherRow:
    name: str
    email: str | None
    courses: list[str]


def load_teachers(client: MyGES, state: State) -> list[TeacherRow]:
    taught = client.courses()
    return [
        TeacherRow(
            f"{p.firstname} {p.lastname}",
            p.email,
            sorted({c.name for c in taught if c.teacher_id == p.uid}, key=cli._fold),
        )
        for p in sorted(client.teachers(), key=lambda p: cli._fold(p.lastname))
    ]


def render_teachers(data: list[TeacherRow], state: State) -> View:
    return View(
        "Intervenants",
        ["Nom", "Matières", "E-mail"],
        [(t.name, ", ".join(t.courses), t.email or _dim("—")) for t in data],
        list(data),
        empty="Aucun intervenant.",
        flex=(1, 2),
    )


# ---------------------------------------------------------------- Catalogue


SECTIONS: Sequence[Section] = (
    Section("today", "Aujourd'hui", load_today, render_today, "detail", detail_event),
    Section(
        "week", "Semaine", load_week, render_week, "detail", detail_event,
        hint="[ ] semaine précédente / suivante · t cette semaine", week_key=True,
    ),
    Section(
        "grades", "Notes", load_grades, render_grades, "simulate",
        hint="Entrée : simuler une note · x effacer les simulations",
    ),
    Section("absences", "Absences", load_absences, render_absences),
    Section(
        "courses", "Matières", load_courses, render_courses, "detail", detail_course,
        hint="Entrée : syllabus",
    ),
    Section(
        "projects", "Projets et TP", load_projects, render_projects, "detail", detail_project,
        hint="Entrée : groupe, étapes et chat",
    ),
    Section("steps", "Échéances", load_steps, render_steps),
    Section(
        "news", "Actualités", load_news, render_news, "detail", detail_news,
        hint="Entrée : lire",
    ),
    Section(
        "events", "Événements", load_events, render_events, "detail", detail_campus_event,
        hint="Entrée : détail",
    ),
    Section(
        "teachers", "Intervenants", load_teachers, render_teachers, "copy",
        hint="Entrée : copier l'e-mail",
    ),
)  # fmt: skip
