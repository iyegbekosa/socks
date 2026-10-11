"""Tenant-scoped Cloudinary storage.

Stored names are relative (``report_cards/2026-1/a.pdf``). The ``{env}/{schema}/`` prefix is
added on every access from the *current* connection, so a name copied from another school
resolves inside the current school's folder and can never reach the other school's file.
"""

import hashlib
import os
import time
import unicodedata
import urllib.request
import uuid
from typing import Literal

import cloudinary
import cloudinary.api
import cloudinary.exceptions
import cloudinary.uploader
import cloudinary.utils
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.db import connection
from django.utils.deconstruct import deconstructible
from django.utils.text import slugify
from django_tenants.utils import get_public_schema_name

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class NoTenantError(RuntimeError):
    """Raised when storage is used outside a school (e.g. in the public schema)."""


def resource_type_for(name: str) -> Literal["image", "raw"]:
    ext = os.path.splitext(name)[1].lower()
    return "image" if ext in IMAGE_EXTENSIONS else "raw"


def _ascii_slug(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return slugify(folded)


def _fallback_stem(original: str) -> str:
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
    return f"file-{digest}"


def unique_upload_name(folder: str, filename: str) -> str:
    """Collision-free stored name for user uploads: ``folder/<uuid4 hex><.ext>``."""
    ext = os.path.splitext(filename)[1].lower()
    return f"{folder}/{uuid.uuid4().hex}{ext}"


def clean_name(name: str) -> str:
    """Slugify each path segment (ASCII-folded) and lowercase the extension."""
    segments = [s for s in name.replace("\\", "/").split("/") if s not in ("", ".", "..")]
    cleaned = []
    for i, segment in enumerate(segments):
        if i == len(segments) - 1:
            stem, ext = os.path.splitext(segment)
            ext = _ascii_slug(ext.lstrip(".")) if ext else ""
            segment = (_ascii_slug(stem) or _fallback_stem(stem)) + (f".{ext}" if ext else "")
        else:
            segment = _ascii_slug(segment) or _fallback_stem(segment)
        cleaned.append(segment)
    return "/".join(cleaned)


def _current_schema() -> str:
    schema = getattr(connection, "schema_name", None)
    if not schema or schema == get_public_schema_name():
        raise NoTenantError("Storage can only be used inside a school (tenant) schema.")
    return schema


def public_id_for(name: str) -> str:
    path = clean_name(name)
    if resource_type_for(path) == "image":
        path = os.path.splitext(path)[0]
    return f"{settings.CLOUDINARY_ENV_PREFIX}/{_current_schema()}/{path}"


@deconstructible
class TenantCloudinaryStorage(Storage):
    """Cloudinary storage whose public_ids are namespaced by env prefix and current schema.

    Contract:
    - Names must be unique after cleaning: case, spaces and punctuation are folded, and
      image extensions are not part of the public_id (``x.jpg`` and ``x.png`` collide).
    - Callers build names from IDs (or ``unique_upload_name``), never from user-supplied text.
    - Saving an existing name overwrites it (intended for regenerated PDFs).

    Delivery types: ``"authenticated"`` (default, child data) or ``"upload"`` (public
    branding). Child files must not use ``"private"``: Cloudinary serves derived
    (transformed) versions of private assets publicly, so ``.../image/private/w_800/<id>``
    would expose them. Authenticated assets need a signed URL for every version.
    """

    def __init__(self, delivery_type: Literal["upload", "authenticated"] = "authenticated"):
        self.delivery_type = delivery_type

    def _target(self, name: str) -> tuple[str, str, str]:
        """Return (public_id, resource_type, format) for a stored name."""
        cleaned = clean_name(name)
        resource_type = resource_type_for(cleaned)
        fmt = os.path.splitext(cleaned)[1].lstrip(".") if resource_type == "image" else ""
        return public_id_for(name), resource_type, fmt

    def get_available_name(self, name, max_length=None):
        return name

    def _save(self, name, content):
        from apps.files.models import StoredFile
        from apps.tenancy.limits import PlanLimits

        public_id, resource_type, _ = self._target(name)
        stored_name = clean_name(name)
        content.seek(0)
        data = content.read()
        size = getattr(content, "size", None)
        if size is None:
            size = len(data)
        existing = StoredFile.objects.filter(name=stored_name).values_list("bytes", flat=True)
        PlanLimits.for_current_school().check_can_store(size - (existing.first() or 0))
        cloudinary.uploader.upload(
            data,
            public_id=public_id,
            resource_type=resource_type,
            type=self.delivery_type,
            overwrite=True,
        )
        StoredFile.objects.update_or_create(
            name=stored_name, defaults={"bytes": size, "delivery_type": self.delivery_type}
        )
        return stored_name

    def url(self, name):
        public_id, resource_type, fmt = self._target(name)
        if self.delivery_type == "authenticated":
            expires_at = int(time.time()) + settings.PRIVATE_URL_TTL_SECONDS
            return cloudinary.utils.private_download_url(
                public_id,
                fmt,
                resource_type=resource_type,
                type="authenticated",
                expires_at=expires_at,
            )
        options = {"resource_type": resource_type, "type": "upload", "secure": True}
        if fmt:
            options["format"] = fmt
        return cloudinary.utils.cloudinary_url(public_id, **options)[0]

    def _open(self, name, mode="rb"):
        with urllib.request.urlopen(self.url(name), timeout=30) as resp:  # noqa: S310
            return ContentFile(resp.read(), name=name)

    def delete(self, name):
        public_id, resource_type, _ = self._target(name)
        cloudinary.uploader.destroy(public_id, resource_type=resource_type, type=self.delivery_type)
        from apps.files.models import StoredFile

        StoredFile.objects.filter(name=clean_name(name)).delete()

    def _resource(self, name):
        public_id, resource_type, _ = self._target(name)
        return cloudinary.api.resource(
            public_id, resource_type=resource_type, type=self.delivery_type
        )

    def exists(self, name):
        try:
            self._resource(name)
        except cloudinary.exceptions.NotFound:
            return False
        return True

    def size(self, name):
        return self._resource(name)["bytes"]
