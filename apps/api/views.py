from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection
from django.db.models import Prefetch, Q, QuerySet
from django.shortcuts import get_object_or_404
from rest_framework import exceptions, status
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.academics.scope import students_visible_to
from apps.academics.services import current_session
from apps.accounts.roles import (
    BURSAR,
    PRINCIPAL,
    PROPRIETOR,
    SCHOOL_ADMIN,
    TEACHER,
    HasRole,
)
from apps.api.serializers import (
    ImportJobSerializer,
    ImportUploadSerializer,
    StudentListSerializer,
)
from apps.imports.models import ImportJob
from apps.imports.services import ImportNotReady, create_import_job
from apps.students.models import Enrolment, Student
from apps.tenancy.limits import PlanLimitExceeded

STAFF_ROLES = (SCHOOL_ADMIN, PROPRIETOR, PRINCIPAL, BURSAR, TEACHER)


def current_class(student: Student, session: Any) -> dict[str, str] | None:
    enrolment = (
        student.enrolments.filter(session=session, status=Enrolment.Status.ACTIVE)
        .select_related("arm__level")
        .first()
        if session is not None
        else None
    )
    if enrolment is None:
        return None
    return {"arm": enrolment.arm.display_name, "session": session.name}


class MeView(APIView):
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "head", "options"]

    def get(self, request: Request) -> Response:
        user = request.user
        school_name = connection.tenant.name  # type: ignore[attr-defined]
        data: dict[str, Any] = {
            "id": user.pk,
            "kind": user.kind,
            "full_name": user.get_full_name(),
            "roles": sorted(user.roles),
            "admission_number": user.admission_number,
            "school": {"name": school_name},
        }
        if user.kind == "student":
            student = Student.objects.filter(user=user).first()
            data["current_class"] = (
                current_class(student, current_session()) if student is not None else None
            )
        return Response(data)


class StudentPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


class StudentListView(ListAPIView):  # type: ignore[type-arg]
    permission_classes = [HasRole.with_roles(*STAFF_ROLES)]
    serializer_class = StudentListSerializer
    pagination_class = StudentPagination

    def get_queryset(self) -> QuerySet[Student]:
        session = current_session()
        qs = students_visible_to(self.request.user).select_related("user")
        params = self.request.query_params
        raw_arm = (params.get("class_arm") or "").strip()
        if raw_arm:
            if not (raw_arm.isascii() and raw_arm.isdigit()):
                raise exceptions.ValidationError({"class_arm": "Must be a whole number."})
            if session is None:
                return qs.none()
            qs = qs.filter(
                enrolments__arm_id=int(raw_arm),
                enrolments__session=session,
                enrolments__status=Enrolment.Status.ACTIVE,
            ).distinct()
        q = params.get("q") or ""
        if "\x00" in q:
            raise exceptions.ValidationError({"q": "Contains an invalid character."})
        for token in q.split():
            qs = qs.filter(
                Q(first_name__icontains=token)
                | Q(middle_name__icontains=token)
                | Q(last_name__icontains=token)
                | Q(user__admission_number__icontains=token)
            )
        enrolments = Enrolment.objects.filter(
            session=session, status=Enrolment.Status.ACTIVE
        ).select_related("arm__level")
        return qs.prefetch_related(Prefetch("enrolments", enrolments, to_attr="current_enrolments"))


class StudentImportView(APIView):
    permission_classes = [HasRole.with_roles(SCHOOL_ADMIN)]
    parser_classes = [MultiPartParser]
    http_method_names = ["post", "options"]

    def post(self, request: Request) -> Response:
        serializer = ImportUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        upload = serializer.validated_data["file"]
        try:
            job = create_import_job(
                uploaded_file=upload,
                kind=ImportJob.Kind.STUDENTS,
                user=request.user,
                request=request,
            )
        except DjangoValidationError as exc:
            raise exceptions.ValidationError({"file": exc.messages}) from exc
        except (ImportNotReady, PlanLimitExceeded) as exc:
            field = "file" if isinstance(exc, PlanLimitExceeded) else "non_field_errors"
            raise exceptions.ValidationError({field: [exc.message]}) from exc
        return Response({"job_id": job.pk}, status=status.HTTP_202_ACCEPTED)


class ImportDetailView(APIView):
    permission_classes = [HasRole.with_roles(SCHOOL_ADMIN)]
    http_method_names = ["get", "head", "options"]

    def get(self, request: Request, pk: int) -> Response:
        job = get_object_or_404(ImportJob, pk=pk)
        return Response(ImportJobSerializer(job).data)
