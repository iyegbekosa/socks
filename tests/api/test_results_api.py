import pytest
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.results.models import ClassResult
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified
from tests.results.factories import make_sheet_world

pytestmark = pytest.mark.django_db


def staff(*roles):
    user = StaffFactory()
    set_roles(user, list(roles), by=None)
    return user


@pytest.fixture
def g(school_a):
    with schema_context("school_a"):
        world = make_sheet_world()
        world.result = ClassResult.objects.create(
            arm=world.arm, term=world.term, status=ClassResult.Status.REVIEWED
        )
        yield world


def url(arm_pk):
    return f"/api/v1/results/{arm_pk}/approve"


def status(g):
    with schema_context("school_a"):
        return ClassResult.objects.get(pk=g.result.pk).status


def test_principal_approves(client_a, g):
    login_verified(client_a, staff("principal"))
    r = client_a.post(url(g.arm.pk))
    assert r.status_code == 202, r.content
    assert r.json() == {"status": "approved"}
    assert status(g) == "approved"
    with schema_context("school_a"):
        ev = AuditEvent.objects.get(action="results.class_approved")
        assert ev.after == {"arm_id": g.arm.pk, "term_id": g.term.pk}


def test_wrong_state_is_409(client_a, g):
    ClassResult.objects.filter(pk=g.result.pk).update(status="draft")
    login_verified(client_a, staff("principal"))
    r = client_a.post(url(g.arm.pk))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESULTS_STATE"
    assert status(g) == "draft"


def test_class_without_result_row_is_409(client_a, g):
    level = ClassLevel.objects.create(name="JSS 2", section="jss", order=10)
    arm = ClassArm.objects.create(level=level, suffix="B")
    login_verified(client_a, staff("principal"))
    r = client_a.post(url(arm.pk))
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESULTS_STATE"


def test_others_are_403(client_a, g):
    for roles in (["teacher"], ["school_admin"], ["proprietor"], ["bursar"]):
        user = staff(*roles)
        login_verified(client_a, user)
        r = client_a.post(url(g.arm.pk))
        assert r.status_code == 403, roles
    client_a.force_login(g.students[0].user)
    assert client_a.post(url(g.arm.pk)).status_code == 403
    assert status(g) == "reviewed"


def test_unknown_arm_is_404(client_a, g):
    login_verified(client_a, staff("principal"))
    assert client_a.post(url(999999)).status_code == 404


def test_anonymous_is_refused(client_a, g):
    assert client_a.post(url(g.arm.pk)).status_code in (401, 403)


def test_get_not_allowed(client_a, g):
    login_verified(client_a, staff("principal"))
    assert client_a.get(url(g.arm.pk)).status_code == 405


def test_no_current_term(client_a, g):
    from apps.academics.models import Term

    Term.objects.update(is_current=False)
    login_verified(client_a, staff("principal"))
    r = client_a.post(url(g.arm.pk))
    assert r.status_code == 400
    assert status(g) == "reviewed"


def test_principal_in_a_cannot_approve_school_b_class(client_a, school_a, school_b):
    with schema_context("school_b"):
        w = make_sheet_world()
        level = ClassLevel.objects.create(name="SSS 9", section="sss", order=99)
        arm_b = ClassArm.objects.create(pk=987654, level=level, suffix="Z")
        result_b = ClassResult.objects.create(
            arm=arm_b, term=w.term, status=ClassResult.Status.REVIEWED
        )
    with schema_context("school_a"):
        make_sheet_world()
        assert not ClassArm.objects.filter(pk=arm_b.pk).exists()
        principal = staff("principal")
    login_verified(client_a, principal)
    r = client_a.post(url(arm_b.pk))
    assert r.status_code == 404
    with schema_context("school_b"):
        result_b.refresh_from_db()
        assert result_b.status == "reviewed" and result_b.approved_at is None
