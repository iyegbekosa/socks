from apps.tenancy import seeding


def test_seeders_run_in_order_and_in_schema(school_a, monkeypatch):
    from django.db import connection

    monkeypatch.setattr(seeding, "_SEEDERS", [])
    seen = []
    seeding.register_seeder(lambda s: seen.append(("one", connection.schema_name)))
    seeding.register_seeder(lambda s: seen.append(("two", connection.schema_name)))
    seeding.seed_school(school_a)
    assert seen == [("one", "school_a"), ("two", "school_a")]


def test_roles_seeder_registered():
    from apps.accounts.roles import seed_roles

    assert seed_roles in seeding._SEEDERS
