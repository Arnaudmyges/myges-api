"""Application Textual du menu interactif (``myges`` seul, ou ``myges menu``).

En haut, l'en-tête : qui est connecté, cours en cours ou prochain cours, moyenne générale
et prochaine échéance. À gauche le menu des sections, à droite leur contenu. Les appels à
l'API passent par des threads : l'interface ne se fige jamais, et chaque section reste en
mémoire jusqu'à ``r`` (actualiser).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

import httpx
from rich.cells import cell_len
from rich.console import RenderableType
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from .. import cli
from .._version import __version__
from ..client import MyGES
from ..errors import AuthenticationError, InvalidCredentialsError, MyGESError, TokenExpiredError
from . import sections as sec

DARK = Theme(
    name="myges",
    primary="#5fafd7",
    secondary="#8a8f98",
    accent="#5fafd7",
    foreground="#e4e4e4",
    background="#121417",
    surface="#1b1e23",
    panel="#2a2e36",
    success="#87af87",
    warning="#d7af5f",
    error="#d75f5f",
    dark=True,
)
LIGHT = Theme(
    name="myges-clair",
    primary="#1f6fa8",
    secondary="#6b7280",
    accent="#1f6fa8",
    foreground="#1f2328",
    background="#fafafa",
    surface="#f0f1f3",
    panel="#dfe2e6",
    success="#2f7d32",
    warning="#9a6700",
    error="#c62828",
    dark=False,
)

CSS = """
Screen { background: $background; }

#header {
    height: auto;
    padding: 1 2;
    border-bottom: solid $panel;
}

#body { height: 1fr; }

#menu {
    width: 24;
    height: 1fr;
    padding: 1 0;
    background: $background;
    border-right: solid $panel;
}
#menu:focus { background-tint: $foreground 0%; }
#menu > ListItem {
    padding: 0 1;
    background: $background;
    border-left: thick $background;
}
#menu > ListItem.-highlight {
    color: $foreground;
    background: $surface;
    text-style: none;
    border-left: thick $panel;
}
#menu:focus > ListItem.-highlight {
    border-left: thick $primary;
    text-style: bold;
}
#menu > ListItem#menu-quit { color: $text-muted; margin-top: 1; }

#content { padding: 1 2 0 2; }
#title { height: 1; color: $primary; text-style: bold; }
#summary { height: auto; margin-top: 1; }
#table { height: 1fr; margin-top: 1; background: $background; }
#table:focus { background-tint: $foreground 0%; }
#table > .datatable--header { background: $background; color: $text-muted; }
#table > .datatable--cursor { background: $surface; }
#table:focus > .datatable--cursor { background: $panel; }
#message { margin-top: 1; color: $text-muted; }
#message.error { color: $error; }
#hint { dock: bottom; height: 1; color: $text-muted; }

DetailModal, SimulateModal { align: center middle; background: $background 60%; }
#detail {
    width: 90%;
    max-width: 110;
    height: 85%;
    padding: 1 2;
    border: round $primary;
    background: $surface;
    scrollbar-size-vertical: 1;
}
#simulate {
    width: 56;
    height: auto;
    padding: 1 2;
    border: round $primary;
    background: $surface;
}
#simulate Input { margin-top: 1; }
#simulate-error { color: $error; height: auto; }

LoginScreen { align: center middle; }
#login {
    width: 64;
    height: auto;
    padding: 1 2;
    border: round $primary;
    background: $surface;
}
#login-title { text-style: bold; color: $primary; }
#login-message { color: $text-muted; margin-bottom: 1; }
#login Input { margin-bottom: 1; }
#login-error { color: $error; height: auto; }
#login Button { margin-top: 1; width: 100%; }
"""


def _explain(exc: BaseException) -> str:
    """Message d'erreur lisible pour l'utilisateur."""
    if isinstance(exc, httpx.TimeoutException):
        return "myGES ne répond pas (délai dépassé). Réessayer avec r."
    if isinstance(exc, httpx.HTTPError):
        return "Pas de connexion à myGES : vérifier le réseau, puis r pour réessayer."
    if isinstance(exc, MyGESError):
        return f"myGES : {exc}"
    return f"Erreur inattendue : {exc!r}"


AUTH_ERRORS = (TokenExpiredError, AuthenticationError)
SESSION_EXPIRED = "Session expirée (ou mot de passe enregistré refusé) : se reconnecter."


def _cell_width(cell: sec.Cell) -> int:
    return cell.cell_len if isinstance(cell, Text) else cell_len(cell)


def _cell(cell: sec.Cell) -> Text:
    """Cellule affichable : jamais interprétée comme du balisage (un titre « [Info] … »
    resterait intact), coupée avec « … » si la colonne est trop étroite."""
    text = cell.copy() if isinstance(cell, Text) else Text(cell)
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


class ContentTable(DataTable[Any]):
    """Tableau de la section ; ← ou Échap revient au menu."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left,escape", "back", "Menu", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def action_back(self) -> None:
        self.screen.query_one("#menu", ListView).focus()


