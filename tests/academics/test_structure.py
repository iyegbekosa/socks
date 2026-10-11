from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel, Subject
from apps.academics.services import (
    create_arm,
    create_subject,
    delete_arm,
    delete_subject,
    rename_arm,
    update_subject,
)
from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean(school_a):
    def wipe():
        with schema_context("school_a"):
            ClassArm.objects.all().delete()
            ClassLevel.objects.all().delete()
            Subject.objects.all().delete()

    wipe()
    yield
    wipe()


@pytest.fixture
def admin_user(school_a):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["school_admin"], by=u)
    return u


@pytest.fixture
def levels(school_a):
    with schema_context("school_a"):
        return {
            "jss1": ClassLevel.objects.create(name="JSS 1", section="jss", order=1),
            "sss1": ClassLevel.objects.create(name="SSS 1", section="sss", order=1),
        }


def test_stream_on_jss_arm_refused(levels, admin_user):
    with schema_context("school_a"):
        with pytest.raises(ValidationError):
            create_arm(level=levels["jss1"], suffix="A", stream="science", by=admin_user)
        arm = create_arm(level=levels["sss1"], suffix="A", stream="science", by=admin_user)
        assert arm.stream == "science"
        bad = ClassArm(level=levels["jss1"], suffix="B", stream="arts")
        with pytest.raises(ValidationError):
            bad.full_clean()


def test_display_name(levels, admin_user):
    with schema_context("school_a"):
        a = create_arm(level=levels["jss1"], suffix="A", by=admin_user)
        g = create_arm(level=levels["jss1"], suffix="Gold", by=admin_user)
        assert a.display_name == "JSS 1A"
        assert g.display_name == "JSS 1 Gold"


def test_find_by_display(levels, admin_user):
    with schema_context("school_a"):
        a = create_arm(level=levels["jss1"], suffix="A", by=admin_user)
        g = create_arm(level=levels["jss1"], suffix="Gold", by=admin_user)
        for text in ("jss 1a", "JSS1A", "JSS 1 A", "  Jss  1  a "):
            assert ClassArm.find_by_display(text) == a
        assert ClassArm.find_by_display("JSS 1 Gold") == g
        assert ClassArm.find_by_display("jss1gold") == g
        assert ClassArm.find_by_display("JSS 2A") is None
        assert ClassArm.find_by_display("") is None


def test_duplicate_and_ambiguous_refused(levels, admin_user):
    with schema_context("school_a"):
        create_arm(level=levels["jss1"], suffix="A", by=admin_user)
        with pytest.raises(ValidationError):
            create_arm(level=levels["jss1"], suffix="A", by=admin_user)
        with pytest.raises(ValidationError):
            create_arm(level=levels["jss1"], suffix=" A ", by=admin_user)
        with pytest.raises(ValidationError):
            create_arm(level=levels["jss1"], suffix="", by=admin_user)
        # db-level uniqueness
        with pytest.raises(IntegrityError), transaction.atomic():
            ClassArm.objects.create(level=levels["jss1"], suffix="A")
    with schema_context("school_a"):
        # "JSS 1" + "A" collides with a level named "JSS" + arm suffix "1A"
        lvl = ClassLevel.objects.create(name="JSS", section="jss", order=9)
        with pytest.raises(ValidationError):
            create_arm(level=lvl, suffix="1A", by=admin_user)


def test_rename_arm(levels, admin_user):
    with schema_context("school_a"):
        a = create_arm(level=levels["jss1"], suffix="A", by=admin_user)
        b = create_arm(level=levels["jss1"], suffix="B", by=admin_user)
        rename_arm(a, suffix="Gold", by=admin_user)
        a.refresh_from_db()
        assert a.suffix == "Gold"
        with pytest.raises(ValidationError):
            rename_arm(a, suffix="B", by=admin_user)
        rename_arm(b, suffix="B", by=admin_user)  # unchanged is fine


