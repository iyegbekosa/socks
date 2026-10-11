from django.db import connection
from django.urls import path
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

app_name = "api"


class MeView(APIView):
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "head", "options"]

    def get(self, request: Request) -> Response:
        user = request.user
        school_name = connection.tenant.name  # type: ignore[attr-defined]
        return Response(
            {
                "id": user.pk,
                "kind": user.kind,
                "full_name": user.get_full_name(),
                "roles": sorted(user.roles),
                "admission_number": user.admission_number,
                "school": {"name": school_name},
            }
        )


urlpatterns = [path("me", MeView.as_view(), name="me")]
