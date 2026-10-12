import json
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.academics.models import Subject, SubjectOffering, Term
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.results.models import Score, ScoreSheet
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified
from tests.results.factories import make_sheet_world

pytestmark = pytest.mark.django_db

URL = "/api/v1/scores"


@pytest.fixture
def g(school_a):
    with schema_context("school_a"):
        yield make_sheet_world()


def url(g, term=True):
    q = f"{URL}?offering={g.offering.pk}"
    return f"{q}&term={g.term.pk}" if term else q


def put(client, target, cells):
    return client.put(target, data=json.dumps({"cells": cells}), content_type="application/json")


def c(student, comp, value, version=0):
    return {"student": student.pk, "component": comp.pk, "value": value, "version": version}


def as_teacher(client, g):
    with schema_context("school_a"):
        client.force_login(g.teacher)


def scores(g):
    with schema_context("school_a"):
        return {
            (s.student_id, s.component_id): (s.value, s.version)
            for s in Score.objects.filter(offering=g.offering, term=g.term)
        }


def test_get_lists_takers_with_blank_cells(client_a, g):
    as_teacher(client_a, g)
    for target in (url(g), url(g, term=False)):  # term defaults to the current term
        r = client_a.get(target)
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["offering"] == g.offering.pk and body["term"] == g.term.pk
        assert body["editable"] is True
        assert body["sheet_status"] == "draft" and body["class_status"] == "draft"
        assert [x["id"] for x in body["components"]] == [x.pk for x in g.comps]
        assert body["components"][0] == {
            "id": g.comps[0].pk,
            "name": "CA1",
            "max_score": 10,
            "order": 1,
        }
        assert {row["student_id"] for row in body["rows"]} == {s.pk for s in g.students}
        row = body["rows"][0]
        assert set(row) == {"student_id", "name", "admission_number", "cells"}
        for row in body["rows"]:
            assert all(x["value"] is None and x["version"] == 0 for x in row["cells"])
    text = r.content.decode()
    assert "date_of_birth" not in text and "guardian" not in text


def test_put_three_cells(client_a, g):
    as_teacher(client_a, g)
    s, k = g.students, g.comps
    r = put(client_a, url(g), [c(s[0], k[0], "7.5"), c(s[1], k[0], 10), c(s[2], k[4], 59.5)])
    assert r.status_code == 200, r.content
    assert sorted((v["student"], v["component"], v["version"]) for v in r.json()["versions"]) == (
        sorted([(s[0].pk, k[0].pk, 1), (s[1].pk, k[0].pk, 1), (s[2].pk, k[4].pk, 1)])
    )
    assert scores(g) == {
        (s[0].pk, k[0].pk): (Decimal("7.5"), 1),
        (s[1].pk, k[0].pk): (Decimal("10"), 1),
        (s[2].pk, k[4].pk): (Decimal("59.5"), 1),
    }
    got = client_a.get(url(g)).json()
    first = next(row for row in got["rows"] if row["student_id"] == s[0].pk)
    assert first["cells"][0] == {"component_id": k[0].pk, "value": "7.5", "version": 1}


def test_two_clients_second_gets_conflict(client_a, g, school_a):
    from django_tenants.test.client import TenantClient

    s, k = g.students, g.comps
    as_teacher(client_a, g)
    assert put(client_a, url(g), [c(s[0], k[0], "5")]).status_code == 200
    other = TenantClient(school_a)
    as_teacher(other, g)
    # Both read version 1.
    assert put(client_a, url(g), [c(s[0], k[0], "6", 1)]).json()["versions"][0]["version"] == 2
    r = put(other, url(g), [c(s[0], k[0], "9", 1), c(s[1], k[1], "3")])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "VERSION_CONFLICT"
    assert err["details"] == [
        {"student": s[0].pk, "component": k[0].pk, "value": "6.0", "version": 2}
    ]
    assert scores(g) == {(s[0].pk, k[0].pk): (Decimal("6"), 2)}


