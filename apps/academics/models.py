from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

PREFIX_RE = r"^[A-Z]{1,8}$"
DEFAULT_ADMISSION_PATTERN = "{PREFIX}/{YEAR}/{SEQ:4}"


class AcademicSession(models.Model):
    name = models.CharField(max_length=9, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ["-start_date"]

    def __str__(self) -> str:
        return self.name


class Term(models.Model):
    FIRST, SECOND, THIRD = 1, 2, 3
    NUMBER_CHOICES = [(FIRST, "1st Term"), (SECOND, "2nd Term"), (THIRD, "3rd Term")]

    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE, related_name="terms")
    number = models.PositiveSmallIntegerField(choices=NUMBER_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ["session__start_date", "number"]
        constraints = [
            models.UniqueConstraint(fields=["session", "number"], name="academics_term_unique"),
            models.UniqueConstraint(
                fields=["is_current"],
                condition=Q(is_current=True),
                name="academics_one_current_term",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.session.name} {self.get_number_display()}"


SECTIONS = ["nursery", "primary", "jss", "sss"]
SECTION_CHOICES = [
    ("nursery", "Nursery"),
    ("primary", "Primary"),
    ("jss", "Junior Secondary"),
    ("sss", "Senior Secondary"),
]
STREAM_CHOICES = [("science", "Science"), ("arts", "Arts"), ("commercial", "Commercial")]


def normalise_display(text: str) -> str:
    return "".join((text or "").split()).upper()


class ClassLevel(models.Model):
    name = models.CharField(max_length=40, unique=True)
    section = models.CharField(max_length=10, choices=SECTION_CHOICES)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return self.name


class ClassArm(models.Model):
    level = models.ForeignKey(ClassLevel, on_delete=models.PROTECT, related_name="arms")
    suffix = models.CharField(max_length=40)
    stream = models.CharField(max_length=12, choices=STREAM_CHOICES, null=True, blank=True)  # noqa: DJ001 - None means no stream

    class Meta:
        ordering = ["level__order", "level__name", "suffix"]
        constraints = [
            models.UniqueConstraint(fields=["level", "suffix"], name="academics_arm_unique"),
        ]

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        sep = "" if len(self.suffix) == 1 else " "
        return f"{self.level.name}{sep}{self.suffix}"

    @property
    def normalised_display(self) -> str:
        return normalise_display(self.level.name + self.suffix)

    def clean(self) -> None:
        errors: dict[str, str] = {}
        self.suffix = (self.suffix or "").strip()
        if not self.suffix:
            errors["suffix"] = "Give the arm a name or letter, such as A or Gold."
        if self.stream and self.level_id and self.level.section != "sss":
            errors["stream"] = "Only senior secondary (SSS) arms can have a stream."
        if self.suffix and self.level_id:
            key = normalise_display(self.level.name + self.suffix)
            others = ClassArm.objects.select_related("level").exclude(pk=self.pk)
            for other in others:
                if other.normalised_display == key:
                    errors["suffix"] = f"That would look the same as {other.display_name}."
                    break
        if errors:
            raise ValidationError(errors)

    @classmethod
    def find_by_display(cls, text: str) -> "ClassArm | None":
        key = normalise_display(text)
        if not key:
            return None
        for arm in cls.objects.select_related("level"):
            if arm.normalised_display == key:
                return arm
        return None


class Subject(models.Model):
    name = models.CharField(max_length=80, unique=True)
    code = models.SlugField(max_length=90, unique=True)
    sections = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class SubjectOffering(models.Model):
    """A subject taught in an arm for a session; teacher is null until assigned."""

    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="offerings")
    arm = models.ForeignKey(ClassArm, on_delete=models.PROTECT, related_name="offerings")
    session = models.ForeignKey(AcademicSession, on_delete=models.PROTECT, related_name="offerings")
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="subject_offerings",
    )
    is_compulsory = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "arm", "session"], name="academics_offering_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.subject} / {self.arm} / {self.session}"


class FormTeacher(models.Model):
    arm = models.ForeignKey(ClassArm, on_delete=models.PROTECT, related_name="form_teachers")
    session = models.ForeignKey(
        AcademicSession, on_delete=models.PROTECT, related_name="form_teachers"
    )
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="form_teacher_of"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["arm", "session"], name="academics_formteacher_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.arm} / {self.session}: {self.teacher}"


class AcademicSettings(models.Model):
    """Per-school singleton (pk=1). Use services.get_academic_settings()."""

    admission_prefix = models.CharField(
        max_length=8,
        blank=True,
        validators=[RegexValidator(PREFIX_RE, "Use 1 to 8 capital letters, A to Z only.")],
    )
    admission_pattern = models.CharField(max_length=60, default=DEFAULT_ADMISSION_PATTERN)

    class Meta:
        verbose_name_plural = "academic settings"

    def __str__(self) -> str:
        return f"Academic settings ({self.admission_prefix})"


class AdmissionCounter(models.Model):
    year = models.PositiveIntegerField(unique=True)
    last = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.year}: {self.last}"
