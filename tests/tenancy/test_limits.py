import pytest
from django.db import connection
from django_tenants.utils import schema_context

from apps.files.models import StoredFile
from apps.tenancy.limits import PlanLimitExceeded, PlanLimits
from apps.tenancy.models import Plan
from tests.accounts.factories import StudentFactory
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db

GIB = 1024**3


def test_plans_seeded():
    got = {
        p.code: (p.max_students, p.storage_bytes, p.sms_credits_per_term)
        for p in Plan.objects.all()
    }
    assert got == {
        "basic": (300, 2 * GIB, 500),
        "standard": (1000, 10 * GIB, 2000),
        "premium": (None, 50 * GIB, 5000),
    }


def test_null_plan_uses_basic(school_a):
    assert school_a.plan is None
    with schema_context("school_a"):
        limits = PlanLimits.for_current_school()
    assert limits.plan.code == "basic"


def test_assigned_plan_is_used(school_a):
    school_a.plan = Plan.objects.get(code="premium")
    school_a.save()
    try:
        with schema_context("school_a"):
            assert PlanLimits.for_current_school().plan.code == "premium"
    finally:
        school_a.plan = None
        school_a.save()


def test_check_can_store(school_a):
    with schema_context("school_a"):
        limits = PlanLimits.for_current_school()
        StoredFile.objects.create(name="a.pdf", bytes=limits.plan.storage_bytes - 10)
        assert limits.storage_used_bytes() == limits.plan.storage_bytes - 10
        limits.check_can_store(10)
        with pytest.raises(PlanLimitExceeded) as exc:
            limits.check_can_store(11)
    assert exc.value.code == "PLAN_LIMIT_EXCEEDED"
    assert exc.value.status == 403


def test_usage_is_per_school(school_a, school_b):
    with schema_context("school_a"):
        StoredFile.objects.create(name="a.pdf", bytes=1000)
    with schema_context("school_b"):
        assert PlanLimits.for_current_school().storage_used_bytes() == 0


@pytest.fixture
def tiny_plan(school_a):
    plan = Plan.objects.create(
        code="tiny", name="Tiny", max_students=3, storage_bytes=GIB, sms_credits_per_term=0
    )
    old = school_a.plan
    school_a.plan = plan
    school_a.save()
    yield plan
    school_a.plan = old
    school_a.save()


def test_student_limit(tiny_plan):
    with schema_context("school_a"):
        StudentProfileFactory.create_batch(2)
        limits = PlanLimits.for_current_school()
        assert limits.student_count() == 2
        limits.check_can_add_students()
        with pytest.raises(PlanLimitExceeded) as exc:
            limits.check_can_add_students(2)
        assert "up to 3 students" in exc.value.message
        StudentProfileFactory()
        with pytest.raises(PlanLimitExceeded):
            limits.check_can_add_students()


def test_withdrawn_and_profileless_students_not_counted(school_a):
    with schema_context("school_a"):
        StudentProfileFactory(status="withdrawn")
        StudentFactory()
        assert PlanLimits.for_current_school().student_count() == 0


def test_premium_never_raises(school_a):
    school_a.plan = Plan.objects.get(code="premium")
    school_a.save()
    try:
        with schema_context("school_a"):
            StudentProfileFactory.create_batch(2)
            PlanLimits.for_current_school().check_can_add_students(5000)
    finally:
        school_a.plan = None
        school_a.save()


def test_fake_tenant_falls_back_to_schema_lookup(school_a):
    from django_tenants.postgresql_backend.base import FakeTenant

    with schema_context("school_a"):
        connection.tenant = FakeTenant(schema_name="school_a")
        assert PlanLimits.for_current_school().plan.code == "basic"
