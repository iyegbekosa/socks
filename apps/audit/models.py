from django.db import models


class AuditEvent(models.Model):
    """Append-only: a Postgres trigger rejects UPDATE and DELETE (see migration 0001)."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor_id = models.BigIntegerField(null=True, blank=True)  # no FK: rows never change
    actor_label = models.CharField(max_length=255, blank=True, default="")
    action = models.CharField(max_length=64, db_index=True)
    object_type = models.CharField(max_length=100, blank=True, default="")
    object_id = models.CharField(max_length=64, blank=True, default="")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["at", "id"]

    def __str__(self) -> str:
        return f"{self.action} {self.object_type}:{self.object_id}"
