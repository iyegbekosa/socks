from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


class School(TenantMixin):
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    auto_create_schema = True

    def __str__(self) -> str:
        return self.name


class Domain(DomainMixin):
    pass
