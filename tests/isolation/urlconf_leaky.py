"""Test-only URLConf: deliberately leaky and well-behaved authenticated views.

Used to prove ``assert_cross_school_refused`` catches a real leak. Never routed in production.
"""

from django.contrib.auth import get_user_model
from django.http import Http404
from django.urls import path
from django_tenants.utils import schema_context
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

LEAK_SCHEMA = "school_b"


def _row(user):
    return {"id": user.pk, "admission_number": user.admission_number}


class LeakyDetail(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        with schema_context(LEAK_SCHEMA):
            return Response(_row(get_user_model().objects.get(pk=pk)))


class LeakyList(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        with schema_context(LEAK_SCHEMA):
            return Response([_row(u) for u in get_user_model().objects.filter(kind="student")])


class SafeDetail(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        raise Http404


class SafeList(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response([_row(u) for u in get_user_model().objects.filter(kind="student")])


urlpatterns = [
    path("api/v1/leaky/<int:pk>/", LeakyDetail.as_view()),
    path("api/v1/leaky/", LeakyList.as_view()),
    path("api/v1/safe/<int:pk>/", SafeDetail.as_view()),
    path("api/v1/safe/", SafeList.as_view()),
]
