"""Registry of cross-school isolation cases, and the guard that enforces it.

Every named tenant route under ``api/v1/`` must either have an ``IsolationCase``
(school A must not be able to reach school B's object through it) or be listed
in ``EXEMPT_ROUTES``. Later phases append a case for each route they add.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

from django.urls import URLPattern, URLResolver, get_resolver

API_PREFIX = "api/v1/"


def bursar_actor():
    """A bursar (an MFA role, logged in verified by the harness) in school A."""
    from apps.accounts.roles import set_roles
    from tests.accounts.factories import StaffFactory

    user = StaffFactory.create()
    set_roles(user, ["bursar"], by=None)
    return user


def default_actor():
    """A school_admin in the active (school A) schema; the caller logs them into client_a."""
    from apps.accounts.roles import set_roles
    from tests.accounts.factories import StaffFactory

    user = StaffFactory.create()
    set_roles(user, ["school_admin"], by=None)
    return user


@dataclass(frozen=True)
class IsolationCase:
    # Full namespaced route name, as passed to reverse() (e.g. "students:list").
    name: str
    method: str
    url: Callable[[Any], str]  # object created in school B -> URL requested as school A
    make_obj: Callable[[], Any]  # creates the object in the active (school B) schema
    body: dict | None = None
    # JSON body built from school B's object (e.g. B's ids posted to A); replaces ``body``.
    body_for: Callable[[Any], dict] | None = None
    # Creates the logged-in school A user (called in school A's schema); None = anonymous.
    actor: Callable[[], Any] | None = default_actor
    # "refused": status must be 401/403/404. "absent" (list endpoints, and endpoints that
    # describe the caller, such as api:me): a 2xx response (so a broken login cannot pass
    # vacuously) whose body lacks marker(obj). "no_cross_effect"
    # (write endpoints that act on the caller's own school): any status below 500, school B's
    # object unchanged and school B's count_model() unchanged, with a 2xx status and school
    # A's count_model() up by exactly one (so the request really wrote, in the right school).
    expect: Literal["refused", "absent", "no_cross_effect"] = "refused"
    marker: Callable[[Any], str] | None = None
    # Multipart form data (e.g. an uploaded file), built per request; replaces the JSON body.
    multipart: Callable[[], dict] | None = None
    # no_cross_effect: counts rows in the active schema that the request could add; the
    # harness calls it in both school A's and school B's schema. With "refused", both
    # schools' counts must stay unchanged.
    count_model: Callable[[], int] | None = None
    # Runs in school A's schema before the request (e.g. academic setup so a write succeeds).
    setup_a: Callable[[], None] | None = None
    # Distinguishes several cases for one route and method in test ids (e.g. "anonymous").
    label: str = ""

    @property
    def test_id(self) -> str:
        base = f"{self.name} {self.method.upper()}"
        return f"{base} [{self.label}]" if self.label else base

    def __post_init__(self):
        if self.expect == "absent" and self.marker is None:
            raise ValueError("expect='absent' requires a marker")
        if self.expect == "no_cross_effect" and self.count_model is None:
            raise ValueError("expect='no_cross_effect' requires count_model")


def _student_in_active_schema():
    from tests.accounts.factories import StudentFactory

    return StudentFactory.create()


def _student_with_profile():
    from tests.students.factories import StudentProfileFactory

    return StudentProfileFactory.create()


def _import_job():
    from apps.imports.models import ImportJob
    from tests.accounts.factories import StaffFactory

    return ImportJob.objects.create(
        kind="students", file_name="imports/x.csv", created_by=StaffFactory.create()
    )


def _import_job_count() -> int:
    from apps.imports.models import ImportJob

    return ImportJob.objects.count()


def _current_term_in_a() -> None:
    from datetime import date, timedelta

    from apps.academics.models import AcademicSession, ClassArm, ClassLevel, Term

    today = date.today()
    session = AcademicSession.objects.create(
        name="2026/2027",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=300),
    )
    Term.objects.create(
        session=session,
        number=1,
        start_date=session.start_date,
        end_date=today + timedelta(days=60),
        is_current=True,
    )
    ClassArm.objects.create(
        level=ClassLevel.objects.create(name="JSS 1", section="jss", order=1), suffix="A"
    )


def _score_in_active_schema():
    """A saved score (with its offering, term, student and component) in the active schema."""
    from decimal import Decimal

    from apps.results.models import Score
    from tests.results.factories import make_sheet_world

    w = make_sheet_world()
    return Score.objects.create(
        student=w.students[0],
        offering=w.offering,
        term=w.term,
        component=w.comps[0],
        value=Decimal("4"),
        version=1,
        entered_by=w.teacher,
    )


def _scores_url(score) -> str:
    return f"/api/v1/scores?offering={score.offering_id}&term={score.term_id}"


def _reviewed_class_result():
    """A class result awaiting approval (with its arm and current term) in the active schema."""
    from apps.results.models import ClassResult
    from tests.results.factories import make_sheet_world

    w = make_sheet_world()
    return ClassResult.objects.create(arm=w.arm, term=w.term, status=ClassResult.Status.REVIEWED)


def _published_report_card():
    """A stored report card (published class, computed summary) in the active schema."""
    from tests.results.factories import make_card, make_sheet_world

    w = make_sheet_world()
    return make_card(w.students[0], w.term, w.arm)


def _invoice_in_active_schema():
    """A student's unpaid invoice (with its term) in the active schema."""
    from apps.fees import invoicing
    from apps.fees.models import Invoice, InvoiceLine
    from tests.fees.factories import make_terms
    from tests.students.factories import StudentProfileFactory

    invoice = Invoice.objects.create(student=StudentProfileFactory(), term=make_terms().term1)
    InvoiceLine.objects.create(
        invoice=invoice, kind=InvoiceLine.FEE, description="Tuition", amount_kobo=1_000_000
    )
    invoicing.recompute(invoice)
    return invoice


