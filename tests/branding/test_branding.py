import io
from unittest import mock

import cloudinary
import pytest
from django.core.cache import cache
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django_tenants.utils import schema_context
from PIL import Image

from apps.accounts.roles import set_roles
from apps.audit.models import AuditEvent
from apps.branding.models import Branding, get_branding
from apps.tenancy.limits import PlanLimitExceeded
from tests.accounts.factories import StaffFactory
from tests.accounts.helpers import login_verified

pytestmark = pytest.mark.django_db

URL = "/settings/branding/"


@pytest.fixture(autouse=True)
def _cloudinary_config():
    old = cloudinary.config().__dict__.copy()
    cloudinary.config(cloud_name="test", api_key="k", api_secret="s")
    yield
    cloudinary.config(**old)


@pytest.fixture(autouse=True)
def _clean(school_a):
    cache.clear()
    with schema_context("school_a"):
        Branding.objects.all().delete()
    yield
    cache.clear()
    with schema_context("school_a"):
        Branding.objects.all().delete()


@pytest.fixture
def upload():
    with mock.patch("cloudinary.uploader.upload", return_value={"public_id": "x"}) as m:
        yield m


def png_bytes(fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, fmt)
    return buf.getvalue()


def admin(client, roles=("school_admin",)):
    with schema_context("school_a"):
        u = StaffFactory()
        set_roles(u, list(roles), by=u)
    login_verified(client, u)
    return u


def post(client, **over):
    data = {
        "motto": "Knowledge first",
        "address": "1 School Rd",
        "primary_color": "#112233",
        "accent_color": "#AABBCC",
    }
    data.update(over)
    return client.post(URL, data)


def test_login_page_shows_name_motto_and_colour(client_a):
    with schema_context("school_a"):
        b = get_branding()
        b.motto = "Excellence"
        b.save()
    html = client_a.get(reverse("accounts:login")).content.decode()
    assert "School A" in html
    assert "Excellence" in html
    assert "--primary: #1F4E79" in html


def test_login_page_logo_url(client_a, upload):
    with schema_context("school_a"):
        b = get_branding()
        b.logo.save("x.png", SimpleUploadedFile("x.png", png_bytes()), save=False)
        b.save()
    html = client_a.get(reverse("accounts:login")).content.decode()
    for part in ("w_240", "c_limit", "f_auto", "q_auto", "dev/school_a/branding/"):
        assert part in html


def test_image_url_refuses_public_and_raw():
    from apps.core.storage import NoTenantError

    st = storages["public"]
    with pytest.raises(NoTenantError):
        st.image_url("branding/a.png", width=10)
    with schema_context("school_a"):
        with pytest.raises(ValueError):
            st.image_url("branding/a.pdf", width=10)


def test_invalid_colour(client_a, upload):
    admin(client_a)
    r = post(client_a, primary_color="red")
    assert r.status_code == 200
    assert "primary_color" in r.context["form"].errors
    r = post(client_a, accent_color="#12345")
    assert "accent_color" in r.context["form"].errors


@pytest.mark.parametrize(
    "name,content",
    [
        ("a.gif", png_bytes("GIF")),
        ("a.png", png_bytes("GIF")),
        ("a.png", png_bytes("JPEG")),
        ("a.png", b"x" * 1_048_577),
        ("a.png", b"not an image"),
    ],
)
def test_bad_logo_rejected(client_a, upload, name, content):
    admin(client_a)
    r = post(client_a, logo=SimpleUploadedFile(name, content))
    assert r.status_code == 200
    assert "logo" in r.context["form"].errors
    upload.assert_not_called()


def test_exact_limit_accepted(client_a, upload):
    admin(client_a)
    data = png_bytes()
    pad = b"\0" * (1_048_576 - len(data))
    r = post(client_a, logo=SimpleUploadedFile("a.PNG", data + pad))
    assert r.status_code == 302, r.context["form"].errors if r.context else None
    upload.assert_called_once()


def test_teacher_forbidden(client_a):
    admin(client_a, roles=("teacher",))
    assert client_a.get(URL).status_code == 403
    assert post(client_a).status_code == 403


