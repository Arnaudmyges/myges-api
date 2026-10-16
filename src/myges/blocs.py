"""Moyennes par bloc de compétences, avec simulation de notes.

myGES n'expose pas les blocs : l'étudiant les définit lui-même (``myges blocs add``), en
regroupant des matières. Ce module calcule, à partir des notes, la moyenne pondérée de
chaque bloc et la moyenne générale, en tenant compte de notes hypothétiques.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .models import Course, Grade

PASSING = 10.0


@dataclass(frozen=True)
class CourseLine:
    rc_id: int
    name: str
    weight: float
    average: float | None
    source: str  # officielle | provisoire | simulée | aucune
    official_weight: bool  # False : coefficient non publié, pondération 1


@dataclass
class BlocResult:
    name: str
    lines: list[CourseLine] = field(default_factory=list)

    @property
    def average(self) -> float | None:
        return weighted_average(self.lines)

    @property
    def validated(self) -> bool | None:
        avg = self.average
        return None if avg is None else avg >= PASSING


def weighted_average(lines: Sequence[CourseLine]) -> float | None:
    counted = [line for line in lines if line.average is not None]
    total = sum(line.weight for line in counted)
    if not counted or total <= 0:
        return None
    return sum(line.weight * (line.average or 0.0) for line in counted) / total


def course_line(course: Course, grade: Grade | None, simulated: float | None) -> CourseLine:
    weight = course.coef or (grade.coef_value if grade else None) or 0.0
    official_weight = weight > 0
    average: float | None
    if simulated is not None:
        average, source = simulated, "simulée"
    elif grade is not None and grade.average is not None:
        average, source = grade.average, "officielle"
    elif grade is not None and grade.grades:
        average, source = grade.ccaverage, "provisoire"
    else:
        average, source = None, "aucune"
    return CourseLine(
        rc_id=course.rc_id,
        name=course.name,
        weight=weight if official_weight else 1.0,
        average=average,
        source=source,
        official_weight=official_weight,
    )


def compute(
    courses: Sequence[Course],
    grades: Sequence[Grade],
    blocs: Mapping[str, Sequence[int]],
    simulated: Mapping[int, float] | None = None,
) -> tuple[list[BlocResult], BlocResult]:
    """(un résultat par bloc dans l'ordre de définition, matières hors bloc)."""
    simulated = simulated or {}
    by_id = {c.rc_id: c for c in courses}
    grade_by_id = {g.rc_id: g for g in grades}
    lines = {rc: course_line(c, grade_by_id.get(rc), simulated.get(rc)) for rc, c in by_id.items()}
    results = [
        BlocResult(name, [lines[rc] for rc in members if rc in lines])
        for name, members in blocs.items()
    ]
    assigned = {rc for members in blocs.values() for rc in members}
    others = BlocResult("Hors bloc", [line for rc, line in lines.items() if rc not in assigned])
    return results, others


def overall(results: Sequence[BlocResult], others: BlocResult) -> float | None:
    """Moyenne générale pondérée de toutes les matières (chacune comptée une fois)."""
    seen: dict[int, CourseLine] = {}
    for result in [*results, others]:
        for line in result.lines:
            seen.setdefault(line.rc_id, line)
    return weighted_average(list(seen.values()))
