import logging

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection

from apps.core import sms

pytestmark = pytest.mark.django_db


def test_locmem_outbox(sms_outbox, school_a):
    connection.set_tenant(school_a)
    sms.send_sms("+2348031234567", "hello")
    assert [(m.to, m.body) for m in sms_outbox] == [("+2348031234567", "hello")]


def test_console_prints_masked_not_logged(settings, capsys, caplog, school_a):
    connection.set_tenant(school_a)
    settings.SMS_BACKEND = "apps.core.sms.ConsoleSmsBackend"
    with caplog.at_level(logging.DEBUG):
        sms.send_sms("+2348031234567", "code 123456")
    out = capsys.readouterr().out
    assert "***4567" in out and "code 123456" in out
    assert "+2348031234567" not in out and "8031234567" not in out
    assert "123456" not in caplog.text


def test_unset_backend_without_debug_raises(settings, school_a):
    connection.set_tenant(school_a)
    settings.DEBUG = False
    settings.SMS_BACKEND = None
    with pytest.raises(ImproperlyConfigured):
        sms.send_sms("+2348031234567", "x")
