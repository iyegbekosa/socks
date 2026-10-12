import csv
import io
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

import cloudinary
import openpyxl
import pytest
from django.core.files.base import ContentFile
from django_tenants.utils import schema_context

from apps.academics.models import (
    AcademicSession,
    AcademicSettings,
    AdmissionCounter,
    ClassArm,
    ClassLevel,
    FormTeacher,
    Term,
)
from apps.academics.services import update_admission_settings
from apps.accounts.roles import set_roles
from apps.core.storage import TenantCloudinaryStorage, public_id_for
from apps.imports.models import ImportJob
from apps.students.models import Enrolment, Student
from tests.accounts.factories import StaffFactory

HEADERS = [
    "first_name",
    "middle_name",
    "last_name",
    "sex",
    "date_of_birth",
    "class",
    "admission_number",
    "guardian_name",
    "guardian_relationship",
    "guardian_phone",
    "guardian_email",
]


def good_row(**over) -> dict:
    row = {
        "first_name": "Ada",
        "middle_name": "",
        "last_name": "Eze",
        "sex": "F",
        "date_of_birth": "17/05/2014",
        "class": "JSS 1A",
        "admission_number": "",
        "guardian_name": "Ngozi Eze",
        "guardian_relationship": "mother",
        "guardian_phone": "08031234567",
        "guardian_email": "",
    }
    row.update(over)
    return row


def csv_file(rows: list[dict], headers: list[str] = HEADERS) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def xlsx_file(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _wipe():
    Enrolment.objects.all().delete()
    Student.objects.all().delete()
    FormTeacher.objects.all().delete()
    Term.objects.all().delete()
    AcademicSession.objects.all().delete()
    ClassArm.objects.all().delete()
    ClassLevel.objects.all().delete()
    AcademicSettings.objects.all().delete()
    AdmissionCounter.objects.all().delete()


@pytest.fixture(autouse=True)
def _eager(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True


@pytest.fixture(autouse=True)
def _cloudinary_config():
    old = cloudinary.config().__dict__.copy()
    cloudinary.config(cloud_name="test", api_key="k", api_secret="s")
    yield
    cloudinary.config(**old)


@pytest.fixture
def cloud():
    """In-memory stand-in for Cloudinary: upload, destroy and reading files back."""
    store: dict[str, bytes] = {}

    def upload(data, public_id, **kwargs):
        store[public_id] = data
        return {"public_id": public_id}

    def destroy(public_id, **kwargs):
        store.pop(public_id, None)
        return {"result": "ok"}

    def open_(self, name, mode="rb"):
        return ContentFile(store[public_id_for(name)], name=name)

    with (
        mock.patch("cloudinary.uploader.upload", side_effect=upload) as up,
        mock.patch("cloudinary.uploader.destroy", side_effect=destroy) as de,
        mock.patch.object(TenantCloudinaryStorage, "_open", open_),
    ):
        yield SimpleNamespace(store=store, upload=up, destroy=de)


@pytest.fixture
def world(school_a):
    with schema_context("school_a"):
        _wipe()
        ImportJob.objects.all().delete()
        update_admission_settings(prefix="GFS", pattern="{PREFIX}/{YEAR}/{SEQ:4}", by=None)
        today = date.today()
        session = AcademicSession.objects.create(
            name="2026/2027",
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=300),
        )
        Term.objects.create(
            session=session,
            number=1,
            start_date=session.start_date,
            end_date=today + timedelta(days=60),
            is_current=True,
        )
        lvl1 = ClassLevel.objects.create(name="JSS 1", section="jss", order=1)
        lvl2 = ClassLevel.objects.create(name="JSS 2", section="jss", order=2)
        a = ClassArm.objects.create(level=lvl1, suffix="A")
        b = ClassArm.objects.create(level=lvl2, suffix="Gold")
        admin = StaffFactory()
        set_roles(admin, ["school_admin"], by=admin)
        yield SimpleNamespace(session=session, a=a, b=b, admin=admin)
        ImportJob.objects.all().delete()
        _wipe()