def _manual_payment_body(invoice) -> dict:
    return {
        "student": invoice.student_id,
        "method": "cash",
        "amount_kobo": 100_000,
        "invoices": [{"invoice": invoice.pk, "amount_kobo": 100_000}],
        "paid_on": "2026-10-01",
        "reference_no": "",
    }


def _manual_payment_count() -> int:
    from apps.fees.models import ManualPayment

    return ManualPayment.objects.count()


def student_actor():
    """A student (with a profile) in school A."""
    from tests.students.factories import StudentProfileFactory

    return StudentProfileFactory.create().user


def _school_a_online_ready() -> None:
    """School A has a payout subaccount, so a refusal is about the invoice, not readiness."""
    from apps.tenancy.models import School

    School.objects.filter(schema_name="school_a").update(paystack_subaccount_code="ACCT_iso")


def _initialize_body(invoice) -> dict:
    return {"invoices": [{"invoice": invoice.pk, "amount_kobo": 100_000}]}


def _attempt_count() -> int:
    from apps.fees.models import PaymentAttempt

    return PaymentAttempt.objects.count()


# School B's student's first name. Every api:invoices item shows ``student.first_name``,
# including sibling items (which omit ``lines``), so a leaked invoice always carries it.
INVOICE_MARKER = "Zqxschoolbonly"


def _marked_invoice_in_active_schema():
    """An invoice whose student's first name cannot appear anywhere by accident.

    Its student shares the guardian phone that ``student_sibling_actor`` gets in school A, so
    a sibling lookup or invoice query that crossed schemas would return it as a sibling's.
    """
    invoice = _invoice_in_active_schema()
    invoice.student.first_name = INVOICE_MARKER
    invoice.student.guardian_phone = SHARED_PHONE
    invoice.student.save(update_fields=["first_name", "guardian_phone"])
    return invoice


SHARED_PHONE = "+2348037770001"


