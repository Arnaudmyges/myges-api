"""Modèles des ressources renvoyées par l'API.

Ils suivent ``openapi/myges.yaml`` champ pour champ, avec deux conversions :

- les instants en millisecondes epoch deviennent des ``datetime`` UTC (et
  redeviennent des millisecondes avec ``model_dump(mode="json")``) ;
- ``_links`` (HAL) est exposé sous le nom ``hal_links``.

Les champs inconnus sont conservés (``extra="allow"``) et visibles dans
``model.model_extra`` : un ajout côté Kordis ne casse pas le client.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer


def _from_millis(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return value


def _to_millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


Timestamp = Annotated[
    datetime,
    BeforeValidator(_from_millis),
    PlainSerializer(_to_millis, return_type=int, when_used="json"),
]


def _parse_decimal(value: str | float | None) -> float | None:
    """``"3.0"`` -> 3.0 ; ``"N.C."`` (non communiqué) -> None."""
    if value is None or isinstance(value, float):
        return value
    try:
        return float(value)
    except ValueError:
        return None


class KordisModel(BaseModel):
    # hide_input_in_errors : une erreur de validation ne doit jamais recopier les
    # données personnelles reçues (logs, CI publique).
    model_config = ConfigDict(extra="allow", populate_by_name=True, hide_input_in_errors=True)


class Link(KordisModel):
    rel: str
    href: str


class HalLink(KordisModel):
    href: str
    templated: bool | None = None


class _Linked(KordisModel):
    """Élément de collection : liens sous forme de tableau."""

    links: list[Link] = Field(default_factory=list)

    def href(self, rel: str) -> str | None:
        """URL du lien ``rel`` (ex. ``"photo"``), ou None."""
        return next((lk.href for lk in self.links if lk.rel == rel), None)


class _HalLinked(KordisModel):
    """Ressource unitaire : liens HAL."""

    hal_links: dict[str, HalLink] = Field(default_factory=dict, alias="_links")

    def href(self, rel: str) -> str | None:
        """URL du lien ``rel`` (ex. ``"photo"``), ou None."""
        found = self.hal_links.get(rel)
        return found.href if found else None


# ---------------------------------------------------------------- Profil


class EmergencyContact(KordisModel):
    emergency_id: int | None = None
    type: str | None = None
    type_details: str | None = None
    firstname: str | None = None
    name: str | None = None
    telephone: str | None = None
    mobile: str | None = None
    work_phone: str | None = None


class Profile(_HalLinked):
    uid: int
    student_id: str | None = None
    ine: str | None = None
    civility: str | None = None
    firstname: str
    name: str
    maiden_name: str | None = None
    birthday: Timestamp | None = None
    birthplace: str | None = None
    birth_country: str | None = None
    address1: str | None = None
    address2: str | None = None
    city: str | None = None
    zipcode: str | None = None
    country: str | None = None
    telephone: str | None = None
    mobile: str | None = None
    email: str
    personal_mail: str | None = None
    nationality: str | None = None
    mailing: str | None = None
    emergency_contact: EmergencyContact | None = None
    get_need_to_be_removed_from_edusign: bool | None = None

    @property
    def photo_url(self) -> str | None:
        return self.href("photo")


# ---------------------------------------------------------------- Agenda


class Room(_Linked):
    room_id: int | None = None
    name: str | None = None
    floor: str | None = None
    campus: str | None = None
    color: str | None = None
    latitude: str | None = None
    longitude: str | None = None


class Discipline(_Linked):
    rc_id: int | None = None
    name: str | None = None
    teacher: str | None = None
    teacher_id: int | None = None
    trimester: str | None = None
    trimester_id: int | None = None
    year: int | None = None
    school_id: int | None = None
    student_group_id: int | None = None
    student_group_name: str | None = None
    nb_students: int | None = None
    coef: float | str | None = None
    ects: float | str | None = None
    has_documents: bool | None = None
    has_grades: bool | None = None
    syllabus_id: int | None = None


class AgendaEvent(_Linked):
    reservation_id: int
    name: str
    type: str | None = None
    modality: str | None = None
    state: str | None = None
    start_date: Timestamp
    end_date: Timestamp
    teacher: str | None = None
    promotion: str | None = None
    rooms: list[Room] | None = None
    discipline: Discipline | None = None
    author: int | None = None
    create_date: Timestamp | None = None
    comment: str | None = None
    classes: list[Any] | None = None
    prestation_type: int | None = None
    is_electronic_signature: bool | None = None

    @property
    def location(self) -> str:
        """Salles sous forme lisible, ex. ``"E31 (NATION1)"``."""
        return ", ".join(
            f"{room.name} ({room.campus})" if room.campus else room.name or ""
            for room in self.rooms or []
        )


# ---------------------------------------------------------------- Scolarité


class Course(_Linked):
    rc_id: int
    name: str
    teacher: str | None = None
    teacher_id: int | None = None
    coef: float | None = None
    ects: float | None = None
    trimester: str | None = None
    trimester_id: int | None = None
    year: int | None = None
    school_id: int | None = None
    student_group_id: int | None = None
    student_group_name: str | None = None
    nb_students: int | None = None
    has_documents: bool | None = None
    has_grades: bool | None = None
    syllabus_id: int | None = None


class CourseDetail(_HalLinked):
    rc_id: int
    name: str
    promotion: str | None = None
    promotion_level: str | None = None
    promotion_type: str | None = None
    teacher: str | None = None
    teacher_id: int | None = None
    nb_students: int | None = None
    trimester: str | None = None
    year: int | None = None
    student_group_id: int | None = None


class Grade(_Linked):
    rc_id: int
    course: str
    code: str | None = None
    grades: list[float] = Field(default_factory=list)
    ccaverage: float | None = None
    exam: float | None = None
    average: float | None = None
    bonus: float | None = None
    letter_mark: str | None = None
    coef: str | None = None
    ects: str | None = None
    trimester: int | None = None
    trimester_name: str | None = None
    year: int | None = None
    teacher_civility: str | None = None
    teacher_first_name: str | None = None
    teacher_last_name: str | None = None
    absences: int | None = None
    lates: int | None = None

    @property
    def coef_value(self) -> float | None:
        return _parse_decimal(self.coef)

    @property
    def ects_value(self) -> float | None:
        return _parse_decimal(self.ects)


class Absence(_Linked):
    date: Timestamp
    course_name: str
    justified: bool
    type: str | None = None
    trimester: int | None = None
    trimester_name: str | None = None
    year: int | None = None


class CourseFile(_Linked):
    oc_id: int
    label: str
    extension: str | None = None
    type: str | None = None
    category: str | None = None
    file: None = None
    teacher_name: str | None = None
    update_date: Timestamp | None = None

    @property
    def filename(self) -> str:
        """Nom de fichier complet, ex. ``"Chapitre 1.pdf"``."""
        ext = self.extension or ""
        return self.label if self.label.endswith(ext) else f"{self.label}{ext}"

    @property
    def web_url(self) -> str | None:
        """Lien ``ges-dl`` : exige une session web CAS, pas le token de l'API."""
        return self.href("url")


