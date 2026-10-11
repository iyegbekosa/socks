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

_students = ([path("", _view, name="list"), path("<int:pk>/", _view, name="detail")], "students")
_teachers = ([path("<int:pk>/", _view, name="detail")], "teachers")

urlpatterns = [
    path("api/v1/students/", include(_students)),
    path("api/v1/teachers/", include(_teachers)),
    path("healthz", _view, name="healthz"),
    path("api/v1/", include(_v1)),
    path("other/", _view, name="not-under-api"),
]
