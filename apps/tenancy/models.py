from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


class Plan(models.Model):
    code = models.SlugField(unique=True)
    name = models.CharField(max_length=100)
    max_students = models.PositiveIntegerField(null=True, blank=True)  # None = unlimited
    storage_bytes = models.BigIntegerField()
    sms_credits_per_term = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.name


class School(TenantMixin):
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    # Null means the "basic" plan.
    plan = models.ForeignKey(
        Plan, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    levels_offered = models.JSONField(default=list, blank=True)
    contact_name = models.CharField(max_length=200, blank=True, default="")
    contact_phone = models.CharField(max_length=20, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")
    provisioning_status = models.CharField(
        max_length=10,
        choices=[("pending", "Pending"), ("ready", "Ready"), ("failed", "Failed")],
        default="ready",
    )

    auto_create_schema = True

    def __str__(self) -> str:
        return self.name


class Domain(DomainMixin):
    pass
