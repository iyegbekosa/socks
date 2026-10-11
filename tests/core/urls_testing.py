"""Test-only URLConf: DRF views used to exercise the error format and log context."""

import logging

from django.http import JsonResponse
from django.urls import path
from rest_framework.exceptions import NotFound, PermissionDenied, Throttled, ValidationError
from rest_framework.views import APIView

from apps.core.errors import ApiError

logger = logging.getLogger("tests.core")


class _Base(APIView):
    authentication_classes: list = []
    permission_classes: list = []


class ApiErrorView(_Base):
    def get(self, request):
        raise ApiError(
            "SCORE_EXCEEDS_MAX",
            "CA1 score cannot be more than 10",
            400,
            [{"student": "GFS/2026/0042", "field": "ca1"}],
        )


class ForbiddenView(_Base):
    def get(self, request):
        raise PermissionDenied()


class NotFoundView(_Base):
    def get(self, request):
        raise NotFound()


class ThrottledView(_Base):
    def get(self, request):
        raise Throttled(wait=7)


class ValidationView(_Base):
    def get(self, request):
        raise ValidationError({"ca1": ["Too big."]})


def log_view(request):
    logger.warning("hello from view")
    return JsonResponse({"ok": True})


def boom_view(request):
    raise RuntimeError("boom")


urlpatterns = [
    path("api-error", ApiErrorView.as_view()),
    path("forbidden", ForbiddenView.as_view()),
    path("not-found", NotFoundView.as_view()),
    path("throttled", ThrottledView.as_view()),
    path("validation", ValidationView.as_view()),
    path("log", log_view),
    path("boom", boom_view),
]
