from datetime import date
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection
from django.db.models import Prefetch, Q, QuerySet
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from rest_framework import exceptions, status
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.academics.models import ClassArm, SubjectOffering, Term
from apps.academics.scope import students_visible_to
from apps.academics.services import current_session, current_term
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
    sheet_data_json,
)
from apps.core.errors import ApiError
from apps.core.money import money_fields
from apps.core.parsing import parse_id
from apps.fees import checkout, manual
from apps.fees.gateways import GatewayUnavailable
from apps.fees.ledger import PlannedLine
from apps.fees.models import Invoice, ManualPayment
from apps.fees.scope import can_manage_fees
from apps.imports.models import ImportJob
from apps.imports.services import ImportNotReady, create_import_job
from apps.results.cards import downloadable_card
from apps.results.scope import can_enter_scores
from apps.results.scores import (
    CellInput,
    ResultsLocked,
    VersionConflict,
    save_cells,
    sheet_rows,
)
from apps.results.views.cards import signed_redirect
from apps.results.workflow import ResultsStateError, approve_class
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


MAX_CELLS = 2000
CELL_KEYS = {"student", "component", "value", "version"}


def _bad(field: str, message: str) -> ApiError:
    return ApiError(
        "VALIDATION_ERROR",
        "Some fields are invalid.",
        400,
        [{"field": field, "message": message}],
    )


def _forbidden() -> ApiError:
    return ApiError("FORBIDDEN", "Only the subject's teacher can enter its scores.", 403)


