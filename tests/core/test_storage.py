import re
from unittest import mock
from urllib.parse import unquote

import cloudinary
import cloudinary.exceptions
import cloudinary.utils
import pytest
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage, storages
from django_tenants.utils import schema_context

from apps.core.storage import NoTenantError, resource_type_for, unique_upload_name

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _cloudinary_config():
    old = cloudinary.config().__dict__.copy()
    cloudinary.config(cloud_name="test", api_key="k", api_secret="s")
    yield
    cloudinary.config(**old)


@pytest.fixture
def upload():
    with mock.patch("cloudinary.uploader.upload", return_value={"public_id": "x"}) as m:
        yield m


def test_settings_define_storages():
    assert settings.PRIVATE_URL_TTL_SECONDS == 600
    assert settings.STORAGES["default"]["OPTIONS"] == {"delivery_type": "authenticated"}
    assert settings.STORAGES["public"]["OPTIONS"] == {"delivery_type": "upload"}


def test_resource_type_for():
    assert resource_type_for("a/b.JPG") == "image"
    assert resource_type_for("a/b.webp") == "image"
    assert resource_type_for("a/b.pdf") == "raw"


def test_save_prefixes_env_and_schema(school_a, upload):
    with schema_context("school_a"):
        name = default_storage.save("report_cards/t1/a.pdf", ContentFile(b"%PDF"))
    assert name == "report_cards/t1/a.pdf"
    kwargs = upload.call_args.kwargs
    assert kwargs["public_id"] == "dev/school_a/report_cards/t1/a.pdf"
    assert kwargs["resource_type"] == "raw"
    assert kwargs["type"] == "authenticated"
    assert kwargs["overwrite"] is True


def test_image_drops_extension_and_uses_image_type(school_a, upload):
    with schema_context("school_a"):
        name = default_storage.save("photos/x.jpg", ContentFile(b"img"))
    assert name == "photos/x.jpg"
    kwargs = upload.call_args.kwargs
    assert kwargs["public_id"] == "dev/school_a/photos/x"
    assert kwargs["resource_type"] == "image"


def test_private_url_is_signed_and_expires(school_a):
    with mock.patch("time.time", return_value=1_000_000):
        with schema_context("school_a"):
            url = default_storage.url("report_cards/t1/a.pdf")
        expected = cloudinary.utils.private_download_url(
            "dev/school_a/report_cards/t1/a.pdf",
            "",
            resource_type="raw",
            type="authenticated",
            expires_at=1_000_600,
        )
    assert url == expected


def test_private_image_url_is_signed_and_expires(school_a):
    with mock.patch("time.time", return_value=1_000_000):
        with schema_context("school_a"):
            url = default_storage.url("photos/x.jpg")
        expected = cloudinary.utils.private_download_url(
            "dev/school_a/photos/x",
            "jpg",
            resource_type="image",
            type="authenticated",
            expires_at=1_000_600,
        )
    assert url == expected


def test_public_url_is_plain_https(school_a):
    with schema_context("school_a"):
        url = storages["public"].url("branding/logo.png")
    assert url.startswith("https://res.cloudinary.com/")
    assert "/image/upload/" in url
    assert "dev/school_a/branding/logo" in url


def test_same_name_in_other_school_points_to_other_folder(school_a, school_b):
    with schema_context("school_a"):
        url_a = unquote(default_storage.url("report_cards/t1/a.pdf"))
    with schema_context("school_b"):
        url_b = unquote(default_storage.url("report_cards/t1/a.pdf"))
    assert "school_a/" in url_a and "school_b/" not in url_a
    assert "school_b/" in url_b and "school_a/" not in url_b


def test_public_schema_refuses_storage(upload):
    with schema_context("public"):
        with pytest.raises(NoTenantError):
            default_storage.save("a/b.pdf", ContentFile(b"x"))
        with pytest.raises(NoTenantError):
            default_storage.url("a/b.pdf")
        with pytest.raises(NoTenantError):
            default_storage.delete("a/b.pdf")
        with pytest.raises(NoTenantError):
            default_storage.exists("a/b.pdf")
    upload.assert_not_called()


def test_unicode_filename_is_slugified(school_a, upload):
    with schema_context("school_a"):
        default_storage.save("photos/Adébáyọ̀ photo 1.JPG", ContentFile(b"img"))
    kwargs = upload.call_args.kwargs
    assert kwargs["public_id"] == "dev/school_a/photos/adebayo-photo-1"
    assert kwargs["resource_type"] == "image"


