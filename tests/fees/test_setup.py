import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from apps.fees import services
from apps.fees.models import (
    Discount,
    FeeItem,
    FeeSettings,
    FeeStructure,
    FeeStructureLine,
    StudentDiscount,
)
from apps.fees.scope import can_manage_fees, can_view_fees
from tests.accounts.helpers import login_verified
from tests.fees.factories import (
    make_fixed_discount,
    make_item,
    make_percent_discount,
    make_staff,
    make_structure,
    make_terms,
)
from tests.students.factories import StudentProfileFactory

pytestmark = pytest.mark.django_db


def _login(client, role):
    with schema_context("school_a"):
        user = make_staff(role)
    if role in ("school_admin", "principal", "bursar"):
        login_verified(client, user)
    else:
        with schema_context("school_a"):
            client.force_login(user)
    return user


def _events(action):
    with schema_context("school_a"):
        return list(AuditEvent.objects.filter(action=action))


def _assert_ids_only(event):
    blob = str(event.before) + str(event.after)
    for forbidden in ("Tuition", "Staff child", "Scholarship", "@", "+234"):
        assert forbidden not in blob


# --- roles ---------------------------------------------------------------


def test_setup_routes_forbidden_for_teacher(client_a):
    _login(client_a, "teacher")
    with schema_context("school_a"):
        w = make_terms()
        student = StudentProfileFactory()
    urls = [
        reverse("fees:settings"),
        reverse("fees:items"),
        reverse("fees:structures"),
        reverse("fees:structure_edit", args=[w.level.pk, w.term1.pk]),
        reverse("fees:discounts"),
        reverse("fees:student_discounts", args=[student.pk]),
    ]
    for url in urls:
        assert client_a.get(url).status_code == 403, url
        assert client_a.post(url, {}).status_code == 403, url


def test_proprietor_reads_but_cannot_write(client_a):
    _login(client_a, "proprietor")
    with schema_context("school_a"):
        w = make_terms()
        student = StudentProfileFactory()
    urls = [
        reverse("fees:settings"),
        reverse("fees:items"),
        reverse("fees:structures"),
        reverse("fees:structure_edit", args=[w.level.pk, w.term1.pk]),
        reverse("fees:discounts"),
        reverse("fees:student_discounts", args=[student.pk]),
    ]
    for url in urls:
        assert client_a.get(url).status_code == 200, url
        assert client_a.post(url, {"name": "x"}).status_code == 403, url
    with schema_context("school_a"):
        assert not FeeItem.objects.exists()


def test_scope_helpers(school_a):
    with schema_context("school_a"):
        bursar, admin, owner, teacher, principal = (
            make_staff(r) for r in ("bursar", "school_admin", "proprietor", "teacher", "principal")
        )
        assert can_manage_fees(bursar) and can_manage_fees(admin)
        assert not can_manage_fees(owner) and not can_manage_fees(teacher)
        assert can_view_fees(bursar) and can_view_fees(admin) and can_view_fees(owner)
        assert not can_view_fees(teacher) and not can_view_fees(principal)


# --- the bursar's flow -----------------------------------------------------