class MainScreen(Screen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "app.quit", "Quitter"),
        Binding("r", "reload", "Actualiser"),
        Binding("right", "focus_content", show=False),
        Binding("j", "menu_down", show=False),
        Binding("k", "menu_up", show=False),
        Binding("left_square_bracket", "week(-1)", "Sem. préc."),
        Binding("right_square_bracket", "week(1)", "Sem. suiv."),
        Binding("t", "this_week", "Cette semaine"),
        Binding("x", "clear_simulations", "Effacer les simulations"),
    ]

    app: MygesApp

    def __init__(self) -> None:
        super().__init__()
        self.current: sec.Section | None = None
        self.header: sec.Header | None = None
        self.header_error: str | None = None
        self.ready = False
        self._items: list[Any] = []
        self._loading: set[str] = set()
        self._errors: dict[str, str] = {}
        self._shown_key: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(sec.render_header(None, sec._now()), id="header")
        with Horizontal(id="body"):
            yield ListView(
                *[ListItem(Label(s.label), id=f"menu-{s.key}") for s in sec.SECTIONS],
                ListItem(Label("Quitter"), id="menu-quit"),
                id="menu",
            )
            with Vertical(id="content"):
                yield Static(id="title")
                yield Static(id="summary")
                yield ContentTable(id="table", cursor_type="row")
                yield Static(id="message")
                yield Static(id="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#menu", ListView).focus()
        self.set_interval(30, self._render_header)
        self.start()

    # ------------------------------------------------------------ Chargement

    def start(self) -> None:
        """(Re)démarre : vérifie la session, puis charge l'en-tête et la section."""
        self.ready = False
        self._errors.clear()
        if not self.app.logged_in():
            self.app.need_login()
            return
        self._message("Connexion à myGES…")
        self._connect()

    @work(thread=True, exclusive=True, group="connect")
    def _connect(self) -> None:
        # Un seul thread obtient le jeton : pas d'authentifications en parallèle.
        try:
            with self.app.client() as client:
                client.get_token()
        except AUTH_ERRORS:
            self.app.call_from_thread(self.app.need_login, SESSION_EXPIRED)
            return
        except Exception as exc:
            self.app.call_from_thread(self._message, _explain(exc), True)
            return
        self.app.call_from_thread(self._connected)
        self._check_version()

    def _connected(self) -> None:
        self.ready = True
        self._load_header()
        self.show()
        self._prefetch()

    def _check_version(self) -> None:
        if os.environ.get("MYGES_NO_UPDATE_CHECK"):
            return
        from ..updates import newer_version

        try:
            latest = newer_version(__version__)
        except Exception:
            return
        if latest:
            self.app.call_from_thread(
                self.app.notify,
                f"Nouvelle version {latest} disponible (vous avez {__version__}) : "
                "pipx upgrade myges-api",
                timeout=8,
            )

    @work(thread=True, exclusive=True, group="header")
    def _load_header(self) -> None:
        try:
            with self.app.client() as client:
                header, grades = sec.load_header(client, self.app.state)
        except AUTH_ERRORS:
            self.app.call_from_thread(self.app.need_login, SESSION_EXPIRED)
            return
        except Exception as exc:
            self.app.call_from_thread(self._header_failed, _explain(exc))
            return
        self.app.call_from_thread(self._header_loaded, header, grades)

    def _header_loaded(self, header: sec.Header, grades: sec.GradesData) -> None:
        self.header, self.header_error = header, None
        self.app.cache["grades"] = grades
        self._errors.pop("grades", None)
        self._render_header()
        # Notes déjà en mémoire ; projets : l'identifiant de l'étudiant est maintenant connu.
        if self.current and self.current.key in ("grades", "projects"):
            self.show()

    def _header_failed(self, message: str) -> None:
        self.header_error = message
        self._render_header()

    def _render_header(self) -> None:
        text = sec.render_header(self.header, sec._now())
        if self.header_error and self.header is None:
            text = Text.assemble(("myGES", sec.accent()), "  ", (self.header_error, "red"))
        self.query_one("#header", Static).update(text)

    def show(self) -> None:
        """Affiche la section courante : depuis la mémoire, sinon en la chargeant."""
        section = self.current
        hint = self.query_one("#hint", Static)
        self.refresh_bindings()
        if section is None:
            self.query_one("#title", Static).update("Quitter")
            hint.update("")
            self._message("Entrée pour quitter (ou q à tout moment).")
            return
        hint.update(section.hint)
        self.query_one("#title", Static).update(section.label)
        if not self.ready:
            return
        key = section.cache_key(self.app.state)
        if key in self.app.cache:
            self._display(section, self.app.cache[key])
        elif key in self._errors:
            self._message(self._errors[key] + "\n\nr pour réessayer.", error=True)
        else:
            self._message("Chargement…")
            if key not in self._loading:
                self._loading.add(key)
                self._load(section, key)

    @work(thread=True)
    def _load(self, section: sec.Section, key: str) -> None:
        self._fetch(section, key)

    @work(thread=True, exclusive=True, group="prefetch")
    def _prefetch(self) -> None:
        """Charge les autres sections une à une en arrière-plan : la navigation devient
        instantanée, sans multiplier les requêtes simultanées vers myGES."""
        for section in sec.SECTIONS:
            key = section.cache_key(self.app.state)
            if key in self.app.cache or key in self._loading or key in self._errors:
                continue
            self._loading.add(key)
            self._fetch(section, key)

    def _fetch(self, section: sec.Section, key: str) -> None:
        """Charge une section (dans un thread) et transmet le résultat à l'interface."""
        try:
            with self.app.client() as client:
                data = section.load(client, self.app.state)
        except AUTH_ERRORS:
            self.app.call_from_thread(self._load_failed, key, SESSION_EXPIRED, True)
            return
        except Exception as exc:
            self.app.call_from_thread(self._load_failed, key, _explain(exc), False)
            return
        self.app.call_from_thread(self._loaded, key, data)

    def _loaded(self, key: str, data: Any) -> None:
        self._loading.discard(key)
        self.app.cache[key] = data
        if self.current and self.current.cache_key(self.app.state) == key:
            self.show()

    def _load_failed(self, key: str, message: str, auth: bool) -> None:
        self._loading.discard(key)
        if auth:
            self.app.need_login(message)
            return
        self._errors[key] = message
        if self.current and self.current.cache_key(self.app.state) == key:
            self.show()

    # ------------------------------------------------------------ Affichage

    def _message(self, text: str, error: bool = False) -> None:
        self.query_one("#table", ContentTable).display = False
        self.query_one("#summary", Static).display = False
        message = self.query_one("#message", Static)
        message.set_class(error, "error")
        message.update(text)
        message.display = True
        self._items = []
        self._shown_key = None

    def _display(self, section: sec.Section, data: Any) -> None:
        view = section.render(data, self.app.state)
        key = section.cache_key(self.app.state)
        self.query_one("#title", Static).update(view.title)
        summary = self.query_one("#summary", Static)
        summary.update(view.summary or "")
        summary.display = view.summary is not None
        table = self.query_one("#table", ContentTable)
        keep = table.cursor_row if self._shown_key == key else 0
        table.clear(columns=True)
        # Largeurs calculées ici : DataTable ne les mesure qu'au repos de son écran, ce qui
        # n'arrive pas toujours quand on remet en forme derrière une fenêtre (simulation).
        # Elles sont ensuite ajustées à la place disponible (noms de matières très longs).
        natural = [
            max([cell_len(label), *(_cell_width(row[index]) for row in view.rows)])
            for index, label in enumerate(view.columns)
        ]
        available = self.query_one("#content", Vertical).content_region.width - 2
        widths = sec.fit_widths(natural, view.flex, available, 2 * table.cell_padding)
        for label, width in zip(view.columns, widths, strict=True):
            table.add_column(label, width=width)
        for row in view.rows:
            table.add_row(*(_cell(cell) for cell in row))
        self._items = view.items
        self._shown_key = key
        message = self.query_one("#message", Static)
        message.set_class(False, "error")
        message.update(view.empty)
        message.display = not view.rows
        table.display = bool(view.rows)
        if view.rows:
            table.move_cursor(row=min(keep, len(view.rows) - 1))

    def on_resize(self) -> None:
        if self._shown_key is not None:
            self.show()

    def rerender(self) -> None:
        """Remet en forme sans recharger (changement de thème, simulation…)."""
        self._render_header()
        self.show()

    # ------------------------------------------------------------ Événements

    @on(ListView.Highlighted, "#menu")
    def _highlighted(self, event: ListView.Highlighted) -> None:
        item_id = event.item.id if event.item else None
        self.current = next((s for s in sec.SECTIONS if f"menu-{s.key}" == item_id), None)
        self.show()

    @on(ListView.Selected, "#menu")
    def _selected(self, event: ListView.Selected) -> None:
        if event.item.id == "menu-quit":
            self.app.exit()
            return
        self.action_focus_content()

    @on(DataTable.RowSelected, "#table")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        section = self.current
        if section is None or not 0 <= event.cursor_row < len(self._items):
            return
        item = self._items[event.cursor_row]
        if section.select == "detail" and section.detail is not None:
            self.app.push_screen(DetailModal(section, item))
        elif section.select == "simulate":
            self._simulate(item)
        elif section.select == "copy":
            email = getattr(item, "email", None)
            if email:
                self.app.copy_to_clipboard(email)
                self.notify(f"E-mail copié : {email}")
            else:
                self.notify("Pas d'e-mail connu pour cette personne.", severity="warning")

    def _simulate(self, course: Any) -> None:
        current = self.app.state.simulated.get(course.rc_id)

        def apply(result: tuple[float | None] | None) -> None:
            if result is None:
                return
            (value,) = result
            if value is None:
                self.app.state.simulated.pop(course.rc_id, None)
            else:
                self.app.state.simulated[course.rc_id] = value
            self.show()

        self.app.push_screen(SimulateModal(course.name, current), apply)

    # ------------------------------------------------------------ Actions

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        key = self.current.key if self.current else None
        if action in ("week", "this_week"):
            return key == "week"
        if action == "clear_simulations":
            return key == "grades" and bool(self.app.state.simulated)
        return True

    def action_reload(self) -> None:
        """Oublie les données en mémoire et recharge tout."""
        self.app.cache.clear()
        self._loading.clear()
        self.start()

    def action_focus_content(self) -> None:
        table = self.query_one("#table", ContentTable)
        if table.display and table.row_count:
            table.focus()

    def action_menu_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_menu_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()

    def action_week(self, step: int) -> None:
        self.app.state.week += step
        self.show()

    def action_this_week(self) -> None:
        self.app.state.week = 0
        self.show()

    def action_clear_simulations(self) -> None:
        self.app.state.simulated.clear()
        self.show()