def test_bad_cells_400_and_nothing_saved(client_a, g):
    as_teacher(client_a, g)
    s, k = g.students, g.comps
    cells = [
        c(s[0], k[0], "11"),
        c(s[0], k[1], "-1"),
        c(s[1], k[0], "abc"),
        c(s[1], k[1], "7.25"),
        c(s[2], k[0], "8"),
    ]
    r = put(client_a, url(g), cells)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert {d["field"] for d in err["details"]} == {
        f"{s[0].pk}:{k[0].pk}",
        f"{s[0].pk}:{k[1].pk}",
        f"{s[1].pk}:{k[0].pk}",
        f"{s[1].pk}:{k[1].pk}",
    }
    assert len(err["details"]) == 4
    assert scores(g) == {}


def test_json_float_with_two_decimals_rejected(client_a, g):
    as_teacher(client_a, g)
    r = put(client_a, url(g), [c(g.students[0], g.comps[0], 7.25)])
    assert r.status_code == 400
    assert scores(g) == {}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"cells": "x"},
        {"cells": [1]},
        {"cells": [{"student": 1, "component": 1, "value": "5"}]},
        {"cells": [{"student": "1x", "component": 1, "value": "5", "version": 0}]},
        {"cells": [{"student": 1, "component": 1, "value": "5", "version": -1}]},
        {"cells": [{"student": 1, "component": 1, "value": "5", "version": True}]},
        {"cells": [{"student": 1, "component": 1, "value": {"a": 1}, "version": 0}]},
        {"cells": [{"student": 1, "component": 1, "value": True, "version": 0}]},
        [],
    ],
)
def test_malformed_body_400(client_a, g, body):
    as_teacher(client_a, g)
    r = client_a.put(url(g), data=json.dumps(body), content_type="application/json")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_clear_cell_with_empty_and_null(client_a, g):
    as_teacher(client_a, g)
    st, comp = g.students[0], g.comps[0]
    put(client_a, url(g), [c(st, comp, "5")])
    r = put(client_a, url(g), [c(st, comp, None, 1)])
    assert r.status_code == 200
    assert scores(g) == {(st.pk, comp.pk): (None, 2)}


def _staff(role, client):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, [role], by=None)
    if role in ("school_admin", "principal"):
        login_verified(client, u)
    else:
        with schema_context("school_a"):
            client.force_login(u)
    return u


@pytest.mark.parametrize("role", ["teacher", "principal", "school_admin", "proprietor"])
def test_other_staff_forbidden(client_a, g, role):
    _staff(role, client_a)
    assert client_a.get(url(g)).status_code == 403
    r = put(client_a, url(g), [c(g.students[0], g.comps[0], "5")])
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"
    assert scores(g) == {}


def test_student_forbidden(client_a, g):
    with schema_context("school_a"):
        client_a.force_login(g.students[0].user)
    assert client_a.get(url(g)).status_code == 403
    assert put(client_a, url(g), [c(g.students[0], g.comps[0], "5")]).status_code == 403
    assert scores(g) == {}


def test_anonymous_refused(client_a, g):
    assert client_a.get(url(g)).status_code in (401, 403)


def test_unknown_offering_404_and_bad_ids_400(client_a, g):
    as_teacher(client_a, g)
    assert client_a.get(f"{URL}?offering=999999999&term={g.term.pk}").status_code == 404
    for bad in ("abc", "²", "9" * 5000, ""):
        r = client_a.get(f"{URL}?offering={bad}&term={g.term.pk}")
        assert r.status_code == 400, bad
    assert client_a.get(f"{URL}?term={g.term.pk}").status_code == 400
    r = client_a.get(f"{URL}?offering={g.offering.pk}&term=x")
    assert r.status_code == 400
    r = client_a.get(f"{URL}?offering={g.offering.pk}&term=999999999")
    assert r.status_code == 400


