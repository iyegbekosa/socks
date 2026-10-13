"""Siblings (§7.1): other active students sharing a non-empty guardian phone or email."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.utils import schema_context

from apps.fees import checkout, manual
from apps.fees.siblings import siblings_of
from apps.students.models import Student
from tests.fees.test_ledger import _invoice, _world
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


def test_sibling_matching_rules(school_a):
    with schema_context("school_a"):
        me = StudentProfileFactory(
            guardian_phone="+2348030000001",
            guardian_email="Mum@Example.com",
            guardian2_email="",
        )
        same_phone = StudentProfileFactory(guardian_phone="+2348030000001")
        # Main guardian email matches a second guardian's email in another letter case.
        email_case = StudentProfileFactory(
            guardian_phone=None, guardian_email="aunt@example.com", guardian2_name="Mum"
        )
        Student.objects.filter(pk=email_case.pk).update(guardian2_email="MUM@EXAMPLE.COM")
        # Both emails blank, different phone: never matches on the blanks.
        blank = StudentProfileFactory(
            guardian_phone="+2348030000002", guardian_email="", guardian2_email=""
        )
        withdrawn = StudentProfileFactory(
            guardian_phone="+2348030000001", status=Student.Status.WITHDRAWN
        )
        second_phone = StudentProfileFactory(
            guardian_phone="+2348030000003",
            guardian2_name="Dad",
            guardian2_phone="+2348030000001",
        )

        found = siblings_of(me)
        pks = [s.pk for s in found]
        assert set(pks) == {same_phone.pk, email_case.pk, second_phone.pk}
        assert blank.pk not in pks and withdrawn.pk not in pks and me.pk not in pks
        assert pks == [s.pk for s in sorted(found, key=lambda s: s.user.admission_number or "")]


def test_blank_contacts_never_match(school_a):
    with schema_context("school_a"):
        me = StudentProfileFactory(guardian_phone="+2348030000011", guardian_email="")
        StudentProfileFactory(guardian_phone="+2348030000012", guardian_email="")
        StudentProfileFactory(guardian_phone=None, guardian_email="x@example.com")
        assert siblings_of(me) == []
        # Blank second-guardian slots on both sides do not make a match either.
        assert all(not s.guardian2_email for s in Student.objects.all())


def test_siblings_of_is_one_query(school_a):
    with schema_context("school_a"):
        me = StudentProfileFactory(guardian_phone="+2348030000021", guardian_email="a@b.ng")
        StudentProfileFactory(guardian_phone="+2348030000021")
        StudentProfileFactory(guardian_phone=None, guardian_email="a@b.ng")
        with CaptureQueriesContext(connection) as ctx:
            found = siblings_of(me)
            [s.user.admission_number for s in found]
        assert len(found) == 2
        selects = [q for q in ctx.captured_queries if not q["sql"].startswith("SET ")]
        assert len(selects) == 1  # django-tenants may add a SET search_path


def test_real_siblings_are_payable_and_strangers_are_not(school_a):
    with schema_context("school_a"):
        w = _world()
        inv = _invoice(w.student, w.term1, 1_000_000)
        sib = StudentProfileFactory(guardian_phone=w.student.guardian_phone)
        sib_inv = _invoice(sib, w.term1, 500_000)
        other_inv = _invoice(StudentProfileFactory(), w.term1, 400_000)
        payable = {i.pk for i in checkout.payable_invoices(w.student)}
        assert payable == {inv.pk, sib_inv.pk}
        assert other_inv.pk not in payable
        assert manual.payer_and_siblings(w.student) == {w.student.pk, sib.pk}


def test_manual_owner_message_mentions_siblings():
    assert manual.MSG_OWNER == "Choose only invoices of this student or their siblings."
