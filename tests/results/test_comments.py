import pytest
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import FormTeacher
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.results.models import ClassResult, Rating, ReportComment, Trait
from apps.results.seeding import DEFAULT_TRAITS, seed_results
from tests.accounts.factories import StaffFactory, StudentFactory
from tests.accounts.helpers import login_verified
from tests.results.factories import make_sheet_world

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(school_a):
    with schema_context("school_a"):
        Trait.objects.all().delete()
        seed_results(school_a)
        world = make_sheet_world()
        ft = StaffFactory()
        set_roles(ft, ["teacher"], by=None)
        FormTeacher.objects.create(arm=world.arm, session=world.session, teacher=ft)
        world.ft = ft
        yield world


def url(w):
    return reverse("results:class_comments", args=[w.arm.pk])


def post_data(w, **extra):
    traits = list(Trait.objects.all())
    data = {"days_open": "100"}
    for i, s in enumerate(w.students):
        data[f"ct_{s.pk}"] = f"Good work {i}"
        data[f"dp_{s.pk}"] = str(90 + i)
        data[f"rt_{s.pk}_{traits[0].pk}"] = "4"
        data[f"rt_{s.pk}_{traits[6].pk}"] = "5"
    data.update(extra)
    return data


def principal():
    p = StaffFactory()
    set_roles(p, ["principal"], by=None)
    return p


def test_traits_seeded_once(school_a):
    with schema_context("school_a"):
        Trait.objects.all().delete()
        seed_results(school_a)
        assert [(t.kind, t.name) for t in Trait.objects.all()] == DEFAULT_TRAITS
        Trait.objects.filter(name="Sports").delete()
        seed_results(school_a)
        assert Trait.objects.count() == len(DEFAULT_TRAITS) - 1
        Trait.objects.all().delete()
        seed_results(school_a)
        assert Trait.objects.count() == len(DEFAULT_TRAITS)


def test_form_teacher_saves(client_a, w):
    client_a.force_login(w.ft)
    r = client_a.get(url(w))
    assert r.status_code == 200
    assert "Principal comment" not in r.content.decode()
    r = client_a.post(url(w), post_data(w))
    assert r.status_code == 302
    for i, s in enumerate(w.students):
        c = ReportComment.objects.get(student=s, term=w.term)
        assert (c.class_teacher_comment, c.days_present, c.days_open) == (
            f"Good work {i}", 90 + i, 100,
        )  # fmt: skip
        assert Rating.objects.filter(student=s, term=w.term).count() == 2
    assert ClassResult.objects.get(arm=w.arm, term=w.term).status == "draft"
    ids = sorted(s.pk for s in w.students)
    ev = AuditEvent.objects.get(action="results.comment_changed")
    assert sorted(ev.after["student_ids"]) == ids
    assert AuditEvent.objects.get(action="results.ratings_changed")
    assert "Good work" not in str(AuditEvent.objects.filter(action__startswith="results.").values())
    # rerun unchanged: no new audit; blank rating deletes
    client_a.post(url(w), post_data(w))
    assert AuditEvent.objects.filter(action="results.comment_changed").count() == 1
    t0 = Trait.objects.first()
    client_a.post(url(w), post_data(w, **{f"rt_{w.students[0].pk}_{t0.pk}": ""}))
    assert not Rating.objects.filter(student=w.students[0], trait=t0).exists()
    ev = AuditEvent.objects.filter(action="results.ratings_changed").last()
    assert ev.after["student_ids"] == [w.students[0].pk]


def test_principal_saves_principal_comments(client_a, w):
    p = principal()
    login_verified(client_a, p)
    html = client_a.get(url(w)).content.decode()
    assert "Principal comment" in html and "Class teacher comment" not in html
    data = {f"pc_{s.pk}": "Well done" for s in w.students}
    assert client_a.post(url(w), data).status_code == 302
    assert ReportComment.objects.filter(principal_comment="Well done").count() == 3
    ClassResult.objects.filter(arm=w.arm).update(status="reviewed")
    assert client_a.post(url(w), {f"pc_{w.students[0].pk}": "Again"}).status_code == 302
    ClassResult.objects.filter(arm=w.arm).update(status="approved")
    assert client_a.post(url(w), {f"pc_{w.students[0].pk}": "Late"}).status_code == 409
    assert ReportComment.objects.get(student=w.students[0]).principal_comment == "Again"


