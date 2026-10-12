from django.db import models
from django.db.models import Q

from apps.academics.models import AcademicSession, ClassLevel, Term
from apps.students.models import Student


class FeeSettings(models.Model):
    """Per-school singleton (pk=1). Use FeeSettings.load()."""

    allow_part_payment = models.BooleanField(default=True)
    min_part_payment_kobo = models.BigIntegerField(default=0)
    manual_approval_threshold_kobo = models.BigIntegerField(default=5_000_000)
    block_results_when_owing = models.BooleanField(default=False)
    bank_transfer_note = models.TextField(
        blank=True, help_text="Shown to students when online payment is unavailable."
    )

    class Meta:
        verbose_name_plural = "fee settings"
        constraints = [
            models.CheckConstraint(
                condition=Q(min_part_payment_kobo__gte=0), name="fees_settings_min_part_gte0"
            ),
            models.CheckConstraint(
                condition=Q(manual_approval_threshold_kobo__gte=0),
                name="fees_settings_threshold_gte0",
            ),
        ]

    def __str__(self) -> str:
        return "Fee settings"

    @classmethod
    def load(cls) -> "FeeSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class FeeItem(models.Model):
    name = models.CharField(max_length=80, unique=True)
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class FeeStructure(models.Model):
    level = models.ForeignKey(ClassLevel, on_delete=models.PROTECT, related_name="fee_structures")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="fee_structures")

    class Meta:
        ordering = ["term__session__start_date", "term__number", "level__order", "level__name"]
        constraints = [
            models.UniqueConstraint(fields=["level", "term"], name="fees_structure_unique")
        ]

    def __str__(self) -> str:
        return f"{self.level} / {self.term}"


class FeeStructureLine(models.Model):
    structure = models.ForeignKey(FeeStructure, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(FeeItem, on_delete=models.PROTECT, related_name="structure_lines")
    amount_kobo = models.BigIntegerField()
    is_optional = models.BooleanField(default=False)

    class Meta:
        ordering = ["item__name"]
        constraints = [
            models.UniqueConstraint(fields=["structure", "item"], name="fees_line_unique"),
            models.CheckConstraint(condition=Q(amount_kobo__gte=0), name="fees_line_amount_gte0"),
        ]

    def __str__(self) -> str:
        return f"{self.item}: {self.amount_kobo}"


class Discount(models.Model):
    PERCENT, FIXED = "percent", "fixed"
    KIND_CHOICES = [(PERCENT, "Percentage"), (FIXED, "Fixed amount")]

    name = models.CharField(max_length=80, unique=True)
    kind = models.CharField(max_length=7, choices=KIND_CHOICES)
    percent = models.PositiveSmallIntegerField(null=True, blank=True)
    amount_kobo = models.BigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        kind="percent",
                        percent__isnull=False,
                        percent__gte=1,
                        percent__lte=100,
                        amount_kobo__isnull=True,
                    )
                    | Q(
                        kind="fixed",
                        amount_kobo__isnull=False,
                        amount_kobo__gte=0,
                        percent__isnull=True,
                    )
                ),
                name="fees_discount_one_value",
            )
        ]

    def __str__(self) -> str:
        return self.name


class StudentDiscount(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="fee_discounts")
    discount = models.ForeignKey(Discount, on_delete=models.PROTECT, related_name="assignments")
    session = models.ForeignKey(
        AcademicSession, on_delete=models.PROTECT, related_name="student_discounts"
    )

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "discount", "session"], name="fees_student_discount_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.student_id}: {self.discount_id} ({self.session_id})"