def student_sibling_actor():
    """A school A student with the same guardian phone as school B's marked invoice."""
    from tests.students.factories import StudentProfileFactory

    return StudentProfileFactory.create(guardian_phone=SHARED_PHONE).user


# A unique string in school B's in-app message body: it must never reach school A.
NOTIFICATION_MARKER = "Zqxnotifbodyonly"


def _marked_delivery_in_active_schema():
    from apps.comms import service
    from tests.students.factories import StudentProfileFactory

    student = StudentProfileFactory.create()
    service.notify(
        "announcement",
        students=[student],
        subject="Notice",
        body=f"Hello {NOTIFICATION_MARKER}",
        per_student=False,
        channels=("inapp",),
    )
    return student.deliveries.get(channel="inapp")


def _tiny_csv() -> dict:
    from django.core.files.uploadedfile import SimpleUploadedFile

    return {"file": SimpleUploadedFile("s.csv", b"first_name,last_name\n", "text/csv")}


def form_teacher_actor():
    """A teacher in school A who is form teacher of an arm in A's current session."""
    from tests.attendance.factories import make_register_world

    return make_register_world(1).teacher


# School B's arm id: far above any arm school A creates, so the id is unknown in A.
SCHOOL_B_ARM_PK = 7_000_001


def _attendance_record_in_active_schema():
    """A register record (its arm with a form teacher, its student) in the active schema."""
    from datetime import date

    from apps.academics.models import ClassArm, FormTeacher
    from apps.attendance.models import AttendanceRecord
    from tests.fees.factories import make_staff, make_terms
    from tests.students.factories import StudentProfileFactory

    w = make_terms()
    arm = ClassArm.objects.create(pk=SCHOOL_B_ARM_PK, level=w.level, suffix="Iso")
    teacher = make_staff("teacher")
    FormTeacher.objects.create(arm=arm, session=w.session, teacher=teacher)
    student = StudentProfileFactory(enrolment__arm=arm)
    return AttendanceRecord.objects.create(
        student=student, date=date(2026, 1, 12), arm=arm, status="present", recorded_by=teacher
    )


def _attendance_url(rec) -> str:
    return f"/api/v1/attendance/{rec.arm_id}/{rec.date.isoformat()}"


def _attendance_body(rec) -> dict:
    return {"records": [{"student": rec.student_id, "status": "absent"}]}


def _attendance_count() -> int:
    from apps.attendance.models import AttendanceRecord

    return AttendanceRecord.objects.count()