def test_exists_false_on_not_found(school_a):
    with mock.patch("cloudinary.api.resource", side_effect=cloudinary.exceptions.NotFound("x")):
        with schema_context("school_a"):
            assert default_storage.exists("a/b.pdf") is False


def test_exists_true_and_size(school_a):
    with mock.patch("cloudinary.api.resource", return_value={"bytes": 42}) as res:
        with schema_context("school_a"):
            assert default_storage.exists("a/b.pdf") is True
            assert default_storage.size("a/b.pdf") == 42
    assert res.call_args.args[0] == "dev/school_a/a/b.pdf"
    assert res.call_args.kwargs["resource_type"] == "raw"
    assert res.call_args.kwargs["type"] == "authenticated"


def test_delete_calls_destroy(school_a):
    with mock.patch("cloudinary.uploader.destroy") as destroy:
        with schema_context("school_a"):
            default_storage.delete("photos/x.jpg")
    assert destroy.call_args.args[0] == "dev/school_a/photos/x"
    assert destroy.call_args.kwargs["resource_type"] == "image"
    assert destroy.call_args.kwargs["type"] == "authenticated"


def test_non_latin_names_get_distinct_deterministic_ids(school_a, upload):
    ids = []
    with schema_context("school_a"):
        for name in ("photos/张三.jpg", "photos/李四.jpg", "photos/张三.jpg"):
            default_storage.save(name, ContentFile(b"i"))
            ids.append(upload.call_args.kwargs["public_id"])
    assert ids[0] != ids[1]
    assert ids[0] == ids[2]
    for public_id in ids:
        assert re.fullmatch(r"dev/school_a/photos/file-[0-9a-f]{8}", public_id)


def test_unique_upload_name():
    assert unique_upload_name("photos", "a.jpg") != unique_upload_name("photos", "a.jpg")
    assert re.fullmatch(
        r"photos/[0-9a-f]{32}\.jpg", unique_upload_name("photos", "Adébáyọ̀ photo 1.JPG")
    )
    assert "." not in unique_upload_name("docs", "README")


def _used():
    from apps.tenancy.limits import PlanLimits

    return PlanLimits.for_current_school().storage_used_bytes()


def test_quota_blocks_before_upload(school_a, upload):
    from apps.files.models import StoredFile
    from apps.tenancy.limits import PlanLimitExceeded, PlanLimits

    with schema_context("school_a"):
        cap = PlanLimits.for_current_school().plan.storage_bytes
        StoredFile.objects.create(name="big.bin", bytes=cap - 10)
        with pytest.raises(PlanLimitExceeded):
            default_storage.save("docs/over.bin", ContentFile(b"x" * 11))
        upload.assert_not_called()
        assert not StoredFile.objects.filter(name="docs/over.bin").exists()
        default_storage.save("docs/fit.bin", ContentFile(b"x" * 10))
        assert StoredFile.objects.get(name="docs/fit.bin").bytes == 10
        assert StoredFile.objects.get(name="docs/fit.bin").delivery_type == "authenticated"


def test_overwrite_counts_only_difference(school_a, upload):
    from apps.files.models import StoredFile

    with schema_context("school_a"):
        default_storage.save("docs/a.bin", ContentFile(b"x" * 10))
        assert _used() == 10
        default_storage.save("docs/a.bin", ContentFile(b"x" * 4))
        assert _used() == 4
        assert StoredFile.objects.filter(name="docs/a.bin").count() == 1


def test_overwrite_at_cap_is_allowed(school_a, upload):
    from apps.files.models import StoredFile
    from apps.tenancy.limits import PlanLimits

    with schema_context("school_a"):
        cap = PlanLimits.for_current_school().plan.storage_bytes
        StoredFile.objects.create(name="docs/a.bin", bytes=cap)
        default_storage.save("docs/a.bin", ContentFile(b"x" * 5))
        assert _used() == 5


def test_delete_removes_row(school_a, upload):
    from apps.files.models import StoredFile

    with schema_context("school_a"):
        default_storage.save("docs/d.bin", ContentFile(b"abc"))
        with mock.patch("cloudinary.uploader.destroy"):
            default_storage.delete("docs/d.bin")
        assert not StoredFile.objects.filter(name="docs/d.bin").exists()


def test_public_storage_records_upload_type(school_a, upload):
    from apps.files.models import StoredFile

    with schema_context("school_a"):
        storages["public"].save("branding/logo.png", ContentFile(b"png"))
        assert StoredFile.objects.get(name="branding/logo.png").delivery_type == "upload"
