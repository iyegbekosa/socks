from django.urls import path

from apps.core.views import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
]

handler400 = "apps.core.views.bad_request"
handler403 = "apps.core.views.permission_denied"
handler404 = "apps.core.views.not_found"
handler500 = "apps.core.views.server_error"