def _json_id(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if 0 < raw < 10**18 else None
    return parse_id(raw) if isinstance(raw, str) else None


def _parse_cells(data: Any) -> list[CellInput]:
    if not isinstance(data, dict) or not isinstance(data.get("cells"), list):
        raise _bad("cells", "Send a list of cells.")
    raw_cells = data["cells"]
    if len(raw_cells) > MAX_CELLS:
        raise _bad("cells", f"Send at most {MAX_CELLS} cells at a time.")
    cells = []
    for i, raw in enumerate(raw_cells):
        if not isinstance(raw, dict) or set(raw) != CELL_KEYS:
            raise _bad(f"cells.{i}", "Each cell needs student, component, value and version.")
        student, component = _json_id(raw["student"]), _json_id(raw["component"])
        if student is None:
            raise _bad(f"cells.{i}.student", "Must be a student id.")
        if component is None:
            raise _bad(f"cells.{i}.component", "Must be a component id.")
        version = raw["version"]
        if isinstance(version, bool) or not isinstance(version, int) or not 0 <= version < 2**31:
            raise _bad(f"cells.{i}.version", "Must be a whole number from 0.")
        value = raw["value"]
        if isinstance(value, bool) or not isinstance(value, str | int | float | None):
            raise _bad(f"cells.{i}.value", "Must be a number, a string or null.")
        if isinstance(value, int | float):
            value = str(value)
        cells.append(CellInput(student, component, value, version))
    return cells


class ScoresView(APIView):
    """GET/PUT one score sheet: ``?offering=<id>&term=<id>`` (term defaults to the current one).

    Only the teacher assigned to the offering may use it (FR-06).
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "put", "head", "options"]

    def _sheet(self, request: Request) -> tuple[SubjectOffering, Term]:
        user = request.user
        if user.is_student or not user.has_role(TEACHER):  # type: ignore[union-attr]
            raise _forbidden()
        params = request.query_params
        offering_id = parse_id(params.get("offering", ""))
        if offering_id is None:
            raise _bad("offering", "Must be a subject id.")
        raw_term = params.get("term")
        term_id = None
        if raw_term is not None:
            term_id = parse_id(raw_term)
            if term_id is None:
                raise _bad("term", "Must be a term id.")
        offering = get_object_or_404(
            SubjectOffering.objects.select_related("arm__level"), pk=offering_id
        )
        if not can_enter_scores(user, offering):
            raise _forbidden()
        if term_id is None:
            term = current_term()
            if term is None:
                raise ApiError("VALIDATION_ERROR", "Set a current term first", 400)
        else:
            term = Term.objects.filter(pk=term_id).first()  # type: ignore[assignment]
        if term is None or term.session_id != offering.session_id:
            raise _bad("term", "Choose a term in this subject's session.")
        return offering, term

    def get(self, request: Request) -> Response:
        offering, term = self._sheet(request)
        return Response(sheet_data_json(sheet_rows(offering, term)))

    def put(self, request: Request) -> Response:
        offering, term = self._sheet(request)
        cells = _parse_cells(request.data)
        try:
            result = save_cells(offering, term, cells, by=request.user, request=request)
        except ResultsLocked as exc:
            raise ApiError("RESULTS_LOCKED", exc.message, 409) from exc
        except VersionConflict as exc:
            raise ApiError(
                "VERSION_CONFLICT",
                "Someone else changed some of these scores. Reload to see their values.",
                409,
                exc.conflicts,
            ) from exc
        except DjangoValidationError as exc:
            if hasattr(exc, "error_dict"):
                details = [
                    {"field": key, "message": message}
                    for key, messages in exc.message_dict.items()
                    for message in messages
                ]
            else:
                details = [{"field": "non_field_errors", "message": m} for m in exc.messages]
            raise ApiError("VALIDATION_ERROR", "Some scores are invalid.", 400, details) from exc
        return Response({"versions": result.versions})


class ResultsApproveView(APIView):
    """POST: the principal approves a reviewed class for the current term (FR-08).

    202 ``{"status": "approved"}``; the results are computed in the background.
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ["post", "options"]

    def post(self, request: Request, class_arm: int) -> Response:
        user = request.user
        if user.is_student or not user.has_role(PRINCIPAL):  # type: ignore[union-attr]
            raise ApiError("FORBIDDEN", "Only the principal can approve class results.", 403)
        arm = get_object_or_404(ClassArm.objects.select_related("level"), pk=class_arm)
        term = current_term()
        if term is None:
            raise ApiError("VALIDATION_ERROR", "Set a current term first", 400)
        try:
            approve_class(arm, term, by=user, request=request)
        except ResultsStateError as exc:
            raise ApiError("RESULTS_STATE", " ".join(exc.messages), 409) from exc
        return Response({"status": "approved"}, status=status.HTTP_202_ACCEPTED)


class ReportCardView(APIView):
    """GET: 302 to a short-lived signed URL for a student's report card (``no-store``).

    Students: their own card, once their class for the term is published. Staff: students in
    ``students_visible_to``. Anything else, including a missing card, is a §6.5 404.
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "head", "options"]

    def get(self, request: Request, student: str, term: str) -> HttpResponseRedirect:
        card = downloadable_card(request.user, student, term)
        if card is None:
            raise ApiError("NOT_FOUND", "Report card not found.", 404)
        return signed_redirect(card.file_name)


MANUAL_KEYS = {"student", "method", "amount_kobo", "invoices", "paid_on", "reference_no"}
MANUAL_FIELD_NAMES = {"amount": "amount_kobo"}


def _validation(details: list[dict]) -> ApiError:
    return ApiError("VALIDATION_ERROR", "Some fields are invalid.", 400, details)


def _json_kobo(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


class ManualPaymentView(APIView):
    """POST: a bursar or school admin records a manual payment (UC-13).

    201 ``{id, status, payment}``; ``payment`` is null while waiting for approval. Unknown
    student or invoice ids (including another school's) are a 404.
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ["post", "options"]

    def post(self, request: Request) -> Response:
        user = request.user
        if not can_manage_fees(user):
            raise ApiError("FORBIDDEN", "Only a bursar or school admin can record payments.", 403)
        data = request.data
        if not isinstance(data, dict):
            raise _bad("non_field_errors", "Send a JSON object.")
        unknown = sorted(set(data) - MANUAL_KEYS)
        if unknown:
            raise _bad(unknown[0], "Unknown field.")

        student_id = _json_id(data.get("student"))
        if student_id is None:
            raise _bad("student", "Must be a student id.")
        method = data.get("method")
        if not isinstance(method, str) or method not in manual.METHODS:
            raise _bad("method", "Must be one of cash, teller, pos or transfer.")
        amount = _json_kobo(data.get("amount_kobo"))
        if amount is None:
            raise _bad("amount_kobo", "Must be a whole number of kobo.")
        raw_paid_on = data.get("paid_on")
        try:
            if not isinstance(raw_paid_on, str) or len(raw_paid_on) != 10:
                raise ValueError
            paid_on = date.fromisoformat(raw_paid_on)
        except ValueError:
            raise _bad("paid_on", "Must be a date like 2026-10-01.") from None
        reference_no = data.get("reference_no") or ""
        if not isinstance(reference_no, str):
            raise _bad("reference_no", "Must be text.")
        raw_lines = data.get("invoices")
        if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= manual.MAX_PLAN_LINES:
            raise _bad("invoices", f"Send 1 to {manual.MAX_PLAN_LINES} invoices.")
        plan: list[PlannedLine] = []
        for i, raw in enumerate(raw_lines):
            if not isinstance(raw, dict) or set(raw) != {"invoice", "amount_kobo"}:
                raise _bad(f"invoices.{i}", "Each line needs invoice and amount_kobo.")
            invoice_id = _json_id(raw["invoice"])
            if invoice_id is None:
                raise _bad(f"invoices.{i}.invoice", "Must be an invoice id.")
            kobo = _json_kobo(raw["amount_kobo"])
            if kobo is None:
                raise _bad(f"invoices.{i}.amount_kobo", "Must be a whole number of kobo.")
            plan.append(PlannedLine(invoice_id, kobo))

        student = get_object_or_404(Student, pk=student_id)
        found = set(
            Invoice.objects.filter(pk__in={ln.invoice_id for ln in plan}).values_list(
                "pk", flat=True
            )
        )
        if any(ln.invoice_id not in found for ln in plan):
            raise ApiError("NOT_FOUND", "Invoice not found.", 404)

        try:
            mp = manual.record(
                student=student,
                method=method,
                amount_kobo=amount,
                plan=plan,
                paid_on=paid_on,
                reference_no=reference_no,
                by=user,
                request=request,
            )
        except DjangoValidationError as exc:
            if hasattr(exc, "error_dict"):
                details = [
                    {"field": MANUAL_FIELD_NAMES.get(key, key), "message": message}
                    for key, messages in exc.message_dict.items()
                    for message in messages
                ]
            else:
                details = [{"field": "non_field_errors", "message": m} for m in exc.messages]
            raise _validation(details) from exc

        payment = None
        if mp.status == ManualPayment.APPROVED:
            p = mp.payment
            payment = {
                "id": p.pk,
                **money_fields(p.amount_kobo),
                "unallocated_kobo": p.unallocated_kobo,
                "receipt_number": p.receipt.number,
            }
        return Response(
            {"id": mp.pk, "status": mp.status, "payment": payment},
            status=status.HTTP_201_CREATED,
        )


INITIALIZE_KEYS = {"invoices"}


class PaymentsInitializeView(APIView):
    """POST: a student starts an online payment for their own or siblings' invoices (§4.5).

    Body ``{invoices: [{invoice, amount_kobo}]}``. 201 ``{reference, authorization_url}``.
    Staff get 403; an invoice that is not the student's or a sibling's is a 404; no payout
    account is a 409 ``ONLINE_PAYMENTS_NOT_READY``; a gateway outage is a 503
    ``GATEWAY_UNAVAILABLE`` whose message carries the school's bank transfer note.
    """

    permission_classes = [IsAuthenticated]
    http_method_names = ["post", "options"]

    def post(self, request: Request) -> Response:
        user = request.user
        student = (
            Student.objects.filter(user=user).first()
            if getattr(user, "kind", None) == "student"
            else None
        )
        if student is None:
            raise ApiError("FORBIDDEN", "Only students can pay online.", 403)
        data = request.data
        if not isinstance(data, dict):
            raise _bad("non_field_errors", "Send a JSON object.")
        unknown = sorted(set(data) - INITIALIZE_KEYS)
        if unknown:
            raise _bad(unknown[0], "Unknown field.")
        raw_lines = data.get("invoices")
        if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= checkout.MAX_LINES:
            raise _bad("invoices", f"Send 1 to {checkout.MAX_LINES} invoices.")
        lines: list[PlannedLine] = []
        for i, raw in enumerate(raw_lines):
            if not isinstance(raw, dict) or set(raw) != {"invoice", "amount_kobo"}:
                raise _bad(f"invoices.{i}", "Each line needs invoice and amount_kobo.")
            invoice_id = _json_id(raw["invoice"])
            if invoice_id is None:
                raise _bad(f"invoices.{i}.invoice", "Must be an invoice id.")
            kobo = _json_kobo(raw["amount_kobo"])
            if kobo is None:
                raise _bad(f"invoices.{i}.amount_kobo", "Must be a whole number of kobo.")
            lines.append(PlannedLine(invoice_id, kobo))

        try:
            attempt = checkout.start(
                student,
                lines,
                callback_url=request.build_absolute_uri(reverse("fees:pay_return")),
                request=request,
            )
        except checkout.OnlinePaymentsNotReady:
            raise ApiError("ONLINE_PAYMENTS_NOT_READY", checkout.MSG_NOT_READY, 409) from None
        except DjangoValidationError as exc:
            raise _validation([{"field": "invoices", "message": m} for m in exc.messages]) from exc
        except GatewayUnavailable:
            raise ApiError("GATEWAY_UNAVAILABLE", checkout.unavailable_message(), 503) from None
        return Response(
            {"reference": attempt.reference, "authorization_url": attempt.authorization_url},
            status=status.HTTP_201_CREATED,
        )
