from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.academics.models import SECTION_CHOICES


class AssessmentComponent(models.Model):
    section = models.CharField(max_length=10, choices=SECTION_CHOICES)
    name = models.CharField(max_length=40)
    max_score = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(100)]
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["section", "order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["section", "name"], name="results_component_unique_name"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.max_score})"


class GradeBand(models.Model):
    section = models.CharField(max_length=10, choices=SECTION_CHOICES)
    lower = models.PositiveSmallIntegerField()
    upper = models.PositiveSmallIntegerField()
    grade = models.CharField(max_length=4)
    remark = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["section", "-lower"]
        constraints = [
            models.UniqueConstraint(fields=["section", "grade"], name="results_band_unique_grade"),
        ]

    def __str__(self) -> str:
        return f"{self.grade} {self.lower}-{self.upper}"


def components_for(section: str) -> list[AssessmentComponent]:
    return list(AssessmentComponent.objects.filter(section=section))


def components_complete(section: str) -> bool:
    total = AssessmentComponent.objects.filter(section=section).aggregate(t=models.Sum("max_score"))
    return (total["t"] or 0) == 100


def _as_tuple(band) -> tuple[int, int, str, str]:
    if isinstance(band, tuple | list):
        lower, upper, grade, *rest = band
        return int(lower), int(upper), str(grade), str(rest[0]) if rest else ""
    return int(band.lower), int(band.upper), str(band.grade), str(band.remark)


def validate_scale(bands) -> None:
    """Bands must be contiguous whole-number ranges covering 0-100 with unique short grades."""
    rows = sorted((_as_tuple(b) for b in bands), key=lambda r: r[0])
    if not rows:
        raise ValidationError("Add at least one grade band.")
    seen: set[str] = set()
    for lower, upper, grade, _remark in rows:
        label = grade.strip()
        if not label or len(label) > 4:
            raise ValidationError("Each grade needs a label of 1 to 4 characters.")
        if label.lower() in seen:
            raise ValidationError(f"Grade {label} is used more than once.")
        seen.add(label.lower())
        if lower > upper:
            raise ValidationError(f"Band {label}: the lower mark is above the upper mark.")
    if rows[0][0] != 0:
        raise ValidationError("The scale must start at 0.")
    if rows[-1][1] != 100:
        raise ValidationError("The scale must end at 100.")
    for prev, cur in zip(rows, rows[1:], strict=False):
        if cur[0] < prev[1] + 1:
            raise ValidationError(f"Bands {prev[2]} and {cur[2]} overlap.")
        if cur[0] > prev[1] + 1:
            raise ValidationError(f"There is a gap between {prev[2]} and {cur[2]}.")


def grade_for(section: str, total: Decimal | float | int | str) -> GradeBand:
    rounded = int(Decimal(str(total)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    band = GradeBand.objects.filter(section=section, lower__lte=rounded, upper__gte=rounded).first()
    if band is None:
        raise LookupError(f"No grade band for {rounded} in {section}; the scale is broken.")
    return band