# Several cases may share a route and method (the guard only needs one); give them labels.
# Per-schema session storage is covered by
# tests/accounts/test_me_api.py::test_school_b_session_cookie_rejected_on_school_a.
ISOLATION_CASES: list[IsolationCase] = [
    # Anonymous: school B's object is unreachable without a session.
    IsolationCase(
        "api:me",
        "GET",
        url=lambda u: "/api/v1/me",
        make_obj=_student_in_active_schema,
        actor=None,
        label="anonymous",
    ),
    # Logged in as school A's admin: /me describes the caller only, never B's student.
    IsolationCase(
        "api:me",
        "GET",
        url=lambda s: "/api/v1/me",
        make_obj=_student_with_profile,
        expect="absent",
        marker=lambda s: s.admission_number,
        label="authenticated",
    ),
    # A's admin lists students: B's student must be absent (and the response must be 2xx).
    IsolationCase(
        "api:students",
        "GET",
        url=lambda s: "/api/v1/students",
        make_obj=_student_with_profile,
        expect="absent",
        marker=lambda s: s.admission_number,
    ),
    # A's POST acts on school A only: setup_a gives A a current term, so A must get a 2xx
    # and exactly one new job, while B's job and B's job count stay unchanged.
    IsolationCase(
        "api:students_import",
        "POST",
        url=lambda j: "/api/v1/students/import",
        make_obj=_import_job,
        multipart=_tiny_csv,
        expect="no_cross_effect",
        count_model=_import_job_count,
        setup_a=_current_term_in_a,
    ),
    IsolationCase(
        "api:import_detail",
        "GET",
        url=lambda j: f"/api/v1/imports/{j.pk}",
        make_obj=_import_job,
    ),
    # Scores are the assigned teacher's only, so school A's admin is refused outright (403);
    # tests/api/test_scores_api.py::test_teacher_in_a_cannot_reach_school_b_scores covers a
    # teacher assigned in school A asking for school B's offering.
    IsolationCase("api:scores", "GET", url=_scores_url, make_obj=_score_in_active_schema),
    IsolationCase(
        "api:scores",
        "PUT",
        url=_scores_url,
        make_obj=_score_in_active_schema,
        body={"cells": [{"student": 1, "component": 1, "value": "9", "version": 1}]},
    ),
    # Approval is the principal's only, so school A's admin is refused outright (403);
    # tests/api/test_results_api.py::test_principal_in_a_cannot_approve_school_b_class covers
    # a verified principal in school A posting school B's arm id (404, B unchanged).
    IsolationCase(
        "api:results_approve",
        "POST",
        url=lambda r: f"/api/v1/results/{r.arm_id}/approve",
        make_obj=_reviewed_class_result,
    ),
    # School A's admin asks for school B's card: refused (404, no redirect);
    # tests/api/test_report_card_api.py::test_admin_in_a_cannot_reach_school_b_card covers B's
    # student/term ids that do not exist in school A.
    IsolationCase(
        "api:report_card",
        "GET",
        url=lambda c: f"/api/v1/results/{c.student_id}/{c.term_id}/report-card",
        make_obj=_published_report_card,
    ),
    # School A's bursar posts school B's student and invoice ids: unknown in A, so 404, and
    # B's invoice is unchanged and no ManualPayment is created in either school.
    # tests/api/test_payments_manual_api.py covers roles.
    IsolationCase(
        "api:payments_manual",
        "POST",
        url=lambda i: "/api/v1/payments/manual",
        make_obj=_invoice_in_active_schema,
        body_for=_manual_payment_body,
        actor=bursar_actor,
        count_model=_manual_payment_count,
        label="bursar",
    ),
    IsolationCase(
        "api:payments_manual",
        "POST",
        url=lambda i: "/api/v1/payments/manual",
        make_obj=_invoice_in_active_schema,
        body_for=_manual_payment_body,
        actor=None,
        label="anonymous",
    ),
    # A school A student posts school B's invoice id: unknown in A, so 404, and no attempt
    # in either school. tests/api/test_payments_initialize_api.py covers the rest.
    IsolationCase(
        "api:payments_initialize",
        "POST",
        url=lambda i: "/api/v1/payments/initialize",
        make_obj=_invoice_in_active_schema,
        body_for=_initialize_body,
        actor=student_actor,
        count_model=_attempt_count,
        setup_a=_school_a_online_ready,
        label="student",
    ),
    IsolationCase(
        "api:payments_initialize",
        "POST",
        url=lambda i: "/api/v1/payments/initialize",
        make_obj=_invoice_in_active_schema,
        body_for=_initialize_body,
        actor=None,
        count_model=_attempt_count,
        label="anonymous",
    ),
    # A school A student lists invoices (own and siblings'): school B's invoice must be
    # absent, even though B's student shares the caller's guardian phone.
    IsolationCase(
        "api:invoices",
        "GET",
        url=lambda i: "/api/v1/invoices",
        make_obj=_marked_invoice_in_active_schema,
        actor=student_sibling_actor,
        expect="absent",
        marker=lambda i: INVOICE_MARKER,
        label="student",
    ),
    # Asking for school B's student id is a 404 (unknown in A).
    IsolationCase(
        "api:invoices",
        "GET",
        url=lambda i: f"/api/v1/invoices?student={i.student_id}",
        make_obj=_marked_invoice_in_active_schema,
        actor=bursar_actor,
        label="bursar",
    ),
    IsolationCase(
        "api:invoices",
        "GET",
        url=lambda i: "/api/v1/invoices",
        make_obj=_marked_invoice_in_active_schema,
        actor=None,
        label="anonymous",
    ),
    # Staff never start online payments: 403.
    IsolationCase(
        "api:payments_initialize",
        "POST",
        url=lambda i: "/api/v1/payments/initialize",
        make_obj=_invoice_in_active_schema,
        body_for=_initialize_body,
        count_model=_attempt_count,
        setup_a=_school_a_online_ready,
        label="staff",
    ),
    # A school A form teacher asks for school B's arm id: unknown in A, so 404, and no
    # record is written in either school. tests/api/test_attendance_api.py covers roles.
    IsolationCase(
        "api:attendance_register",
        "GET",
        url=_attendance_url,
        make_obj=_attendance_record_in_active_schema,
        actor=form_teacher_actor,
        label="form_teacher",
    ),
    IsolationCase(
        "api:attendance_register",
        "GET",
        url=_attendance_url,
        make_obj=_attendance_record_in_active_schema,
        actor=None,
        label="anonymous",
    ),
    IsolationCase(
        "api:attendance_register",
        "POST",
        url=_attendance_url,
        make_obj=_attendance_record_in_active_schema,
        body_for=_attendance_body,
        actor=form_teacher_actor,
        count_model=_attendance_count,
        label="form_teacher",
    ),
    IsolationCase(
        "api:attendance_register",
        "POST",
        url=_attendance_url,
        make_obj=_attendance_record_in_active_schema,
        body_for=_attendance_body,
        actor=None,
        count_model=_attendance_count,
        label="anonymous",
    ),
    IsolationCase(
        "api:notifications",
        "GET",
        url=lambda d: "/api/v1/notifications",
        make_obj=_marked_delivery_in_active_schema,
        actor=student_actor,
        expect="absent",
        marker=lambda d: NOTIFICATION_MARKER,
        label="student",
    ),
    IsolationCase(
        "api:notifications",
        "GET",
        url=lambda d: "/api/v1/notifications",
        make_obj=_marked_delivery_in_active_schema,
        actor=None,
        label="anonymous",
    ),
    # School B's delivery id is unknown in A: 404, and B's delivery stays unread.
    IsolationCase(
        "api:notification_read",
        "POST",
        url=lambda d: f"/api/v1/notifications/{d.pk}/read",
        make_obj=_marked_delivery_in_active_schema,
        actor=student_actor,
        label="student",
    ),
    IsolationCase(
        "api:notification_read",
        "POST",
        url=lambda d: f"/api/v1/notifications/{d.pk}/read",
        make_obj=_marked_delivery_in_active_schema,
        actor=None,
        label="anonymous",
    ),
]