def test_both_roles_see_both(client_a, w):
    set_roles(w.ft, ["teacher", "principal"], by=None)
    login_verified(client_a, w.ft)
    html = client_a.get(url(w)).content.decode()
    assert "Principal comment" in html and "Class teacher comment" in html


def test_form_teacher_cannot_write_principal_comment(client_a, w):
    client_a.force_login(w.ft)
    client_a.post(url(w), post_data(w, **{f"pc_{w.students[0].pk}": "sneaky"}))
    assert not ReportComment.objects.filter(principal_comment="sneaky").exists()


def test_forbidden_and_404(client_a, w):
    other = StaffFactory()
    set_roles(other, ["teacher"], by=None)
    client_a.force_login(other)
    assert client_a.get(url(w)).status_code == 403
    assert client_a.post(url(w), post_data(w)).status_code == 403
    assert not ReportComment.objects.exists()
    client_a.force_login(w.teacher)  # subject teacher, not form teacher
    assert client_a.get(url(w)).status_code == 403
    stu = StudentFactory()
    client_a.force_login(stu)
    assert client_a.get(url(w)).status_code == 403
    client_a.force_login(w.ft)
    assert client_a.get(reverse("results:class_comments", args=[999999])).status_code == 404


@pytest.mark.parametrize(
    "extra",
    [
        {"days_open": "300"},
        {"days_open": "abc"},
        {"days_open": "-1"},
        {"days_open": "10"},  # present 90+ > open
        {"ct_BAD": "x"},
        {"ct_999999": "x"},
        {"rt_999999_1": "3"},
        {"ct_FIRST": "x" * 501},
    ],
)
def test_validation_saves_nothing(client_a, w, extra):
    client_a.force_login(w.ft)
    extra = {k.replace("FIRST", str(w.students[0].pk)): v for k, v in extra.items()}
    r = client_a.post(url(w), post_data(w, **extra))
    assert r.status_code == 400
    assert not ReportComment.objects.exists() and not Rating.objects.exists()
    assert not AuditEvent.objects.filter(action="results.comment_changed").exists()


def test_rating_six_and_present_gt_open(client_a, w):
    client_a.force_login(w.ft)
    t0 = Trait.objects.first()
    r = client_a.post(url(w), post_data(w, **{f"rt_{w.students[0].pk}_{t0.pk}": "6"}))
    assert r.status_code == 400 and "Ratings must be 1 to 5" in r.content.decode()
    r = client_a.post(url(w), post_data(w, **{f"dp_{w.students[0].pk}": "101"}))
    assert r.status_code == 400 and "cannot be more than" in r.content.decode()
    assert not ReportComment.objects.exists()


def test_blank_days_allowed_and_comment_trimmed(client_a, w):
    client_a.force_login(w.ft)
    s = w.students[0]
    r = client_a.post(url(w), {"days_open": "", f"dp_{s.pk}": "", f"ct_{s.pk}": "  hello  "})
    assert r.status_code == 302
    c = ReportComment.objects.get(student=s)
    assert (c.class_teacher_comment, c.days_present, c.days_open) == ("hello", None, None)


@pytest.mark.parametrize("status", ["reviewed", "approved", "published"])
def test_form_teacher_edits_refused_after_draft(client_a, w, status):
    ClassResult.objects.create(arm=w.arm, term=w.term, status=status)
    client_a.force_login(w.ft)
    r = client_a.post(url(w), post_data(w))
    assert r.status_code == 409 and "moved on" in r.content.decode()
    assert not ReportComment.objects.exists()


def test_xss_escaped(client_a, w):
    client_a.force_login(w.ft)
    s = w.students[0]
    client_a.post(url(w), {"days_open": "", f"ct_{s.pk}": "<script>alert(1)</script>"})
    html = client_a.get(url(w)).content.decode()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_withdrawn_student_not_listed_or_accepted(client_a, w):
    client_a.force_login(w.ft)
    r = client_a.post(url(w), {f"ct_{w.withdrawn.pk}": "x"})
    assert r.status_code == 400


def test_links(client_a, w):
    client_a.force_login(w.ft)
    assert url(w) in client_a.get(reverse("results:my_sheets")).content.decode()
