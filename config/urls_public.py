from django.urls import path

from apps.core.views import healthz
from apps.tenancy.views import landing

urlpatterns = [
    path("", landing, name="landing"),
    path("healthz", healthz, name="healthz"),
]

handler500 = "apps.core.views.server_error"
