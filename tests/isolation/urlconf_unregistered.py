"""Test-only URLConf used to prove the registry guard reports missing routes."""

from django.http import HttpResponse
from django.urls import include, path, re_path
from rest_framework.response import Response
from rest_framework.views import APIView


def _view(request):
    return HttpResponse("ok")


class ThingView(APIView):
    """Never called: only its handler methods matter to the guard."""

    def get(self, request, pk):
        return Response({})

    def patch(self, request, pk):
        return Response({})


_v1 = [
    path("students/", _view, name="student-list"),
    path("registered/", _view, name="registered-route"),
    path("anonymous/", _view),  # unnamed: cannot be registered
]

_students = ([path("", _view, name="list"), path("<int:pk>/", _view, name="detail")], "students")
_teachers = ([path("<int:pk>/", _view, name="detail")], "teachers")

_things = ([path("<int:pk>/", ThingView.as_view(), name="detail")], "things")

urlpatterns = [
    path("api/v1/things/", include(_things)),
    path("api/v1/students/", include(_students)),
    path("api/v1/teachers/", include(_teachers)),
    path("healthz", _view, name="healthz"),
    path("api/v1/", include(_v1)),
    path("other/", _view, name="not-under-api"),
    re_path(r"^api/v1/regex-route/$", _view, name="regex-route"),
    re_path(r"^api/", include([re_path(r"^v1/nested-regex/$", _view, name="nested-regex")])),
]