# Route names deliberately outside the isolation requirement (no tenant data).
EXEMPT_ROUTES: set[str] = {"healthz"}


DRF_METHODS = ("get", "post", "put", "patch", "delete")


def _view_methods(callback) -> list[str]:
    """HTTP methods a view serves (HEAD/OPTIONS excluded).

    Router/ViewSet routes: keys of ``view.actions``. DRF APIViews (``view.cls``) and plain
    class-based views (``view.view_class``): the handlers they define. Function views: GET.
    """
    actions = getattr(callback, "actions", None)
    if actions:
        methods = [m.upper() for m in actions if m.lower() in DRF_METHODS]
    else:
        cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
        methods = [m.upper() for m in DRF_METHODS if cls is not None and hasattr(cls, m)]
    return methods or ["GET"]


def _walk(
    patterns, prefix: str = "", namespace: str = ""
) -> Iterator[tuple[str, str | None, list[str]]]:
    """Yield (full route string, namespaced route name or None, methods) per leaf pattern."""
    for entry in patterns:
        # re_path patterns stringify as "^api/v1/...": strip the anchor per segment so
        # regex routes are checked against API_PREFIX like path() routes.
        route = prefix + str(entry.pattern).removeprefix("^")
        if isinstance(entry, URLResolver):
            ns = namespace
            if entry.namespace:
                ns = f"{namespace}:{entry.namespace}" if namespace else entry.namespace
            yield from _walk(entry.url_patterns, route, ns)
        elif isinstance(entry, URLPattern):
            name = entry.name
            if name and namespace:
                name = f"{namespace}:{name}"
            yield route, name, _view_methods(entry.callback)


