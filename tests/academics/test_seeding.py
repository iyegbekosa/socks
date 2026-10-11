import pytest
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel, Subject
from apps.academics.seeding import seed_academics
from apps.tenancy.models import School

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


def _school(levels):
    return School(name="X", schema_name="school_a", levels_offered=levels)


def test_seed_jss_sss_idempotent(school_a):
    with schema_context("school_a"):
        seed_academics(_school(["jss", "sss"]))
        assert ClassLevel.objects.count() == 6
        assert set(ClassLevel.objects.values_list("name", flat=True)) == {
            "JSS 1", "JSS 2", "JSS 3", "SSS 1", "SSS 2", "SSS 3",
        }  # fmt: skip
        n_subjects = Subject.objects.count()
        assert n_subjects == 16 + 18 - 7  # shared names merge
        eng = Subject.objects.get(name="English Language")
        assert eng.code == "english-language"
        assert {"jss", "sss"} <= set(eng.sections)
        assert not Subject.objects.filter(name="Numeracy").exists()
        assert not ClassArm.objects.exists()
        seed_academics(_school(["jss", "sss"]))
        assert ClassLevel.objects.count() == 6
        assert Subject.objects.count() == n_subjects


def test_seed_all_sections_and_empty(school_a):
    with schema_context("school_a"):
        seed_academics(_school([]))
        assert not ClassLevel.objects.exists()
        seed_academics(_school(["nursery", "primary"]))
        assert ClassLevel.objects.count() == 8
        assert Subject.objects.get(name="Numeracy").sections == ["nursery"]
        eng = Subject.objects.get(name="English Language")
        assert eng.sections == ["primary"]
        seed_academics(_school(["jss"]))
        eng.refresh_from_db()
        assert set(eng.sections) == {"primary", "jss"}


def test_registered():
    from apps.tenancy import seeding

    assert seed_academics in seeding._SEEDERS