def test_save_audits_and_refreshes_cache(client_a, upload):
    admin(client_a)
    with schema_context("school_a"):
        assert get_branding().motto == ""
    r = post(client_a, logo=SimpleUploadedFile("a.png", png_bytes()))
    assert r.status_code == 302
    with schema_context("school_a"):
        assert get_branding().motto == "Knowledge first"
        assert get_branding().primary_color == "#112233"
        ev = AuditEvent.objects.get(action="branding.updated")
    assert set(ev.after["changed"]) == {
        "motto",
        "address",
        "primary_color",
        "accent_color",
        "logo",
    }
    assert "png" not in str(ev.after).lower().replace("logo", "")


def test_quota_exceeded_is_form_error(client_a, upload):
    admin(client_a)
    with mock.patch(
        "apps.tenancy.limits.PlanLimits.check_can_store",
        side_effect=PlanLimitExceeded("Storage limit for your plan has been reached."),
    ):
        r = post(client_a, logo=SimpleUploadedFile("a.png", png_bytes()))
    assert r.status_code == 200
    assert "Storage limit" in str(r.context["form"].errors["logo"])
    upload.assert_not_called()


def test_public_host_renders_with_no_branding(client_a):
    from django.test import Client

    r = Client(HTTP_HOST="localhost").get("/")
    assert r.status_code == 200
    assert r.context["branding"] is None


def test_trailing_junk_is_stripped(client_a, upload):
    admin(client_a)
    r = post(client_a, logo=SimpleUploadedFile("a.png", png_bytes() + b"JUNKJUNK" * 128))
    assert r.status_code == 302
    sent = upload.call_args.args[0]
    assert b"JUNKJUNK" not in sent
    assert Image.open(io.BytesIO(sent)).format == "PNG"


def test_replaced_logo_is_deleted(client_a, upload, django_capture_on_commit_callbacks):
    from apps.files.models import StoredFile

    admin(client_a)
    with django_capture_on_commit_callbacks(execute=True):
        post(client_a, logo=SimpleUploadedFile("a.png", png_bytes()))
    with schema_context("school_a"):
        old = Branding.objects.get(pk=1).logo.name
        assert StoredFile.objects.filter(name=old).exists()
    with mock.patch("cloudinary.uploader.destroy") as destroy:
        with django_capture_on_commit_callbacks(execute=True):
            post(client_a, logo=SimpleUploadedFile("b.png", png_bytes("PNG")))
    with schema_context("school_a"):
        assert Branding.objects.get(pk=1).logo.name != old
        assert not StoredFile.objects.filter(name=old).exists()
    assert destroy.call_args.args[0] == "dev/school_a/" + old.removesuffix(".png")


def test_logo_only_change_refreshes_cache(client_a, upload, django_capture_on_commit_callbacks):
    admin(client_a)
    with schema_context("school_a"):
        assert get_branding().logo_url is None
    with django_capture_on_commit_callbacks(execute=True):
        post(client_a, logo=SimpleUploadedFile("a.png", png_bytes()))
    with schema_context("school_a"):
        assert get_branding().logo_url is not None


def test_cache_is_per_school(school_b):
    with schema_context("school_a"):
        b = get_branding()
        b.motto = "Only A"
        b.save()
        assert get_branding().motto == "Only A"
    with schema_context("school_b"):
        assert get_branding().motto == ""
        Branding.objects.all().delete()
        cache.clear()


def test_old_logo_delete_failure_is_logged_not_500(
    client_a, upload, django_capture_on_commit_callbacks, caplog
):
    admin(client_a)
    with django_capture_on_commit_callbacks(execute=True):
        post(client_a, logo=SimpleUploadedFile("a.png", png_bytes()))
    with schema_context("school_a"):
        old = Branding.objects.get(pk=1).logo.name
    with mock.patch("cloudinary.uploader.destroy", side_effect=RuntimeError("boom")):
        with django_capture_on_commit_callbacks(execute=True):
            r = post(client_a, logo=SimpleUploadedFile("b.png", png_bytes()))
    assert r.status_code == 302
    with schema_context("school_a"):
        assert Branding.objects.get(pk=1).logo.name != old
    assert any(rec.levelname == "ERROR" and old in rec.getMessage() for rec in caplog.records)