def test_delete_arm_and_refusal(levels, admin_user):
    with schema_context("school_a"):
        a = create_arm(level=levels["jss1"], suffix="A", by=admin_user)
        with mock.patch.object(
            ClassArm, "delete", side_effect=ProtectedError("protected", {object()})
        ):
            with pytest.raises(ValidationError) as exc:
                delete_arm(a, by=admin_user)
        assert "enrolment" in str(exc.value).lower()
        assert ClassArm.objects.filter(pk=a.pk).exists()
        delete_arm(a, by=admin_user)
        assert not ClassArm.objects.filter(pk=a.pk).exists()


def test_subject_crud_and_refusal(admin_user):
    with schema_context("school_a"):
        s = create_subject(name="Further Maths", sections=["sss"], by=admin_user)
        assert s.code == "further-maths"
        with pytest.raises(ValidationError):
            create_subject(name="further maths", sections=["sss"], by=admin_user)
        with pytest.raises(ValidationError):
            create_subject(name="Art", sections=["bogus"], by=admin_user)
        update_subject(s, name="Further Mathematics", sections=["sss", "jss"], by=admin_user)
        s.refresh_from_db()
        assert s.name == "Further Mathematics"
        assert s.code == "further-maths"
        assert set(s.sections) == {"sss", "jss"}
        with mock.patch.object(Subject, "delete", side_effect=ProtectedError("p", {object()})):
            with pytest.raises(ValidationError) as exc:
                delete_subject(s, by=admin_user)
        assert "offering" in str(exc.value).lower()
        delete_subject(s, by=admin_user)
        assert not Subject.objects.exists()


def test_audits(levels, admin_user):
    with schema_context("school_a"):
        before = AuditEvent.objects.filter(action="academics.structure_changed").count()
        a = create_arm(level=levels["jss1"], suffix="A", by=admin_user)
        rename_arm(a, suffix="B", by=admin_user)
        pk = a.pk
        delete_arm(a, by=admin_user)
        s = create_subject(name="Art", sections=["jss"], by=admin_user)
        update_subject(s, name="Arts", sections=["jss"], by=admin_user)
        delete_subject(s, by=admin_user)
        events = list(
            AuditEvent.objects.filter(action="academics.structure_changed").order_by("id")
        )[before:]
        whats = [e.after["what"] for e in events]
        assert whats == [
            "arm_created",
            "arm_renamed",
            "arm_deleted",
            "subject_created",
            "subject_updated",
            "subject_deleted",
        ]
        assert events[0].after["id"] == pk


def test_pages_roles(client_a, school_a, levels):
    with schema_context("school_a"):
        admin = StaffFactory()
        set_roles(admin, ["school_admin"], by=admin)
        prop = StaffFactory()
        set_roles(prop, ["proprietor"], by=admin)
        principal = StaffFactory()
        set_roles(principal, ["principal"], by=admin)
        teacher = StaffFactory()
        set_roles(teacher, ["teacher"], by=admin)
    structure, subjects = reverse("academics:structure"), reverse("academics:subjects")

    with schema_context("school_a"):
        client_a.force_login(teacher)
    assert client_a.get(structure).status_code == 403
    assert client_a.get(subjects).status_code == 403
    client_a.logout()

    with schema_context("school_a"):
        client_a.force_login(prop)
    assert client_a.get(structure).status_code == 200
    assert client_a.get(subjects).status_code == 200
    r = client_a.post(structure, {"level": levels["jss1"].pk, "suffix": "A"})
    assert r.status_code == 403
    r = client_a.post(subjects, {"name": "Art", "sections": ["jss"]})
    assert r.status_code == 403
    with schema_context("school_a"):
        assert not ClassArm.objects.exists()
        assert not Subject.objects.exists()
    client_a.logout()

    with schema_context("school_a"):
        login_verified(client_a, principal)
    assert client_a.get(structure).status_code == 200
    client_a.logout()

    with schema_context("school_a"):
        login_verified(client_a, admin)
    r = client_a.post(structure, {"level": levels["jss1"].pk, "suffix": "A"})
    assert r.status_code == 302
    with schema_context("school_a"):
        arm = ClassArm.objects.get()
    assert "JSS 1A" in client_a.get(structure).content.decode()
    r = client_a.post(reverse("academics:arm_rename", args=[arm.pk]), {"suffix": "Gold"})
    assert r.status_code == 302
    r = client_a.post(subjects, {"name": "Art", "sections": ["jss"]})
    assert r.status_code == 302
    with schema_context("school_a"):
        subj = Subject.objects.get()
    r = client_a.post(
        reverse("academics:subject_edit", args=[subj.pk]),
        {"name": "Arts", "sections": ["jss", "sss"]},
    )
    assert r.status_code == 302
    assert "Arts" in client_a.get(subjects).content.decode()
    assert client_a.post(reverse("academics:subject_delete", args=[subj.pk])).status_code == 302
    assert client_a.post(reverse("academics:arm_delete", args=[arm.pk])).status_code == 302
    with schema_context("school_a"):
        assert not ClassArm.objects.exists()
        assert not Subject.objects.exists()
    # invalid stream shows error, creates nothing
    r = client_a.post(structure, {"level": levels["jss1"].pk, "suffix": "Z", "stream": "science"})
    assert r.status_code == 200
    with schema_context("school_a"):
        assert not ClassArm.objects.exists()


