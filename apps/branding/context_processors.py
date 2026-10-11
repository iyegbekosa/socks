from django.db import connection
from django_tenants.utils import get_public_schema_name

from apps.branding.models import get_branding


def branding(request):
    if getattr(connection, "schema_name", get_public_schema_name()) == get_public_schema_name():
        return {"branding": None}
    return {"branding": get_branding()}
