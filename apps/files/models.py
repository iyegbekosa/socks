from django.db import models


class StoredFile(models.Model):
    """Bookkeeping row per stored file (per school schema); drives storage quota."""

    name = models.CharField(max_length=500, unique=True)
    bytes = models.BigIntegerField(default=0)
    delivery_type = models.CharField(max_length=20, default="authenticated")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name
