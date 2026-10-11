"""Test-only URLConf used to prove the registry guard reports missing routes."""

from django.http import HttpResponse
from django.urls import include, path, re_path
from django.views import View
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSet


def _view(request):
    return HttpResponse("ok")


class ThingView(APIView):
    """Never called: only its handler methods matter to the guard."""

    def get(self, request, pk):
        return Response({})

    def patch(self, request, pk):
        return Response({})


class WidgetViewSet(ViewSet):
    """Never called: routed with an explicit actions mapping."""

    def retrieve(self, request, pk):
        return Response({})

    def partial_update(self, request, pk):
        return Response({})


class GadgetView(View):
    def get(self, request):
        return HttpResponse("ok")

    def post(self, request):
        return HttpResponse("ok")


_v1 = [
    path(
        "widgets/<int:pk>/",
        WidgetViewSet.as_view({"get": "retrieve", "patch": "partial_update"}),
        name="widget-detail",
    ),
    path("gadgets/", GadgetView.as_view(), name="gadget-list"),
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