class DetailModal(ModalScreen[None]):
    """Détail d'une ligne (syllabus, projet, actualité…), chargé dans un thread."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q,left", "dismiss", "Fermer"),
        Binding("j", "scroll_down", show=False),
        Binding("k", "scroll_up", show=False),
    ]

    app: MygesApp

    def __init__(self, section: sec.Section, item: Any) -> None:
        super().__init__()
        self.section = section
        self.item = item

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="detail"):
            yield Static(Text("Chargement…", style="dim"), id="detail-body")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#detail", VerticalScroll).focus()
        self._fetch()

    @work(thread=True)
    def _fetch(self) -> None:
        assert self.section.detail is not None
        body: RenderableType
        try:
            with self.app.client() as client:
                body = self.section.detail(client, self.item, self.app.state)
        except Exception as exc:
            body = Text(_explain(exc), style="red")
        self.app.call_from_thread(self._show, body)

    def _show(self, body: RenderableType) -> None:
        self.query_one("#detail-body", Static).update(body)

    def action_scroll_down(self) -> None:
        self.query_one("#detail", VerticalScroll).scroll_down()

    def action_scroll_up(self) -> None:
        self.query_one("#detail", VerticalScroll).scroll_up()


class SimulateModal(ModalScreen[tuple[float | None] | None]):
    """Saisie d'une note simulée ; vide la retire. Renvoie None si annulé."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "dismiss", "Annuler")]

    def __init__(self, course: str, current: float | None) -> None:
        super().__init__()
        self.course = course
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="simulate"):
            yield Label(Text.assemble("Simuler une moyenne en ", (self.course, "bold")))
            yield Label(Text("Note sur 20 ; laisser vide pour retirer la simulation.",
                             style="dim"))  # fmt: skip
            yield Input(
                value="" if self.current is None else f"{self.current:g}",
                placeholder="ex. 14 ou 12,5",
                id="simulate-input",
            )
            yield Static(id="simulate-error")

    @on(Input.Submitted, "#simulate-input")
    def _submitted(self, event: Input.Submitted) -> None:
        try:
            value = sec.parse_grade(event.value)
        except ValueError:
            self.query_one("#simulate-error", Static).update(
                "Note invalide : un nombre entre 0 et 20."
            )
            return
        self.dismiss((value,))


