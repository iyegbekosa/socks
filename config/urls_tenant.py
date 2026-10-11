from django.urls import include, path

from apps.accounts.views import home
from apps.core.views import healthz

urlpatterns = [
    path("", home, name="home"),
    path("accounts/", include("apps.accounts.urls")),
    path("healthz", healthz, name="healthz"),
]

handler400 = "apps.core.views.bad_request"
handler403 = "apps.core.views.permission_denied"
handler404 = "apps.core.views.not_found"
handler500 = "apps.core.views.server_error"
