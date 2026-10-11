import os
import time
import urllib.error
import urllib.request

import cloudinary
import cloudinary.utils
import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django_tenants.utils import schema_context

pytestmark = [
    pytest.mark.cloudinary,
    pytest.mark.django_db,
    pytest.mark.skipif(not os.environ.get("CLOUDINARY_URL"), reason="CLOUDINARY_URL not set"),
]

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n%%EOF"


def test_private_pdf_roundtrip(school_a):
    name = "live_check/probe.pdf"
    with schema_context("school_a"):
        default_storage.save(name, ContentFile(PDF))
        try:
            with urllib.request.urlopen(default_storage.url(name), timeout=30) as resp:
                assert resp.status == 200
                assert resp.read().startswith(b"%PDF")
            expired = cloudinary.utils.private_download_url(
                "dev/school_a/live_check/probe.pdf",
                "",
                resource_type="raw",
                type="private",
                expires_at=int(time.time()) - 60,
            )
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(expired, timeout=30)
        finally:
            default_storage.delete(name)
