from unittest import mock

import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.results.models import AssessmentComponent, GradeBand
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified
from tests.results.factories import reseed

pytestmark = pytest.mark.django_db

URL = "results:settings"


@pytest.fixture(autouse=True)
def _state(school_a):
    saved = school_a.levels_offered
    school_a.levels_offered = ["jss", "sss"]
    reseed(school_a)
    with schema_context("school_a"):
        ClassArm.objects.all().delete()
        ClassLevel.objects.all().delete()
    yield
    school_a.levels_offered = saved
    with schema_context("school_a"):
        AssessmentComponent.objects.all().delete()
        GradeBand.objects.all().delete()


def _user(role):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, [role], by=u)
    return u


def _login(client, role):
    u = _user(role)
    if role in ("school_admin", "principal"):
        login_verified(client, u)
    else:
        with schema_context("school_a"):
            client.force_login(u)
    return u


def _comp_post(section, rows, kind="components"):
    data = {"section": section, "kind": kind}
    p = "c-" if kind == "components" else "b-"
    data[f"{p}{section}-TOTAL_FORMS"] = len(rows)
    data[f"{p}{section}-INITIAL_FORMS"] = 0
    data[f"{p}{section}-MIN_NUM_FORMS"] = 0
    data[f"{p}{section}-MAX_NUM_FORMS"] = 1000
    for i, row in enumerate(rows):
        for k, v in row.items():
            data[f"{p}{section}-{i}-{k}"] = v
    return data


def _bands(*rows):
    return [{"lower": lo, "upper": up, "grade": g, "remark": r} for lo, up, g, r in rows]


def test_roles(client_a):
    _login(client_a, "teacher")
    assert client_a.get(reverse(URL)).status_code == 403
    client_a.logout()
    _login(client_a, "principal")
    r = client_a.get(reverse(URL))
    assert r.status_code == 200 and b"Save components" not in r.content
    assert client_a.post(reverse(URL), _comp_post("jss", [])).status_code == 403
    client_a.logout()
    _login(client_a, "proprietor")
    assert client_a.get(reverse(URL)).status_code == 200
    client_a.logout()
    _login(client_a, "school_admin")
    r = client_a.get(reverse(URL))
    assert b"Save components" in r.content and b"Senior Secondary" in r.content


def test_levels_drive_panels(client_a):
    with schema_context("school_a"):
        ClassLevel.objects.create(name="JSS 1", section="jss", order=1)
    _login(client_a, "school_admin")
    r = client_a.get(reverse(URL))
    assert b"Junior Secondary" in r.content and b"Senior Secondary" not in r.content


def test_all_sections_when_no_levels(client_a):
    _login(client_a, "school_admin")
    r = client_a.get(reverse(URL))
    for label in (b"Nursery", b"Primary", b"Junior Secondary", b"Senior Secondary"):
        assert label in r.content


def test_save_components_and_audit(client_a):
    _login(client_a, "school_admin")
    rows = [
        {"name": "CA", "max_score": 40, "order": 1},
        {"name": "Exam", "max_score": 60, "order": 2},
    ]
    r = client_a.post(reverse(URL), _comp_post("jss", rows))
    assert r.status_code == 302
    with schema_context("school_a"):
        jss = AssessmentComponent.objects.filter(section="jss")
        assert [(c.name, c.max_score) for c in jss] == [
            ("CA", 40), ("Exam", 60),
        ]  # fmt: skip
        assert AssessmentComponent.objects.filter(section="sss").count() == 5
        ev = AuditEvent.objects.get(action="results.components_changed")
        assert ev.after == {"section": "jss", "items": [["CA", 40], ["Exam", 60]]}


def test_incomplete_total_warns_but_saves(client_a):
    _login(client_a, "school_admin")
    rows = [
        {"name": "CA", "max_score": 10, "order": 1},
        {"name": "Exam", "max_score": 60, "order": 2},
    ]
    assert client_a.post(reverse(URL), _comp_post("jss", rows)).status_code == 302
    r = client_a.get(reverse(URL))
    assert b"not 100" in r.content


def test_duplicate_component_names_refused(client_a):
    _login(client_a, "school_admin")
    rows = [
        {"name": "CA", "max_score": 40, "order": 1},
        {"name": "ca", "max_score": 60, "order": 2},
    ]
    r = client_a.post(reverse(URL), _comp_post("jss", rows))
    assert r.status_code == 200
    with schema_context("school_a"):
        assert AssessmentComponent.objects.filter(section="jss").count() == 5


def test_components_refused_when_scores_exist(client_a):
    _login(client_a, "school_admin")
    rows = [{"name": "Exam", "max_score": 100, "order": 1}]
    with mock.patch("apps.results.scope.has_scores", return_value=True):
        r = client_a.post(reverse(URL), _comp_post("jss", rows))
        assert r.status_code == 200
        assert b"can no longer be changed" in r.content
    with schema_context("school_a"):
        assert AssessmentComponent.objects.filter(section="jss").count() == 5
        assert not AuditEvent.objects.filter(action="results.components_changed").exists()


def test_save_scale_and_audit(client_a):
    _login(client_a, "school_admin")
    rows = _bands((50, 100, "P", "Pass"), (0, 49, "F", "Fail"))
    assert client_a.post(reverse(URL), _comp_post("jss", rows, "scale")).status_code == 302
    with schema_context("school_a"):
        assert [(b.grade, b.lower, b.upper) for b in GradeBand.objects.filter(section="jss")] == [
            ("P", 50, 100), ("F", 0, 49),
        ]  # fmt: skip
        ev = AuditEvent.objects.get(action="results.scale_changed")
        assert ev.after == {"section": "jss", "items": [[50, 100, "P"], [0, 49, "F"]]}


def test_bad_scale_refused_and_unchanged(client_a):
    _login(client_a, "school_admin")
    rows = _bands((51, 100, "P", ""), (0, 49, "F", ""))  # gap at 50
    r = client_a.post(reverse(URL), _comp_post("jss", rows, "scale"))
    assert r.status_code == 200 and b"gap" in r.content
    with schema_context("school_a"):
        assert GradeBand.objects.filter(section="jss").count() == 6