def unregistered_api_routes(
    urlconf: str = "config.urls_tenant", registered: set[tuple[str, str]] | None = None
) -> list[str]:
    """Return (name, method) pairs under api/v1/ with no case or exemption, as "name METHOD".

    Unnamed routes cannot be registered, so they are reported by route string.
    """
    if registered is None:
        registered = {(c.name, c.method) for c in ISOLATION_CASES}
    registered = {(name, method.upper()) for name, method in registered}
    missing = []
    for route, name, methods in _walk(get_resolver(urlconf).url_patterns):
        if not route.startswith(API_PREFIX):
            continue
        if name is None:
            missing.append(route)
        elif name not in EXEMPT_ROUTES:
            missing.extend(f"{name} {m}" for m in methods if (name, m) not in registered)
    return sorted(set(missing))


def _snapshot(obj):
    obj.refresh_from_db()
    return {f.attname: getattr(obj, f.attname) for f in obj._meta.concrete_fields}


def _request_kwargs(case, obj=None):
    if case.multipart is not None:
        return {"data": case.multipart()}
    if case.method.upper() in {"POST", "PUT", "PATCH"}:
        body = case.body_for(obj) if case.body_for is not None else case.body
        return {"data": json.dumps(body or {}), "content_type": "application/json"}
    return {}


def assert_cross_school_refused(case, school_a, school_b, client_a) -> None:
    """Request school B's object as school A (logged in as ``case.actor``) and assert no leak."""
    from django_tenants.utils import schema_context

    from tests.accounts.helpers import login_verified

    with schema_context(school_b.schema_name):
        obj = case.make_obj()
        before = _snapshot(obj)
        count_b_before = case.count_model() if case.count_model else None
        marker = case.marker(obj) if case.marker else None
    count_a_before = None
    with schema_context(school_a.schema_name):
        actor = case.actor() if case.actor is not None else None
        if case.setup_a:
            case.setup_a()
        if case.count_model:
            count_a_before = case.count_model()
    if actor is not None:
        login_verified(client_a, actor)

    response = getattr(client_a, case.method.lower())(case.url(obj), **_request_kwargs(case, obj))

    if case.expect == "refused":
        assert response.status_code in {401, 403, 404}, (
            f"{case.name}: expected 401/403/404, got {response.status_code}"
        )
        if case.count_model is not None:
            with schema_context(school_a.schema_name):
                assert case.count_model() == count_a_before, (
                    f"{case.name}: a refused request wrote rows in school A"
                )
    elif case.expect == "no_cross_effect":
        assert 200 <= response.status_code < 300, (
            f"{case.name}: expected a 2xx response, got {response.status_code}"
        )
        assert case.count_model is not None and count_a_before is not None
        with schema_context(school_a.schema_name):
            assert case.count_model() == count_a_before + 1, (
                f"{case.name}: the request did not write exactly one row in school A"
            )
    else:
        assert case.marker is not None
        assert 200 <= response.status_code < 300, (
            f"{case.name}: expected a 2xx response, got {response.status_code}"
        )
        assert marker is not None
        assert marker not in response.content.decode(), (
            f"{case.name}: school B's marker {marker!r} leaked into the response"
        )
    with schema_context(school_b.schema_name):
        assert _snapshot(obj) == before, f"{case.name}: school B's object changed"
        if case.count_model is not None:
            assert case.count_model() == count_b_before, f"{case.name}: school B's rows changed"
