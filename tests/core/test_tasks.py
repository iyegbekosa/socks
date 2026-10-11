import logging

import pytest
from django.db import connection
from django_tenants.utils import schema_context

from apps.core.logging import ContextFilter
from apps.core.request_id import request_id_var, schema_name_var
from apps.core.tasks import (
    CrossTenantEnqueueError,
    MissingTenantError,
    TenantTask,
    UnknownTenantError,
)
from config.celery import app

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _eager_celery(settings):
    # app.conf reads Django settings (namespace CELERY) ahead of its own overrides, and
    # settings default to eager only without REDIS_URL, so pin both via the settings fixture.
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True


@app.task(base=TenantTask, name="tests.current_schema")
def current_schema():
    return connection.schema_name


@app.task(base=TenantTask, bind=True, name="tests.retry_once")
def retry_once(self):
    if self.request.retries == 0:
        raise self.retry(countdown=0)
    return connection.schema_name


def test_task_runs_in_enqueuing_school(school_b):
    with schema_context("school_b"):
        assert current_schema.delay().get() == "school_b"


def test_task_without_schema_fails():
    with pytest.raises(MissingTenantError):
        current_schema.apply_async()


def test_explicit_schema_from_public(school_a):
    result = current_schema.apply_async(headers={"schema_name": "school_a"})
    assert result.get() == "school_a"


def test_retry_keeps_schema(school_a, settings):
    # Celery quirk: with task_eager_propagates=True an eager self.retry() re-raises Retry
    # instead of re-running the task, so this one test turns propagation off.
    settings.CELERY_TASK_EAGER_PROPAGATES = False
    with schema_context("school_a"):
        result = retry_once.delay()
    assert result.get() == "school_a"


def test_message_without_header_raises_and_is_not_retried():
    # Simulates a worker receiving a message with no schema header (bypassing apply_async).
    with pytest.raises(MissingTenantError):
        current_schema.apply().get()


def test_unknown_schema_fails_clearly():
    # django-tenants would otherwise set search_path to "no_such_school, public" and
    # silently read public tables, so TenantTask verifies the school exists.
    with pytest.raises(UnknownTenantError, match="no_such_school"):
        current_schema.apply_async(headers={"schema_name": "no_such_school"})


@app.task(base=TenantTask, name="tests.log_line")
def log_line():
    logging.getLogger("tests.tasks").warning("hello from task")


def test_task_log_lines_carry_school_and_task_id(school_a, caplog):
    caplog.handler.addFilter(ContextFilter())
    with caplog.at_level(logging.WARNING, logger="tests.tasks"):
        with schema_context("school_a"):
            result = log_line.delay()
    records = [r for r in caplog.records if r.name == "tests.tasks"]
    assert records
    assert records[0].schema == "school_a"
    assert records[0].request_id == result.id
    assert schema_name_var.get() == "-"
    assert request_id_var.get() == "-"


@app.task(base=TenantTask, name="tests.sentry_tags")
def sentry_tags():
    from config.settings import _sentry_before_send

    return _sentry_before_send({}, None)["tags"]


def test_sentry_tags_carry_school_inside_task(school_a):
    with schema_context("school_a"):
        result = sentry_tags.delay()
    assert result.get() == {"schema": "school_a", "request_id": result.id}


def test_cross_school_enqueue_from_inside_a_school_is_refused(school_a, school_b):
    with schema_context("school_a"):
        with pytest.raises(CrossTenantEnqueueError, match="school_b"):
            current_schema.apply_async(headers={"schema_name": "school_b"})


def test_explicit_header_naming_current_school_is_allowed(school_a):
    with schema_context("school_a"):
        result = current_schema.apply_async(headers={"schema_name": "school_a"})
    assert result.get() == "school_a"


@app.task(base=TenantTask, name="tests.boom")
def boom():
    raise ValueError("boom")


def test_context_is_set_when_task_failure_fires(school_a, settings):
    from celery.signals import task_failure

    seen = []

    def receiver(sender=None, **kwargs):
        seen.append((schema_name_var.get(), request_id_var.get()))

    # Eager mode skips task_failure while propagating, so let the failure be recorded.
    settings.CELERY_TASK_EAGER_PROPAGATES = False
    task_failure.connect(receiver, weak=False)
    try:
        with schema_context("school_a"):
            result = boom.delay()
        assert result.failed()
    finally:
        task_failure.disconnect(receiver)
    assert seen
    assert seen[0][0] == "school_a"
    assert schema_name_var.get() == "-"
    assert request_id_var.get() == "-"
