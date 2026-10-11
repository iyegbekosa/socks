from django.urls import path

from apps.core.views import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
]

handler500 = "apps.core.views.server_error"