def test_bursar_sets_up_fees_end_to_end(client_a):
    _login(client_a, "bursar")
    with schema_context("school_a"):
        w = make_terms()
        student = StudentProfileFactory()

    # items
    for name in ("Tuition", "Uniform"):
        r = client_a.post(reverse("fees:items"), {"name": name, "description": ""})
        assert r.status_code == 302
    with schema_context("school_a"):
        tuition = FeeItem.objects.get(name="Tuition")
        uniform = FeeItem.objects.get(name="Uniform")
    assert len(_events("fees.item_saved")) == 2
    _assert_ids_only(_events("fees.item_saved")[0])

    # structure with two lines, one optional
    url = reverse("fees:structure_edit", args=[w.level.pk, w.term1.pk])
    r = client_a.post(
        url,
        {
            f"amount_{tuition.pk}": "150000",
            f"amount_{uniform.pk}": "12,500.50".replace(",", ""),
            f"optional_{uniform.pk}": "on",
        },
    )
    assert r.status_code == 302, r.content.decode()[:500]
    with schema_context("school_a"):
        structure = FeeStructure.objects.get(level=w.level, term=w.term1)
        lines = {line.item_id: line for line in structure.lines.all()}
        assert lines[tuition.pk].amount_kobo == 15_000_000
        assert not lines[tuition.pk].is_optional
        assert lines[uniform.pk].amount_kobo == 1_250_050
        assert lines[uniform.pk].is_optional
    events = _events("fees.structure_saved")
    assert len(events) == 1
    assert events[0].object_id == str(structure.pk)
    _assert_ids_only(events[0])

    # discounts
    r = client_a.post(
        reverse("fees:discounts"), {"name": "Staff child", "kind": "percent", "percent": "10"}
    )
    assert r.status_code == 302
    r = client_a.post(
        reverse("fees:discounts"), {"name": "Scholarship", "kind": "fixed", "amount": "5000"}
    )
    assert r.status_code == 302
    with schema_context("school_a"):
        pct = Discount.objects.get(name="Staff child")
        fixed = Discount.objects.get(name="Scholarship")
        assert (pct.percent, pct.amount_kobo) == (10, None)
        assert (fixed.percent, fixed.amount_kobo) == (None, 500_000)
    assert len(_events("fees.discount_saved")) == 2
    _assert_ids_only(_events("fees.discount_saved")[0])

    # assign to a student
    r = client_a.post(
        reverse("fees:student_discounts", args=[student.pk]),
        {"discount": pct.pk, "session": w.session.pk},
    )
    assert r.status_code == 302
    with schema_context("school_a"):
        sd = StudentDiscount.objects.get(student=student)
        assert (sd.discount_id, sd.session_id) == (pct.pk, w.session.pk)
    events = _events("fees.student_discount_set")
    assert len(events) == 1
    _assert_ids_only(events[0])
    assert client_a.get(reverse("fees:student_discounts", args=[student.pk])).status_code == 200

    # remove: a malformed or unknown row id is a 404 (Task 1 minor); a real one is removed
    url = reverse("fees:student_discounts", args=[student.pk])
    for bad in ("abc", "", "-1", "999999"):
        assert client_a.post(url, {"remove": bad}).status_code == 404, bad
    assert client_a.post(url, {"remove": sd.pk}).status_code == 302
    with schema_context("school_a"):
        assert not StudentDiscount.objects.filter(student=student).exists()


def test_structure_edit_removes_blank_lines(client_a):
    _login(client_a, "bursar")
    with schema_context("school_a"):
        w = make_terms()
        tuition, uniform = make_item("Tuition"), make_item("Uniform")
        make_structure(w.level, w.term1, [(tuition, 100_000, False), (uniform, 5_000, True)])
    url = reverse("fees:structure_edit", args=[w.level.pk, w.term1.pk])
    assert client_a.get(url).status_code == 200
    client_a.post(url, {f"amount_{tuition.pk}": "2000", f"amount_{uniform.pk}": ""})
    with schema_context("school_a"):
        lines = FeeStructureLine.objects.all()
        assert [(line.item_id, line.amount_kobo) for line in lines] == [(tuition.pk, 200_000)]


def test_bad_line_amounts_give_form_errors(client_a):
    _login(client_a, "bursar")
    with schema_context("school_a"):
        w = make_terms()
        tuition = make_item("Tuition")
    url = reverse("fees:structure_edit", args=[w.level.pk, w.term1.pk])
    for bad in ("12.345", "-5", "abc", "100000000.01"):
        r = client_a.post(url, {f"amount_{tuition.pk}": bad})
        assert r.status_code == 200, bad
        assert b"error" in r.content, bad
    with schema_context("school_a"):
        assert not FeeStructureLine.objects.exists()
    assert not _events("fees.structure_saved")