def test_term_from_other_session_400(client_a, g):
    from datetime import date

    from apps.academics.models import AcademicSession

    with schema_context("school_a"):
        nxt = AcademicSession.objects.create(
            name="2027/2028", start_date=date(2027, 9, 1), end_date=date(2028, 7, 31)
        )
        t = Term.objects.create(
            session=nxt, number=1, start_date=date(2027, 9, 1), end_date=date(2027, 12, 1)
        )
    as_teacher(client_a, g)
    target = f"{URL}?offering={g.offering.pk}&term={t.pk}"
    assert client_a.get(target).status_code == 400
    assert put(client_a, target, [c(g.students[0], g.comps[0], "5")]).status_code == 400
    assert scores(g) == {}


def test_no_current_term_400(client_a, g):
    with schema_context("school_a"):
        Term.objects.update(is_current=False)
    as_teacher(client_a, g)
    r = client_a.get(url(g, term=False))
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "Set a current term first"


def test_submitted_sheet_409_and_audited(client_a, g):
    as_teacher(client_a, g)
    st, comp = g.students[0], g.comps[0]
    put(client_a, url(g), [c(st, comp, "5")])
    with schema_context("school_a"):
        ScoreSheet.objects.filter(offering=g.offering, term=g.term).update(status="submitted")
    r = put(client_a, url(g), [c(st, comp, "9", 1)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESULTS_LOCKED"
    assert scores(g) == {(st.pk, comp.pk): (Decimal("5"), 1)}
    with schema_context("school_a"):
        ev = AuditEvent.objects.get(action="results.edit_refused")
        assert ev.after == {"offering_id": g.offering.pk, "term_id": g.term.pk}
    assert client_a.get(url(g)).json()["editable"] is False


def test_withdrawn_student_not_listed_and_save_refused(client_a, g):
    as_teacher(client_a, g)
    body = client_a.get(url(g)).json()
    assert g.withdrawn.pk not in {row["student_id"] for row in body["rows"]}
    r = put(client_a, url(g), [c(g.withdrawn, g.comps[0], "5")])
    assert r.status_code == 400
    assert r.json()["error"]["details"][0]["field"] == f"{g.withdrawn.pk}:{g.comps[0].pk}"


# --- cross-school: a teacher assigned in school A asks for school B's offering --------------


def test_teacher_in_a_cannot_reach_school_b_scores(client_a, school_b, g):
    with schema_context("school_b"):
        wb = make_sheet_world()
        b_score = Score.objects.create(
            student=wb.students[0],
            offering=wb.offering,
            term=wb.term,
            component=wb.comps[0],
            value=Decimal("4"),
            version=1,
            entered_by=wb.teacher,
        )
        b_count = Score.objects.count()
    with schema_context("school_a"):
        # Make sure school A's teacher is not assigned to an A offering whose id equals B's.
        if g.offering.pk == wb.offering.pk:
            subj = Subject.objects.create(name="Basic Science", code="BSC", sections=["jss"])
            mine = SubjectOffering.objects.create(
                subject=subj, arm=g.arm, session=g.session, teacher=g.teacher
            )
            SubjectOffering.objects.filter(pk=g.offering.pk).update(teacher=None)
            assert mine.pk != wb.offering.pk
    as_teacher(client_a, g)
    target = f"{URL}?offering={wb.offering.pk}&term={wb.term.pk}"
    r = client_a.get(target)
    assert r.status_code in (403, 404)
    assert wb.students[0].admission_number not in r.content.decode()
    r = put(
        client_a,
        target,
        [
            {"student": wb.students[0].pk, "component": wb.comps[0].pk, "value": "9", "version": 1},
            {"student": wb.students[1].pk, "component": wb.comps[0].pk, "value": "9", "version": 0},
        ],
    )
    assert r.status_code in (403, 404)
    with schema_context("school_b"):
        b_score.refresh_from_db()
        assert (b_score.value, b_score.version) == (Decimal("4"), 1)
        assert Score.objects.count() == b_count