class SyllabusControlType(_Linked):
    evaluation_type_id: int | None = None
    evaluation_label: str | None = None


class SyllabusSession(_Linked):
    cont_id: int | None = None
    syl_id: int | None = None
    number: int | None = None
    content: str | None = None
    homework: str | None = None
    ref: str | None = None
    eval: str | None = None


class SyllabusSkill(_Linked):
    comp_id: int | None = None
    syl_id: int | None = None
    comp_label: str | None = None
    ti_label: str | None = None


class Syllabus(_Linked):
    syllabus_id: int
    rc_id: int
    cr_id: int | None = None
    promo_id: int | None = None
    school_id: int | None = None
    year: int | None = None
    nextyear: int | None = None
    code: str | None = None
    syllabus_name: str | None = None
    course_name: str | None = None
    formation: str | None = None
    tri_name: str | None = None
    tri_describe: str | None = None
    coef: float | None = None
    ects: float | None = None
    hours_per_week: int | None = None
    personal_work: int | None = None
    exam_duration: int | None = None
    teacher_id: int | None = None
    teacher_firstname: str | None = None
    teacher_lastname: str | None = None
    resp_peda_firstname: str | None = None
    resp_peda_lastname: str | None = None
    resp_email: str | None = None
    asso_firstname: str | None = None
    asso_lastname: str | None = None
    teaching_goals: str | None = None
    teaching_method: str | None = None
    prerequisite: str | None = None
    detail_plan: str | None = None
    computing_tools: str | None = None
    evaluation_type: str | None = None
    evaluation_criteria: str | None = None
    allowed_docs: bool | None = None
    validation: bool | None = None
    books_reference: str | None = None
    other_reference: str | None = None
    cyberlibris_reference: str | None = None
    crossknowledge_reference: str | None = None
    create_date: Timestamp | None = None
    control_types: list[SyllabusControlType] = Field(default_factory=list)
    seance_details: list[SyllabusSession] | None = None
    skills: list[SyllabusSkill] | None = None


