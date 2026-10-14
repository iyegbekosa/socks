from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models import Q

from apps.academics.models import ClassArm
from apps.students.models import Student

DEFAULT_LATE_THRESHOLD = 3


class AttendanceSettings(models.Model):
    """Per-school singleton (pk=1). Use AttendanceSettings.load()."""

    late_threshold = models.PositiveSmallIntegerField(
        default=DEFAULT_LATE_THRESHOLD,
        validators=[MaxValueValidator(60)],
        help_text="Show 'Late n times' on the report card at this many lates. 0 means never.",
    )
    edit_window_days = models.PositiveSmallIntegerField(
        default=7,
        validators=[MaxValueValidator(60)],
        help_text="Days after a register's date during which a form teacher may still edit it.",
    )
    absence_email = models.BooleanField(
        default=True, help_text="Email guardians when a student is marked absent."
    )

    class Meta:
        verbose_name_plural = "attendance settings"
        constraints = [
            models.CheckConstraint(
                condition=Q(late_threshold__lte=60), name="attendance_settings_late_lte60"
            ),
            models.CheckConstraint(
                condition=Q(edit_window_days__lte=60), name="attendance_settings_window_lte60"
            ),
        ]

    def __str__(self) -> str:
        return "Attendance settings"

    @classmethod
    def load(cls) -> "AttendanceSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def current_late_threshold(cls) -> int:
        """The live late threshold, without creating the settings row."""
        value = cls.objects.filter(pk=1).values_list("late_threshold", flat=True).first()
        return DEFAULT_LATE_THRESHOLD if value is None else int(value)


class Holiday(models.Model):
    date = models.DateField(unique=True)
    name = models.CharField(max_length=80)

    class Meta:
        ordering = ["date"]

    def __str__(self) -> str:
        return f"{self.date} {self.name}"


class AttendanceRecord(models.Model):
    class Status(models.TextChoices):
        PRESENT = "present", "Present"
        ABSENT = "absent", "Absent"
        LATE = "late", "Late"
        EXCUSED = "excused", "Excused"

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="attendance")
    date = models.DateField()
    arm = models.ForeignKey(ClassArm, on_delete=models.PROTECT, related_name="attendance")
    status = models.CharField(max_length=10, choices=Status.choices)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    absence_emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["date", "student_id"]
        constraints = [
            models.UniqueConstraint(fields=["student", "date"], name="attendance_student_date_uniq")
        ]
        indexes = [
            models.Index(fields=["arm", "date"], name="attendance_arm_date_idx"),
            models.Index(fields=["student", "date"], name="attendance_student_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.student_id} {self.date} {self.status}"
