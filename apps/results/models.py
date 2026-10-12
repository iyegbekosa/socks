from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.academics.models import SECTION_CHOICES, ClassArm, SubjectOffering, Term
from apps.students.models import Enrolment, Student


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


class ElectiveChoice(models.Model):
    """A student's choice to take a non-compulsory subject offering."""

    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="elective_choices")
    offering = models.ForeignKey(
        SubjectOffering, on_delete=models.PROTECT, related_name="elective_choices"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "offering"], name="results_electivechoice_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.student_id} / {self.offering_id}"

    def clean(self) -> None:
        if self.offering_id is None or self.student_id is None:
            return
        if self.offering.is_compulsory:
            raise ValidationError("This subject is compulsory, so it has no elective choices.")
        if not Enrolment.objects.filter(
            student_id=self.student_id,
            arm_id=self.offering.arm_id,
            session_id=self.offering.session_id,
            status=Enrolment.Status.ACTIVE,
        ).exists():
            raise ValidationError("The student is not in this class for this session.")


class Score(models.Model):
    """One cell of a score sheet. ``value`` null means "not entered"; ``version`` starts at 0
    for a cell that has never been saved and goes up by one on every change."""

    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="scores")
    offering = models.ForeignKey(SubjectOffering, on_delete=models.PROTECT, related_name="scores")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="scores")
    component = models.ForeignKey(
        AssessmentComponent, on_delete=models.PROTECT, related_name="scores"
    )
    value = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    version = models.PositiveIntegerField(default=0)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "offering", "term", "component"], name="results_score_unique"
            ),
        ]
        indexes = [models.Index(fields=["offering", "term"], name="results_score_sheet_idx")]

    def __str__(self) -> str:
        return f"score {self.pk}"

    def __repr__(self) -> str:
        return f"<Score {self.pk}>"


class ScoreSheet(models.Model):
    """Workflow state of one subject's scores for a term."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"

    offering = models.ForeignKey(SubjectOffering, on_delete=models.PROTECT, related_name="sheets")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="score_sheets")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    first_submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["offering", "term"], name="results_scoresheet_unique"),
        ]

    def __str__(self) -> str:
        return f"sheet {self.offering_id}/{self.term_id} ({self.status})"


class ClassResult(models.Model):
    """Workflow state of one class arm's results for a term."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        REVIEWED = "reviewed", "Reviewed"
        APPROVED = "approved", "Approved"
        PUBLISHED = "published", "Published"

    arm = models.ForeignKey(ClassArm, on_delete=models.PROTECT, related_name="class_results")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="class_results")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["arm", "term"], name="results_classresult_unique"),
        ]

    def __str__(self) -> str:
        return f"class {self.arm_id}/{self.term_id} ({self.status})"


class Trait(models.Model):
    """A behavioural (affective) or skill (psychomotor) trait rated 1-5 on the report card."""

    class Kind(models.TextChoices):
        AFFECTIVE = "affective", "Affective"
        PSYCHOMOTOR = "psychomotor", "Psychomotor"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    name = models.CharField(max_length=60)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["kind", "order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "name"], name="results_trait_unique_name"),
        ]

    def __str__(self) -> str:
        return self.name


class ReportComment(models.Model):
    """Comments and attendance totals for one student and term."""

    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="report_comments")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="report_comments")
    class_teacher_comment = models.CharField(max_length=500, blank=True, default="")
    principal_comment = models.CharField(max_length=500, blank=True, default="")
    days_present = models.PositiveSmallIntegerField(null=True, blank=True)
    days_open = models.PositiveSmallIntegerField(null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "term"], name="results_reportcomment_unique"
            ),
            models.CheckConstraint(
                condition=(models.Q(days_open__isnull=True) | models.Q(days_open__lte=200)),
                name="results_reportcomment_open_max",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(days_present__isnull=True)
                    | models.Q(days_open__isnull=True)
                    | models.Q(days_present__lte=models.F("days_open"))
                ),
                name="results_reportcomment_present_le_open",
            ),
        ]

    def __str__(self) -> str:
        return f"comment {self.student_id}/{self.term_id}"

    def __repr__(self) -> str:
        return f"<ReportComment {self.pk}>"


class Rating(models.Model):
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="ratings")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="ratings")
    trait = models.ForeignKey(Trait, on_delete=models.PROTECT, related_name="ratings")
    value = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "term", "trait"], name="results_rating_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(value__gte=1, value__lte=5), name="results_rating_range"
            ),
        ]

    def __str__(self) -> str:
        return f"rating {self.pk}"


class SubjectResult(models.Model):
    """One student's computed result in one subject for a term.

    ``subject_name``, ``teacher_name`` and ``components`` (``[{"name", "max", "score"}]``) are
    snapshots, so a past term renders with its own structure after the setup changes.
    """

    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="subject_results")
    offering = models.ForeignKey(
        SubjectOffering, on_delete=models.PROTECT, related_name="subject_results"
    )
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="subject_results")
    subject_name = models.CharField(max_length=80)
    teacher_name = models.CharField(max_length=310, blank=True)
    components = models.JSONField(default=list)
    total = models.DecimalField(max_digits=4, decimal_places=1)
    grade = models.CharField(max_length=4)
    remark = models.CharField(max_length=40, blank=True)
    position = models.PositiveIntegerField()
    highest = models.DecimalField(max_digits=4, decimal_places=1)
    lowest = models.DecimalField(max_digits=4, decimal_places=1)
    class_average = models.DecimalField(max_digits=5, decimal_places=2)
    cumulative = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "offering", "term"], name="results_subjectresult_unique"
            ),
        ]
        indexes = [models.Index(fields=["term", "offering"], name="results_subres_term_idx")]

    def __str__(self) -> str:
        return f"subject result {self.pk}"

    def __repr__(self) -> str:
        return f"<SubjectResult {self.pk}>"


class ResultSummary(models.Model):
    """One student's overall computed result for a term."""

    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="result_summaries")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="result_summaries")
    arm = models.ForeignKey(ClassArm, on_delete=models.PROTECT, related_name="result_summaries")
    subjects_count = models.PositiveSmallIntegerField()
    total = models.DecimalField(max_digits=7, decimal_places=1)
    average = models.DecimalField(max_digits=5, decimal_places=2)
    position = models.PositiveIntegerField()
    out_of = models.PositiveIntegerField()
    cumulative_average = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    computed_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "term"], name="results_resultsummary_unique"
            ),
        ]
        indexes = [models.Index(fields=["term", "arm"], name="results_summary_term_arm_idx")]

    def __str__(self) -> str:
        return f"result summary {self.pk}"

    def __repr__(self) -> str:
        return f"<ResultSummary {self.pk}>"
