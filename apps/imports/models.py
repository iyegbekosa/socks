from django.conf import settings
from django.db import models


class ImportJob(models.Model):
    """One uploaded file being imported in the background.

    ``errors`` holds ``{"row", "column", "message"}`` entries; messages never contain the
    values of sensitive cells. ``message`` is a safe, human-readable reason for a failed job.
    """

    class Kind(models.TextChoices):
        STUDENTS = "students", "Students"
        STAFF = "staff", "Staff"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    file_name = models.CharField(max_length=255)
    original_name = models.CharField(max_length=255, blank=True)
    total_rows = models.PositiveIntegerField(default=0)
    ok_rows = models.PositiveIntegerField(default=0)
    error_rows = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)
    # Arms that received students: [{"id", "name", "count"}], for links to the class lists.
    arms = models.JSONField(default=list, blank=True)
    # Students admitted by this job: their one-time passwords are printed from here.
    admitted_student_ids = models.JSONField(default=list, blank=True)
    message = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="import_jobs"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} import #{self.pk}"

    def __repr__(self) -> str:
        return f"<ImportJob pk={self.pk} kind={self.kind} status={self.status}>"

    @property
    def is_active(self) -> bool:
        return self.status in (self.Status.QUEUED, self.Status.RUNNING)
