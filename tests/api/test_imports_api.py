import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django_tenants.test.client import TenantClient
from django_tenants.utils import schema_context

from apps.accounts.roles import set_roles
from apps.imports.models import ImportJob
from apps.tenancy.limits import PlanLimitExceeded
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified
from tests.imports.conftest import csv_file, good_row

pytestmark = pytest.mark.django_db

IMPORT = "/api/v1/students/import"


def login_admin(client):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, ["school_admin"], by=None)
    login_verified(client, u)
    return u


def upload(name="s.csv", content=None):
    return {"file": SimpleUploadedFile(name, content or csv_file([good_row()]), "text/csv")}


def test_admin_import_accepted_and_job_runs(
    client_a, world, cloud, django_capture_on_commit_callbacks
):
    login_admin(client_a)
    with django_capture_on_commit_callbacks(execute=True):
        r = client_a.post(IMPORT, upload())
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    with schema_context("school_a"):
        job = ImportJob.objects.get(pk=job_id)
        assert job.kind == "students" and job.status == "completed" and job.ok_rows == 1
    d = client_a.get(f"/api/v1/imports/{job_id}")
    assert d.status_code == 200
    assert d.json() == {
        "id": job_id,
        "kind": "students",
        "status": "completed",
        "total_rows": 1,
        "ok_rows": 1,
        "error_rows": 0,
        "errors": [],
        "message": "",
    }


def test_teacher_and_anonymous_refused(client_a, world, cloud):
    assert client_a.post(IMPORT, upload()).status_code == 401
    with schema_context("school_a"):
        t = StaffFactory()
        set_roles(t, ["teacher"], by=None)
        client_a.force_login(t)
    assert client_a.post(IMPORT, upload()).status_code == 403
    assert client_a.get("/api/v1/imports/1").status_code == 403
    with schema_context("school_a"):
        assert not ImportJob.objects.exists()


@pytest.mark.parametrize(
    "files, field",
    [
        (lambda: upload("s.txt"), "file"),
        (lambda: upload("s.csv", b"x" * (5 * 1024 * 1024 + 1)), "file"),
        (lambda: {}, "file"),
    ],
)
def test_bad_file_is_400(client_a, world, cloud, files, field):
    login_admin(client_a)
    r = client_a.post(IMPORT, files())
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR" and err["details"][0]["field"] == field
    with schema_context("school_a"):
        assert not ImportJob.objects.exists()


def test_plan_quota_is_400(client_a, world, cloud, monkeypatch):
    login_admin(client_a)

    def boom(*a, **k):
        raise PlanLimitExceeded("Storage is full.")

    monkeypatch.setattr("apps.imports.services.default_storage.save", boom)
    r = client_a.post(IMPORT, upload())
    assert r.status_code == 400
    assert r.json()["error"]["details"] == [{"field": "file", "message": "Storage is full."}]


def test_no_current_term_is_400(client_a, world, cloud):
    from apps.academics.models import Term

    login_admin(client_a)
    with schema_context("school_a"):
        Term.objects.update(is_current=False)
    r = client_a.post(IMPORT, upload())
    assert r.status_code == 400
    assert "current term" in r.json()["error"]["details"][0]["message"]


def test_post_without_csrf_token_is_403(school_a, world, cloud):
    c = TenantClient(school_a, enforce_csrf_checks=True)
    login_admin(c)
    assert c.post(IMPORT, upload()).status_code == 403
    with schema_context("school_a"):
        assert not ImportJob.objects.exists()


def test_unknown_job_is_404(client_a, world):
    login_admin(client_a)
    assert client_a.get("/api/v1/imports/999999").status_code == 404


def test_detail_caps_errors_and_includes_message(client_a, world):
    from apps.imports.services import ERRORS_SHOWN

    login_admin(client_a)
    with schema_context("school_a"):
        job = ImportJob.objects.create(
            kind="students",
            file_name="x",
            created_by=world.admin,
            status="failed",
            message="Could not read the file.",
            errors=[{"row": i, "column": "c", "message": "m"} for i in range(ERRORS_SHOWN + 50)],
        )
    body = client_a.get(f"/api/v1/imports/{job.pk}").json()
    assert len(body["errors"]) == ERRORS_SHOWN
    assert body["message"] == "Could not read the file."
