import io
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django_tenants.utils import schema_context

from apps.results.models import (
    AssessmentComponent,
    GradeBand,
    components_complete,
    components_for,
    grade_for,
    validate_scale,
)
from apps.results.seeding import seed_results
from apps.tenancy.models import School

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean(school_a):
    def wipe():
        with schema_context("school_a"):
            AssessmentComponent.objects.all().delete()
            GradeBand.objects.all().delete()

    wipe()
    yield
    wipe()


def _school(levels):
    return School(name="X", schema_name="school_a", levels_offered=levels)


def test_defaults(school_a):
    with schema_context("school_a"):
        seed_results(_school(["jss", "sss"]))
        for s in ("jss", "sss"):
            assert [(c.name, c.max_score, c.order) for c in components_for(s)] == [
                ("CA1", 10, 1), ("CA2", 10, 2), ("Assignment", 10, 3), ("Test", 10, 4),
                ("Exam", 60, 5),
            ]  # fmt: skip
            assert components_complete(s)
        assert not AssessmentComponent.objects.filter(section="primary").exists()
        sss = [
            (b.grade, b.lower, b.upper, b.remark) for b in GradeBand.objects.filter(section="sss")
        ]
        assert sss == [
            ("A1", 75, 100, "Excellent"), ("B2", 70, 74, "Very good"), ("B3", 65, 69, "Good"),
            ("C4", 60, 64, "Credit"), ("C5", 55, 59, "Credit"), ("C6", 50, 54, "Credit"),
            ("D7", 45, 49, "Pass"), ("E8", 40, 44, "Pass"), ("F9", 0, 39, "Fail"),
        ]  # fmt: skip
        jss = [
            (b.grade, b.lower, b.upper, b.remark) for b in GradeBand.objects.filter(section="jss")
        ]
        assert jss == [
            ("A", 70, 100, "Excellent"), ("B", 60, 69, "Very good"), ("C", 50, 59, "Good"),
            ("D", 45, 49, "Fair"), ("E", 40, 44, "Poor"), ("F", 0, 39, "Fail"),
        ]  # fmt: skip


def test_per_section_and_respects_changes(school_a):
    with schema_context("school_a"):
        seed_results(_school(["jss"]))
        AssessmentComponent.objects.filter(section="jss").exclude(name="Exam").delete()
        seed_results(_school(["jss", "sss"]))
        assert AssessmentComponent.objects.filter(section="jss").count() == 1
        assert AssessmentComponent.objects.filter(section="sss").count() == 5
        assert GradeBand.objects.filter(section="jss").count() == 6
        n = AssessmentComponent.objects.count()
        seed_results(_school(["jss", "sss"]))
        assert AssessmentComponent.objects.count() == n


def test_components_complete_false_for_10_60(school_a):
    with schema_context("school_a"):
        AssessmentComponent.objects.create(section="jss", name="CA", max_score=10, order=1)
        AssessmentComponent.objects.create(section="jss", name="Exam", max_score=60, order=2)
        assert not components_complete("jss")
        assert not components_complete("sss")


def test_grade_for(school_a):
    with schema_context("school_a"):
        seed_results(_school(["jss", "sss"]))
        assert grade_for("sss", Decimal("74.5")).grade == "A1"
        assert grade_for("sss", Decimal("74.4")).grade == "B2"
        assert grade_for("jss", Decimal("69.5")).grade == "A"
        assert grade_for("jss", Decimal("0")).grade == "F"
        assert grade_for("jss", 0).grade == "F"
        with pytest.raises(LookupError):
            grade_for("primary", Decimal("50"))


@pytest.mark.parametrize(
    "bands",
    [
        [(0, 39, "F", ""), (41, 100, "A", "")],  # gap
        [(0, 50, "F", ""), (50, 100, "A", "")],  # overlap
        [(0, 39, "F", ""), (40, 90, "A", "")],  # short of 100
        [(1, 39, "F", ""), (40, 100, "A", "")],  # not from 0
        [(0, 39, "", ""), (40, 100, "A", "")],  # empty grade
        [(0, 39, "TOOLONG", ""), (40, 100, "A", "")],
        [(0, 39, "A", ""), (40, 100, "A", "")],  # duplicate
        [],
    ],
)
def test_validate_scale_rejects(bands):
    with pytest.raises(ValidationError):
        validate_scale(bands)


def test_validate_scale_accepts_unsaved_objects():
    validate_scale(
        [GradeBand(lower=40, upper=100, grade="P"), GradeBand(lower=0, upper=39, grade="F")]
    )


def test_command(school_a):
    out = io.StringIO()
    school_a.levels_offered = ["jss"]
    school_a.save()
    call_command("seed_results_defaults", schema="school_a", stdout=out)
    with schema_context("school_a"):
        assert AssessmentComponent.objects.filter(section="jss").count() == 5
    with pytest.raises(CommandError):
        call_command("seed_results_defaults", schema="nope")