def test_foreign_ids_are_404(client_a):
    _login(client_a, "bursar")
    with schema_context("school_a"):
        w = make_terms()
    assert (
        client_a.get(reverse("fees:structure_edit", args=[w.level.pk, 999999])).status_code == 404
    )
    assert (
        client_a.get(reverse("fees:structure_edit", args=[999999, w.term1.pk])).status_code == 404
    )
    assert client_a.get(reverse("fees:student_discounts", args=[999999])).status_code == 404


def test_duplicate_item_name_is_a_form_error(client_a):
    _login(client_a, "bursar")
    with schema_context("school_a"):
        make_item("Tuition")
    r = client_a.post(reverse("fees:items"), {"name": "tuition", "description": ""})
    assert r.status_code == 200
    assert b"already exists" in r.content
    r = client_a.post(reverse("fees:items"), {"name": "Tuition", "description": ""})
    assert r.status_code == 200
    with schema_context("school_a"):
        assert FeeItem.objects.count() == 1


def test_discount_form_requires_exactly_one_value(client_a):
    _login(client_a, "bursar")
    bad = [
        {"name": "A", "kind": "percent", "percent": "0"},
        {"name": "A", "kind": "percent", "percent": "101"},
        {"name": "A", "kind": "percent", "percent": ""},
        {"name": "A", "kind": "fixed", "amount": ""},
        {"name": "A", "kind": "fixed", "amount": "10.555"},
    ]
    for data in bad:
        assert client_a.post(reverse("fees:discounts"), data).status_code == 200, data
    with schema_context("school_a"):
        assert not Discount.objects.exists()


def test_fixed_discount_is_listed_in_naira(client_a):
    _login(client_a, "bursar")
    with schema_context("school_a"):
        make_fixed_discount("Scholarship", 500_000)
    body = client_a.get(reverse("fees:discounts")).content.decode()
    assert "₦5,000.00" in body and "kobo" not in body


def test_settings_page_saves_and_audits(client_a):
    _login(client_a, "school_admin")
    r = client_a.get(reverse("fees:settings"))
    assert r.status_code == 200
    r = client_a.post(
        reverse("fees:settings"),
        {
            "min_part_payment": "1000",
            "manual_approval_threshold": "20000",
            "block_results_when_owing": "on",
            "bank_transfer_note": "Pay into the school account.",
        },
    )
    assert r.status_code == 302
    with schema_context("school_a"):
        conf = FeeSettings.load()
        assert conf.allow_part_payment is False  # unchecked box
        assert conf.min_part_payment_kobo == 100_000
        assert conf.manual_approval_threshold_kobo == 2_000_000
        assert conf.block_results_when_owing is True
    assert len(_events("fees.settings_saved")) == 1
    # Saving the same values again writes no audit event.
    same = {
        "min_part_payment": "1000",
        "manual_approval_threshold": "20000",
        "block_results_when_owing": "on",
        "bank_transfer_note": "Pay into the school account.",
    }
    assert client_a.post(reverse("fees:settings"), same).status_code == 302
    assert len(_events("fees.settings_saved")) == 1
    with schema_context("school_a"):
        FeeSettings.objects.all().delete()


# --- models and services --------------------------------------------------


def test_settings_defaults_and_singleton(school_a):
    with schema_context("school_a"):
        conf = FeeSettings.load()
        assert conf.pk == 1
        assert conf.allow_part_payment is True
        assert conf.min_part_payment_kobo == 0
        assert conf.manual_approval_threshold_kobo == 5_000_000
        assert conf.block_results_when_owing is False
        assert conf.bank_transfer_note == ""
        assert FeeSettings.load().pk == 1
        assert FeeSettings.objects.count() == 1


def test_discount_check_constraints(school_a):
    with schema_context("school_a"):
        for kwargs in (
            {"kind": "percent", "percent": 0},
            {"kind": "percent", "percent": 101},
            {"kind": "percent", "percent": 10, "amount_kobo": 5},
            {"kind": "percent"},
            {"kind": "fixed"},
            {"kind": "fixed", "amount_kobo": 5, "percent": 10},
            {"kind": "fixed", "amount_kobo": -1},
        ):
            with pytest.raises(IntegrityError), transaction.atomic():
                Discount.objects.create(name=f"d{kwargs}", **kwargs)


