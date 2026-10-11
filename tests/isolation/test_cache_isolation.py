from django.core.cache import cache
from django_tenants.utils import schema_context


def test_same_cache_key_is_separate_per_school(school_a, school_b):
    with schema_context("school_a"):
        cache.set("branding", "A")
    with schema_context("school_b"):
        cache.set("branding", "B")
    with schema_context("school_a"):
        assert cache.get("branding") == "A"
    with schema_context("school_b"):
        assert cache.get("branding") == "B"
