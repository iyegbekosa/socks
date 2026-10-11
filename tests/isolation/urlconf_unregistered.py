"""Test-only URLConf used to prove the registry guard reports missing routes."""

from django.http import HttpResponse
from django.urls import include, path


def _view(request):
    return HttpResponse("ok")


_v1 = [
    path("students/", _view, name="student-list"),
    path("registered/", _view, name="registered-route"),
    path("anonymous/", _view),  # unnamed: cannot be registered
]

urlpatterns = [
    path("healthz", _view, name="healthz"),
    path("api/v1/", include(_v1)),
    path("other/", _view, name="not-under-api"),
]
