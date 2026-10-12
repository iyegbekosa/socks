import psycopg
import pytest
from django.db import connection, connections, transaction
from django_tenants.utils import schema_context

from apps.core.locks import school_lock

pytestmark = pytest.mark.django_db


def _other_connection():
    s = connection.settings_dict
    return psycopg.connect(
        dbname=s["NAME"],
        user=s["USER"] or None,
        password=s["PASSWORD"] or None,
        host=s["HOST"] or None,
        port=s["PORT"] or None,
        autocommit=False,
    )


def _try(other, key: str) -> bool:
    row = other.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", [key]).fetchone()
    other.rollback()  # release whatever was taken
    return row[0]


def test_school_lock_is_per_school(school_a, school_b):
    with schema_context("school_a"), transaction.atomic():
        school_lock("students")
        with _other_connection() as other:
            assert _try(other, "school_b:students") is True
            assert _try(other, "school_a:academics") is True
            assert _try(other, "school_a:students") is False


def test_school_lock_requires_atomic(monkeypatch):
    monkeypatch.setattr(connections["default"], "in_atomic_block", False)
    with pytest.raises(RuntimeError):
        school_lock("students")