class LoginScreen(Screen[bool]):
    """Connexion depuis le menu (même effet que ``myges login``)."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "app.quit", "Quitter")]

    app: MygesApp

    def __init__(self, message: str = "") -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="login"):
            yield Static("Connexion à myGES", id="login-title")
            yield Static(
                self.message or "Identifiants de myges.fr. Le compte est retenu pour la suite.",
                id="login-message",
            )
            yield Input(value=cli._saved_username() or "", placeholder="Identifiant", id="username")
            yield Input(password=True, placeholder="Mot de passe", id="password")
            yield Checkbox(
                "Retenir le mot de passe (pour myges watch en tâche planifiée)", id="save"
            )
            yield Static(id="login-error")
            yield Button("Se connecter", variant="primary", id="submit")
        yield Footer()

    def on_mount(self) -> None:
        username = self.query_one("#username", Input)
        (self.query_one("#password", Input) if username.value else username).focus()

    @on(Input.Submitted, "#username")
    def _next_field(self) -> None:
        self.query_one("#password", Input).focus()

    @on(Input.Submitted, "#password")
    @on(Button.Pressed, "#submit")
    def _submit(self) -> None:
        username = self.query_one("#username", Input).value.strip()
        password = self.query_one("#password", Input).value
        error = self.query_one("#login-error", Static)
        if not username or not password:
            error.update("Identifiant et mot de passe requis.")
            return
        error.update(Text("Connexion…", style="dim"))
        self.query_one("#submit", Button).disabled = True
        self._login(username, password, self.query_one("#save", Checkbox).value)

    @work(thread=True, exclusive=True)
    def _login(self, username: str, password: str, save: bool) -> None:
        try:
            with MyGES(username, password) as client:
                client.login()
        except InvalidCredentialsError:
            self.app.call_from_thread(self._failed, "Identifiant ou mot de passe incorrect.")
            return
        except Exception as exc:
            self.app.call_from_thread(self._failed, _explain(exc))
            return
        cli._save_username(username, password if save else None)
        self.app.call_from_thread(self._done, password)

    def _failed(self, message: str) -> None:
        self.query_one("#login-error", Static).update(message)
        self.query_one("#submit", Button).disabled = False
        self.query_one("#password", Input).focus()

    def _done(self, password: str) -> None:
        self.app.password = password
        self.dismiss(True)


class MygesApp(App[None]):
    TITLE = "myGES"
    CSS = CSS

    def __init__(self) -> None:
        super().__init__()
        self.state = sec.State()
        self.cache: dict[str, Any] = {}
        # Mot de passe saisi dans le menu : gardé en mémoire le temps de la session.
        self.password: str | None = None
        self.main = MainScreen()

    def on_mount(self) -> None:
        self.register_theme(DARK)
        self.register_theme(LIGHT)
        saved = cli._load_config().get("tui_theme")
        self.theme = saved if isinstance(saved, str) and saved in self.available_themes else "myges"
        sec.ACCENT = self.current_theme.primary
        self.theme_changed_signal.subscribe(self, self._theme_changed)
        self.push_screen(self.main)

    def _theme_changed(self, theme: Theme) -> None:
        sec.ACCENT = theme.primary
        config = cli._load_config()
        if config.get("tui_theme") != theme.name:
            config["tui_theme"] = theme.name
            cli._store_config(config)
        if self.main.is_mounted:
            self.main.rerender()

    def logged_in(self) -> bool:
        return bool(os.environ.get("MYGES_TOKEN") or cli._current_username())

    @contextmanager
    def client(self) -> Iterator[MyGES]:
        with MyGES(
            cli._current_username(),
            self.password or cli._current_password(),
            token=os.environ.get("MYGES_TOKEN"),
        ) as client:
            yield client

    def need_login(self, message: str = "") -> None:
        if any(isinstance(screen, LoginScreen) for screen in self.screen_stack):
            return
        self.push_screen(LoginScreen(message), self._after_login)

    def _after_login(self, ok: bool | None) -> None:
        if ok:
            self.cache.clear()
            self.main.action_reload()