def test_line_amount_and_uniqueness_constraints(school_a):
    with schema_context("school_a"):
        w = make_terms()
        item = make_item()
        structure = make_structure(w.level, w.term1, [])
        with pytest.raises(IntegrityError), transaction.atomic():
            FeeStructureLine.objects.create(structure=structure, item=item, amount_kobo=-1)
        FeeStructureLine.objects.create(structure=structure, item=item, amount_kobo=1)
        with pytest.raises(IntegrityError), transaction.atomic():
            FeeStructureLine.objects.create(structure=structure, item=item, amount_kobo=2)
        with pytest.raises(IntegrityError), transaction.atomic():
            FeeStructure.objects.create(level=w.level, term=w.term1)


def test_student_discount_unique(school_a):
    with schema_context("school_a"):
        w = make_terms()
        student = StudentProfileFactory()
        d = make_fixed_discount()
        StudentDiscount.objects.create(student=student, discount=d, session=w.session)
        with pytest.raises(IntegrityError), transaction.atomic():
            StudentDiscount.objects.create(student=student, discount=d, session=w.session)
        other = make_percent_discount()
        StudentDiscount.objects.create(student=student, discount=other, session=w.session)


def test_copy_structures_copies_and_skips_existing(school_a):
    with schema_context("school_a"):
        from apps.academics.models import ClassLevel

        w = make_terms()
        jss2 = ClassLevel.objects.create(name="JSS 2", section="jss", order=10)
        tuition, uniform = make_item("Tuition"), make_item("Uniform")
        make_structure(w.level, w.term1, [(tuition, 100_000, False), (uniform, 5_000, True)])
        make_structure(jss2, w.term1, [(tuition, 120_000, False)])
        make_structure(jss2, w.term2, [(tuition, 999, False)])  # already there: untouched
        user = make_staff("bursar")

        assert services.copy_structures(w.term1, w.term2, user) == 1

        copied = FeeStructure.objects.get(level=w.level, term=w.term2)
        got = {line.item_id: (line.amount_kobo, line.is_optional) for line in copied.lines.all()}
        assert got == {tuition.pk: (100_000, False), uniform.pk: (5_000, True)}
        existing = FeeStructure.objects.get(level=jss2, term=w.term2)
        assert [line.amount_kobo for line in existing.lines.all()] == [999]
        assert FeeStructure.objects.count() == 4
        assert services.copy_structures(w.term1, w.term2, user) == 0
        events = AuditEvent.objects.filter(action="fees.structure_saved")
        assert events.count() == 1
        assert events[0].object_id == str(copied.pk)


def test_copy_structures_same_term_rejected(school_a):
    with schema_context("school_a"):
        w = make_terms()
        with pytest.raises(ValidationError):
            services.copy_structures(w.term1, w.term1, make_staff("bursar"))


def test_copy_structures_view(client_a):
    _login(client_a, "bursar")
    with schema_context("school_a"):
        w = make_terms()
        make_structure(w.level, w.term1, [(make_item(), 100_000, False)])
    r = client_a.post(
        reverse("fees:structures"), {"copy": "1", "from_term": w.term1.pk, "to_term": w.term2.pk}
    )
    assert r.status_code == 302
    with schema_context("school_a"):
        assert FeeStructure.objects.filter(term=w.term2).count() == 1


def test_student_detail_shows_fee_discount_link_only_to_fee_managers(client_a):
    with schema_context("school_a"):
        make_terms()
        student = StudentProfileFactory()
    link = reverse("fees:student_discounts", args=[student.pk]).encode()
    detail = reverse("students:detail", args=[student.pk])
    _login(client_a, "bursar")
    assert link in client_a.get(detail).content
    client_a.logout()
    _login(client_a, "proprietor")
    assert link not in client_a.get(detail).content
