from django.conf import settings
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


class Invoice(models.Model):
    """One per (student, term). Totals are integer kobo; ``total_kobo`` is the sum of lines."""

    UNPAID, PART_PAID, PAID, CARRIED = "unpaid", "part_paid", "paid", "carried"
    STATUS_CHOICES = [
        (UNPAID, "Unpaid"),
        (PART_PAID, "Part paid"),
        (PAID, "Paid"),
        (CARRIED, "Carried forward"),
    ]

    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="invoices")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="invoices")
    total_kobo = models.BigIntegerField(default=0)
    paid_kobo = models.BigIntegerField(default=0)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=UNPAID)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["term__session__start_date", "term__number", "pk"]
        indexes = [models.Index(fields=["term", "status"], name="fees_invoice_term_status")]
        constraints = [
            models.UniqueConstraint(fields=["student", "term"], name="fees_invoice_unique"),
            models.CheckConstraint(condition=Q(total_kobo__gte=0), name="fees_invoice_total_gte0"),
            models.CheckConstraint(condition=Q(paid_kobo__gte=0), name="fees_invoice_paid_gte0"),
            models.CheckConstraint(
                condition=Q(paid_kobo__lte=models.F("total_kobo")),
                name="fees_invoice_paid_lte_total",
            ),
        ]

    def __str__(self) -> str:
        return f"Invoice {self.pk} ({self.term_id})"

    @property
    def balance_kobo(self) -> int:
        return self.total_kobo - self.paid_kobo


class InvoiceLine(models.Model):
    FEE, OPTIONAL, DISCOUNT, BROUGHT_FORWARD, ADJUSTMENT = (
        "fee",
        "optional",
        "discount",
        "brought_forward",
        "adjustment",
    )
    KIND_CHOICES = [
        (FEE, "Fee"),
        (OPTIONAL, "Optional item"),
        (DISCOUNT, "Discount"),
        (BROUGHT_FORWARD, "Balance brought forward"),
        (ADJUSTMENT, "Adjustment"),
    ]

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    description = models.CharField(max_length=200)
    item = models.ForeignKey(
        FeeItem, on_delete=models.PROTECT, null=True, blank=True, related_name="invoice_lines"
    )
    amount_kobo = models.BigIntegerField()  # negative for discounts and credits
    reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(kind="adjustment", reason=""),
                name="fees_line_adjustment_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind}: {self.amount_kobo}"


class Payment(models.Model):
    """A confirmed payment. Created only by ``ledger.confirm_payment``."""

    ONLINE, CASH, TELLER, POS, TRANSFER = "online", "cash", "teller", "pos", "transfer"
    METHOD_CHOICES = [
        (ONLINE, "Online"),
        (CASH, "Cash"),
        (TELLER, "Teller"),
        (POS, "POS"),
        (TRANSFER, "Transfer"),
    ]

    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="payments")
    amount_kobo = models.BigIntegerField()
    method = models.CharField(max_length=8, choices=METHOD_CHOICES)
    gateway_reference = models.CharField(max_length=64, unique=True, null=True, blank=True)
    paid_at = models.DateTimeField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    unallocated_kobo = models.BigIntegerField(default=0)
    flagged = models.BooleanField(default=False)
    flag_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(amount_kobo__gt=0), name="fees_payment_amount_gt0"),
            models.CheckConstraint(
                condition=Q(unallocated_kobo__gte=0)
                & Q(unallocated_kobo__lte=models.F("amount_kobo")),
                name="fees_payment_unallocated_range",
            ),
        ]

    def __str__(self) -> str:
        return f"Payment {self.pk}: {self.amount_kobo}"


class Allocation(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="allocations")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="allocations")
    amount_kobo = models.BigIntegerField()

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "invoice"], name="fees_allocation_unique"),
            models.CheckConstraint(
                condition=Q(amount_kobo__gt=0), name="fees_allocation_amount_gt0"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.payment_id} -> {self.invoice_id}: {self.amount_kobo}"


class Receipt(models.Model):
    payment = models.OneToOneField(Payment, on_delete=models.PROTECT, related_name="receipt")
    number = models.CharField(max_length=40, unique=True)
    year = models.PositiveIntegerField()
    pdf_name = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pk"]

    def __str__(self) -> str:
        return self.number


class ReceiptCounter(models.Model):
    year = models.PositiveIntegerField(unique=True)
    last = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.year}: {self.last}"
