import io

import pytest
from django.core.management import CommandError, call_command
from django_tenants.utils import schema_context

from apps.academics.models import ClassArm, ClassLevel, Subject

pytestmark = pytest.mark.django_db


def _wipe(schema):
    with schema_context(schema):
        ClassArm.objects.all().delete()
        ClassLevel.objects.all().delete()
        Subject.objects.all().delete()


@pytest.fixture(autouse=True)
def _clean(school_a, school_b):
    # Schools seeded before P2 have no academic rows.
    saved = (school_a.levels_offered, school_b.levels_offered)
    for schema in ("school_a", "school_b"):
        _wipe(schema)
    yield
    for schema in ("school_a", "school_b"):
        _wipe(schema)
    # Session-scoped objects: undo in-memory changes (the DB rows roll back).
    school_a.levels_offered, school_b.levels_offered = saved


def _run(*args) -> str:
    out = io.StringIO()
    call_command("seed_academics_defaults", *args, stdout=out)
    return out.getvalue()


def test_seeds_every_school(school_a, school_b):
    school_a.levels_offered = ["jss", "sss"]
    school_b.levels_offered = ["primary"]
    school_a.save(update_fields=["levels_offered"])
    school_b.save(update_fields=["levels_offered"])
    output = _run()
    with schema_context("school_a"):
        assert ClassLevel.objects.count() == 6
        assert Subject.objects.exists()
    with schema_context("school_b"):
        assert ClassLevel.objects.count() == 6
        assert set(ClassLevel.objects.values_list("section", flat=True)) == {"primary"}
    assert "school_a: 6 levels" in output and "school_b: 6 levels" in output
    assert "public" not in output


def _offer(school, levels):
    school.levels_offered = levels
    school.save(update_fields=["levels_offered"])


def test_one_schema_only(school_a, school_b):
    _offer(school_a, ["jss"])
    _offer(school_b, ["jss"])
    _run("--schema", "school_a")
    with schema_context("school_a"):
        assert ClassLevel.objects.exists()
    with schema_context("school_b"):
        assert not ClassLevel.objects.exists()


def test_skips_a_school_that_already_has_rows(school_a):
    with schema_context("school_a"):
        ClassLevel.objects.create(name="Year 7", section="jss", order=1)
        Subject.objects.create(name="Robotics", code="robotics", sections=["jss"])
    output = _run("--schema", "school_a")
    assert "school_a: skipped" in output
    with schema_context("school_a"):
        assert list(ClassLevel.objects.values_list("name", flat=True)) == ["Year 7"]
        assert list(Subject.objects.values_list("name", flat=True)) == ["Robotics"]


def test_rerun_is_idempotent(school_a):
    _offer(school_a, ["jss", "sss"])
    _run("--schema", "school_a")
    with schema_context("school_a"):
        counts = (ClassLevel.objects.count(), Subject.objects.count())
    assert "skipped" in _run("--schema", "school_a")
    with schema_context("school_a"):
        assert (ClassLevel.objects.count(), Subject.objects.count()) == counts


def test_unknown_schema_errors(school_a):
    with pytest.raises(CommandError):
        _run("--schema", "nope")
