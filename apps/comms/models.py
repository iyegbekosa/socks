"""Messages to families (P6): one ``Message`` per announcement, reminder or automatic notice,
and one ``Delivery`` per recipient and channel with its status."""

from datetime import time

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.academics.models import Term
from apps.students.models import Student

DEFAULT_REMINDER_TEMPLATE = (
    "Dear parent, {student_first_name}'s school fees balance for {term} is {balance}. "
    "Please pay through the student portal or at the school bursary. Thank you."
)
SUBJECT_MAX = 120


class Message(models.Model):
    class Kind(models.TextChoices):
        ANNOUNCEMENT = "announcement", "Announcement"
        FEE_REMINDER = "fee_reminder", "Fee reminder"
        RESULTS_PUBLISHED = "results_published", "Results published"
        RECEIPT = "receipt", "Receipt"
        ABSENCE = "absence", "Absence"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    subject = models.CharField(max_length=SUBJECT_MAX)
    body = models.TextField()
    # A short description such as "JSS 1A"; never names.
    audience_label = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)
    related_type = models.CharField(max_length=60, blank=True)
    related_id = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["kind", "created_at"], name="comms_msg_kind_created")]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.pk}"


class Delivery(models.Model):
    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        INAPP = "inapp", "In-app"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"  # claimed by a batch task (Ruling AC)
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="deliveries")
    channel = models.CharField(max_length=5, choices=Channel.choices)
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="deliveries")
    email = models.EmailField(blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="deliveries",
    )
    # The rendered text for this recipient (per-student bodies differ).
    body = models.TextField(blank=True)
    status = models.CharField(max_length=7, choices=Status.choices, default=Status.QUEUED)
    error_type = models.CharField(max_length=80, blank=True)
    queued_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    send_after = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "deliveries"
        constraints = [
            # Per-student kinds may email one address once per student; other kinds are
            # de-duplicated across the whole message by ``notify`` itself.
            models.UniqueConstraint(
                fields=["message", "channel", "email", "student"],
                condition=Q(channel="email"),
                name="comms_delivery_email_unique",
            ),
            models.UniqueConstraint(
                fields=["message", "channel", "user"],
                condition=Q(channel="inapp"),
                name="comms_delivery_inapp_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "send_after"], name="comms_dlv_status_after"),
            models.Index(fields=["user", "read_at"], name="comms_dlv_user_read"),
        ]

    def __str__(self) -> str:
        return f"Delivery {self.pk} ({self.channel}, {self.status})"


class CommsSettings(models.Model):
    """Per-school singleton (pk=1). Use CommsSettings.load()."""

    email_quiet_start = models.TimeField(default=time(21, 0))
    email_quiet_end = models.TimeField(default=time(7, 0))
    reminder_min_balance_kobo = models.PositiveBigIntegerField(default=100_000)
    reminder_template = models.TextField(default=DEFAULT_REMINDER_TEMPLATE)

    class Meta:
        verbose_name_plural = "comms settings"

    def __str__(self) -> str:
        return "Comms settings"

    @classmethod
    def load(cls) -> "CommsSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class FeeReminderDate(models.Model):
    """A date on which debtors' families are reminded (up to 6 per term, set in Task 5)."""

    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name="fee_reminder_dates")
    date = models.DateField()

    class Meta:
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(fields=["term", "date"], name="comms_reminder_date_unique"),
        ]

    def __str__(self) -> str:
        return str(self.date)