# ---------------------------------------------------------------- Personnes


class Person(_Linked):
    uid: int
    firstname: str
    lastname: str
    email: str | None = None
    civility: str | None = None
    profile_type: str | None = None

    @property
    def photo_url(self) -> str | None:
        return self.href("photo")


class PersonDetail(_HalLinked):
    uid: int
    firstname: str
    lastname: str
    email: str | None = None
    civility: str | None = None
    profile_type: str | None = None

    @property
    def photo_url(self) -> str | None:
        return self.href("photo")


class SchoolClass(_Linked):
    puid: int
    name: str
    description: str | None = None
    promotion: str | None = None
    school: str | None = None
    year: int | None = None
    trimester: int | None = None


class SchoolClassDetail(_HalLinked):
    puid: int
    name: str
    description: str | None = None
    promotion: str | None = None
    school: str | None = None


# ---------------------------------------------------------------- Projets


class ProjectFile(_Linked):
    pf_id: int | None = None
    pro_id: int | None = None
    pf_title: str | None = None
    pf_file: str | None = None
    pf_crea_date: Timestamp | None = None

    @property
    def filename(self) -> str:
        """Nom lisible : intitulé + extension du fichier stocké (« Cahier des charges.pdf »)."""
        stored = self.pf_file or ""
        ext = stored[stored.rfind(".") :] if "." in stored else ""
        title = (self.pf_title or stored or f"fichier-{self.pf_id}").strip()
        return title if not ext or title.lower().endswith(ext.lower()) else f"{title}{ext}"


class ProjectGroupStudent(_Linked):
    u_id: int | None = None
    firstname: str | None = None
    name: str | None = None
    promotion: str | None = None
    option: str | None = None
    classe: str | None = None


class ProjectGroup(_Linked):
    project_group_id: int
    project_id: int | None = None
    subject_id: int | None = None
    group_name: str | None = None
    subject_validated: bool | None = None
    date_presentation: Timestamp | None = None
    teacher_comment: str | None = None
    teacher_intern_comment: str | None = None
    project_group_students: list[ProjectGroupStudent] | None = None


class ProjectStepFile(_Linked):
    psf_id: int | None = None
    psp_id: int | None = None
    pgr_id: int | None = None
    u_id: int | None = None
    psf_name: str | None = None
    psf_desc: str | None = None
    psf_file: str | None = None
    psf_file_type: str | None = None
    psf_file_size: int | None = None
    psf_file_hash: str | None = None
    psf_role_user: str | None = None
    psf_begin_upload: Timestamp | None = None
    psf_end_upload: Timestamp | None = None


class ProjectStep(_Linked):
    psp_id: int | None = None
    pro_id: int | None = None
    psp_number: int | None = None
    psp_type: str | None = None
    psp_desc: str | None = None
    psp_limit_date: Timestamp | None = None
    files: list[ProjectStepFile] | None = None


class ProjectGroupLog(_Linked):
    pgl_id: int | None = None
    pgr_id: int | None = None
    user_id: int | None = None
    pgl_author: str | None = None
    pgl_role_user: str | None = None
    pgl_describe: str | None = None
    pgl_date: Timestamp | None = None
    pgl_type_action: str | None = None


