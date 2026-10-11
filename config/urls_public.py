from django.urls import path

from apps.core.views import healthz
from apps.tenancy.views import landing, signup, signup_status

urlpatterns = [
    path("", landing, name="landing"),
    path("signup/", signup, name="signup"),
    path("signup/status/<str:token>/", signup_status, name="signup_status"),
    path("healthz", healthz, name="healthz"),
]

handler400 = "apps.core.views.bad_request"
handler403 = "apps.core.views.permission_denied"
handler404 = "apps.core.views.not_found"
handler500 = "apps.core.views.server_error"
