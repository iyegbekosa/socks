import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.test import RequestFactory
from django_tenants.utils import schema_context

from apps.accounts import roles
from apps.tenancy.seeding import seed_school
from tests.accounts.factories import StaffFactory, StudentFactory

pytestmark = pytest.mark.django_db


def test_seed_idempotent(school_a):
    seed_school(school_a)
    seed_school(school_a)
    with schema_context("school_a"):
        assert set(Group.objects.values_list("name", flat=True)) == set(roles.STAFF_ROLES)
        assert Group.objects.count() == 5


def test_has_role_and_mfa(school_a):
    with schema_context("school_a"):
        u = StaffFactory()
        roles.set_roles(u, [roles.BURSAR], by=None)
        assert u.has_role(roles.BURSAR) and not u.has_role(roles.TEACHER)
        assert u.requires_mfa
        roles.set_roles(u, [roles.TEACHER], by=None)
        assert not u.has_role(roles.BURSAR) and not u.requires_mfa
        roles.set_roles(u, [roles.PROPRIETOR], by=None)
        assert not u.requires_mfa


def test_student_rejects_roles(school_a):
    with schema_context("school_a"):
        s = StudentFactory()
        with pytest.raises(ValidationError):
            roles.set_roles(s, [roles.TEACHER], by=None)
        assert s.is_student


def test_unknown_role(school_a):
    with schema_context("school_a"):
        with pytest.raises(ValueError):
            roles.set_roles(StaffFactory(), ["wizard"], by=None)


def test_require_roles(school_a):
    @roles.require_roles(roles.TEACHER)
    def view(request):
        return HttpResponse("ok")

    rf = RequestFactory()
    with schema_context("school_a"):
        req = rf.get("/x/")
        req.user = AnonymousUser()
        resp = view(req)
        assert resp.status_code == 302 and "next=/x/" in resp["Location"]
        u = StaffFactory()
        req.user = u
        with pytest.raises(PermissionDenied):
            view(req)
        roles.set_roles(u, [roles.TEACHER], by=None)
        req.user = type(u).objects.get(pk=u.pk)
        assert view(req).status_code == 200


def test_has_role_permission(school_a):
    perm = roles.HasRole.with_roles(roles.BURSAR)()
    with schema_context("school_a"):
        u = StaffFactory()
        roles.set_roles(u, [roles.BURSAR], by=None)

        class R:
            user = type(u).objects.get(pk=u.pk)

        assert perm.has_permission(R(), None)
        R.user = AnonymousUser()
        assert not perm.has_permission(R(), None)
