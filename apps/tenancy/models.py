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
    # Set (in the same transaction as CREATE SCHEMA) only when the provisioning task created
    # this school's schema itself. A retry may re-use an existing schema only when this is set,
    # so signup can never adopt a schema that was already in the database.
    schema_created_by_provisioning = models.BooleanField(default=False)

    # Online payments (FR-11): the school's Paystack subaccount. Only the bank name and the
    # last 4 digits of the account number are kept here, never the full number.
    paystack_subaccount_code = models.CharField(max_length=50, blank=True, default="")
    payout_bank_name = models.CharField(max_length=120, blank=True, default="")
    payout_account_last4 = models.CharField(max_length=4, blank=True, default="")

    auto_create_schema = True

    def __str__(self) -> str:
        return self.name

    @property
    def online_payments_ready(self) -> bool:
        return bool(self.paystack_subaccount_code)


class PaymentRoute(models.Model):
    """Public map from a payment reference to its school's schema, for the webhook (Task 8).

    Created from the tenant request in the same transaction as the PaymentAttempt: the public
    schema is on the tenant search_path, so a plain ORM create lands here.
    """

    reference = models.CharField(max_length=40, unique=True)
    schema_name = models.CharField(max_length=63)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.reference} -> {self.schema_name}"


class Domain(DomainMixin):
    pass