class ProjectBase(_Linked, _HalLinked):
    """Champs communs à l'élément de liste (``links``) et au détail (``_links``)."""

    project_id: int
    name: str
    course_name: str | None = None
    rc_id: int | None = None
    discipline_id: int | None = None
    teacher_id: int | None = None
    author: str | None = None
    year: int | None = None
    trimester_id: int | None = None
    is_draft: bool | None = None
    update_date: Timestamp | None = None
    update_user: str | None = None
    project_create_date: Timestamp | None = None
    project_type_id: int | None = None
    project_type_group: str | None = None
    project_type_subject: str | None = None
    project_type_presentation: str | None = None
    project_type_presentation_details: str | None = None
    project_min_student_group: int | None = None
    project_max_student_group: int | None = None
    project_personal_work: int | None = None
    project_presentation_duration: int | None = None
    project_teaching_goals: str | None = None
    project_detail_plan: str | None = None
    project_computing_tools: str | None = None
    project_ref_books: str | None = None
    project_hearing_presentation: str | None = None
    project_files: list[ProjectFile] | None = None

    def href(self, rel: str) -> str | None:
        return _Linked.href(self, rel) or _HalLinked.href(self, rel)


class Project(ProjectBase):
    groups: list[ProjectGroup] = Field(default_factory=list)
    steps: list[ProjectStep] | None = None
    project_group_logs: list[ProjectGroupLog] | None = None


class NextProjectStep(_Linked):
    pro_id: int | None = None
    pro_name: str | None = None
    course_name: str | None = None
    group_id: int | None = None
    psp_id: int | None = None
    psp_number: int | None = None
    psp_type: str | None = None
    psp_desc: str | None = None
    psp_limit_date: Timestamp | None = None
    type: str | None = None


# ---------------------------------------------------------------- Vie de campus


class News(_Linked):
    ne_id: int
    title: str
    author: str | None = None
    summary: str | None = None
    text: str | None = None
    html: str | None = None
    date: Timestamp | None = None
    update_date: Timestamp | None = None


class NewsDetail(_HalLinked):
    ne_id: int
    title: str
    author: str | None = None
    summary: str | None = None
    text: str | None = None
    html: str | None = None
    date: Timestamp | None = None
    update_date: Timestamp | None = None


class Banner(_Linked):
    ba_id: int
    title: str
    description: str | None = None
    author: str | None = None
    html: str | None = None
    image: str | None = None
    url: str | None = None
    display_order: int | None = None
    begin_date: Timestamp | None = None
    end_date: Timestamp | None = None


class CampusEvent(_Linked):
    event_id: int
    event_title: str
    event_type: str | None = None
    event_date: Timestamp | None = None
    start_subscription_date: Timestamp | None = None
    end_subscription_date: Timestamp | None = None
    location: str | None = None
    organizer: str | None = None
    description: str | None = None
    is_participant_subscribed: bool | None = None


class School(_Linked):
    puid: int
    name: str
    description: str | None = None


class Campus(_Linked):
    campus_id: int
    name: str
    address: str | None = None
    zipcode: str | None = None
    city: str | None = None
    city_level: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    google_map_url: str | None = None
    color: str | None = None
    available: bool | None = None
    rooms: list[Any] | None = None


class Partner(_Linked):
    partner_id: int
    name: str
    link: str | None = None
    content: str | None = None
    small_image_url: str | None = None
    medium_image_url: str | None = None


# ---------------------------------------------------------------- Pagination

T = TypeVar("T", bound=KordisModel)


class Sort(KordisModel):
    sorted: bool | None = None
    unsorted: bool | None = None
    empty: bool | None = None


class Pageable(KordisModel):
    sort: Sort | None = None
    page_size: int | None = None
    page_number: int | None = None
    offset: int | None = None
    paged: bool | None = None
    unpaged: bool | None = None


class Page(KordisModel, Generic[T]):
    """Page Spring Data. ``number`` est l'index de page (à partir de 0)."""

    content: list[T]
    number: int
    size: int
    total_pages: int
    total_elements: int
    first: bool | None = None
    last: bool | None = None
    empty: bool | None = None
    number_of_elements: int | None = None
    pageable: Pageable | None = None
    sort: Sort | None = None
