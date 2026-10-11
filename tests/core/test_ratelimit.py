import pytest
from django.core.cache import cache
from django_tenants.utils import schema_context

from apps.core.ratelimit import RateLimit

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear():
    cache.clear()
    yield
    cache.clear()


def test_blocks_after_limit_and_retry_after_in_range():
    rl = RateLimit("t")
    for _ in range(4):
        rl.hit("k")
    assert not rl.is_blocked("k")
    assert rl.hit("k") == 5
    assert rl.is_blocked("k")
    assert 1 <= rl.retry_after("k") <= 900


def test_reset_unblocks():
    rl = RateLimit("t")
    for _ in range(5):
        rl.hit("k")
    rl.reset("k")
    assert not rl.is_blocked("k")


def test_schools_count_separately(school_a, school_b):
    rl = RateLimit("t")
    with schema_context("school_a"):
        for _ in range(5):
            rl.hit("k")
        assert rl.is_blocked("k")
    with schema_context("school_b"):
        assert not rl.is_blocked("k")