def test_rename_collision_refused(levels, admin_user):
    with schema_context("school_a"):
        create_arm(level=levels["jss1"], suffix="A", by=admin_user)
        lvl = ClassLevel.objects.create(name="JSS", section="jss", order=9)
        other = create_arm(level=lvl, suffix="B", by=admin_user)
        with pytest.raises(ValidationError):
            rename_arm(other, suffix="1A", by=admin_user)
        other.refresh_from_db()
        assert other.suffix == "B"


def test_rejected_edits_show_stored_values(client_a, school_a, levels):
    with schema_context("school_a"):
        admin = StaffFactory()
        set_roles(admin, ["school_admin"], by=admin)
        a = create_arm(level=levels["jss1"], suffix="A", by=admin)
        create_arm(level=levels["jss1"], suffix="B", by=admin)
        s = create_subject(name="Art", sections=["jss"], by=admin)
        create_subject(name="Music", sections=["jss"], by=admin)
        login_verified(client_a, admin)
    r = client_a.post(reverse("academics:arm_rename", args=[a.pk]), {"suffix": "B"})
    assert r.status_code == 200
    body = r.content.decode()
    assert "<h1>Rename JSS 1A</h1>" in body
    assert "JSS 1B</h1>" not in body
    r = client_a.post(
        reverse("academics:subject_edit", args=[s.pk]), {"name": "Music", "sections": ["jss"]}
    )
    assert r.status_code == 200
    assert "<h1>Edit Art</h1>" in r.content.decode()


@pytest.mark.parametrize("role", ["proprietor", "principal"])
def test_read_roles_cannot_mutate(client_a, school_a, levels, role):
    with schema_context("school_a"):
        admin = StaffFactory()
        set_roles(admin, ["school_admin"], by=admin)
        user = StaffFactory()
        set_roles(user, [role], by=admin)
        a = create_arm(level=levels["jss1"], suffix="A", by=admin)
        s = create_subject(name="Art", sections=["jss"], by=admin)
        login_verified(client_a, user)
    post = client_a.post
    assert post(reverse("academics:arm_rename", args=[a.pk]), {"suffix": "Z"}).status_code == 403
    assert post(reverse("academics:arm_delete", args=[a.pk])).status_code == 403
    r = post(reverse("academics:subject_edit", args=[s.pk]), {"name": "Z", "sections": ["sss"]})
    assert r.status_code == 403
    assert post(reverse("academics:subject_delete", args=[s.pk])).status_code == 403
    with schema_context("school_a"):
        a.refresh_from_db()
        s.refresh_from_db()
        assert (a.suffix, s.name, s.sections) == ("A", "Art", ["jss"])
