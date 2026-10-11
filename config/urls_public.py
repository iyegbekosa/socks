from django.urls import path

from apps.core.views import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
]
