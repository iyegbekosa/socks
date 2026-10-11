from django.db import connection
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.utils import get_public_schema_name, get_tenant_model


class HostTenantMiddleware(TenantMainMiddleware):
    """Resolve the school from the hostname only.

    - Hostnames are lowercased (RFC 4343); django-tenants does not do this.
    - Unknown hostnames fall back to the public tenant *and* set
      ``request.tenant``, so downstream code (logging, health checks) can
      always rely on it. django-tenants leaves it unset in that case.
    """

    @staticmethod
    def hostname_from_request(request) -> str:
        return TenantMainMiddleware.hostname_from_request(request).lower()

    def no_tenant_found(self, request, hostname):
        response = super().no_tenant_found(request, hostname)
        if response is None and not hasattr(request, "tenant"):
            public = get_tenant_model().objects.filter(schema_name=get_public_schema_name()).first()
            if public is not None:
                public.domain_url = hostname
                request.tenant = public
                connection.set_tenant(public)
        return response
