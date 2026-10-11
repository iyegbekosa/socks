"""Review Focus 2: two admins admitting at once never get the same admission number.

The normal test DB wraps each test in a transaction other connections cannot see, so
this test builds a throwaway committed schema (``conc_test``) with raw SQL, runs two
threads on separate DB connections against it, and always drops it afterwards.
"""

import threading

import pytest
from django.db import connection, connections, transaction
from django_tenants.utils import schema_context

from apps.academics.services import next_admission_number

SCHEMA = "conc_test"
# Marks the test as DB-using so the session test DB exists; the work itself runs on
# separate committed connections, not in the test transaction.
pytestmark = pytest.mark.django_db

TABLES = ["academics_admissioncounter", "academics_academicsettings", "accounts_user"]


def _admin_conn():
    import psycopg

    s = connection.settings_dict
    return psycopg.connect(
        dbname=s["NAME"],
        user=s["USER"] or None,
        password=s["PASSWORD"] or None,
        host=s["HOST"] or None,
        port=s["PORT"] or None,
        autocommit=True,
    )


@pytest.fixture
def conc_schema(school_a, django_db_blocker):
    with django_db_blocker.unblock():
        conn = _admin_conn()
        try:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            conn.execute(f"CREATE SCHEMA {SCHEMA}")
            for table in TABLES:
                conn.execute(f"CREATE TABLE {SCHEMA}.{table} (LIKE school_a.{table} INCLUDING ALL)")
            conn.execute(
                f"INSERT INTO {SCHEMA}.academics_academicsettings "
                "(id, admission_prefix, admission_pattern) "
                "VALUES (1, 'GFS', '{PREFIX}/{YEAR}/{SEQ:4}')"
            )
            yield conn
        finally:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            conn.close()


def test_two_threads_get_distinct_numbers(conc_schema, django_db_blocker):
    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            with django_db_blocker.unblock():
                barrier.wait(timeout=10)
                for _ in range(20):
                    with schema_context(SCHEMA), transaction.atomic():
                        results.append(next_admission_number(2026))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "worker thread hung"
    assert not errors, errors
    assert len(results) == 40
    assert len(set(results)) == 40
    assert set(results) == {f"GFS/2026/{n:04d}" for n in range(1, 41)}
