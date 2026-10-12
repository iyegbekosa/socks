import csv
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django_tenants.utils import schema_context

from apps.audit.models import AuditEvent
from apps.results.csvsheet import safe_cell
from apps.results.models import ClassResult, Score
from tests.results.factories import make_sheet_world

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(school_a, client_a):
    with schema_context("school_a"):
        w = make_sheet_world()
        client_a.force_login(w.teacher)
        yield w


def download(client, w):
    r = client.get(reverse("results:grid_csv", args=[w.offering.pk]))
    assert r.status_code == 200
    return r


def upload(client, w, text, name="s.csv", bom=False):
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    return client.post(
        reverse("results:grid_upload", args=[w.offering.pk]),
        {"file": SimpleUploadedFile(name, data, content_type="text/csv")},
    )


def table(r):
    return list(csv.reader(io.StringIO(r.content.decode())))


def test_download_shape(client_a, w):
    r = download(client_a, w)
    assert "no-store" in r["Cache-Control"]
    assert "maths-" in r["Content-Disposition"] and r["Content-Disposition"].endswith('.csv"')
    rows = table(r)
    assert rows[0] == ["admission_number", "name"] + [c.name for c in w.comps]
    assert len(rows) == 4 and rows[1][2:] == [""] * len(w.comps)


def test_formula_guard():
    assert safe_cell("=1+1") == "'=1+1"
    for ch in "+-@\t\r":
        assert safe_cell(ch + "x").startswith("'")
    assert safe_cell("12.5") == "12.5" and safe_cell(None) == ""


def test_round_trip_saves(client_a, w):
    rows = table(download(client_a, w))
    rows[1][2], rows[1][3] = "7.5", "10"
    rows[2][2 + 4] = "55"
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    r = upload(client_a, w, out.getvalue(), bom=True)
    assert r.status_code == 302
    assert Score.objects.filter(offering=w.offering, term=w.term, value__isnull=False).count() == 3
    # Re-upload with the new versions (download again) and a change.
    rows = table(download(client_a, w))
    assert rows[1][2] == "7.5"
    rows[1][2] = "8"
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    assert upload(client_a, w, out.getvalue()).status_code == 302
    assert (
        Score.objects.get(student__user__admission_number=rows[1][0], component=w.comps[0]).value
        == 8
    )


def test_admission_number_case_and_name_ignored(client_a, w):
    rows = table(download(client_a, w))
    rows[1][0] = rows[1][0].lower() + " "
    rows[1][1] = "Whatever"
    rows[1][2] = "5"
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    assert upload(client_a, w, out.getvalue()).status_code == 302


def bad(client, w, mutate, expect):
    rows = table(download(client, w))
    mutate(rows)
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    r = upload(client, w, out.getvalue())
    assert r.status_code == 400
    assert expect in r.content.decode()
    assert not Score.objects.filter(offering=w.offering).exists()


def test_value_over_max(client_a, w):
    def m(rows):
        rows[2][2] = "11"

    bad(client_a, w, m, f"Row 3, {w.comps[0].name}")


def test_unknown_admission_number(client_a, w):
    def m(rows):
        rows[1][0] = "NOPE"
        rows[2][2] = "5"

    bad(client_a, w, m, "Row 2")


def test_duplicate_and_blank_rows(client_a, w):
    def dup(rows):
        rows[2][0] = rows[1][0]

    bad(client_a, w, dup, "more than once")

    def blank(rows):
        rows.insert(2, [])

    bad(client_a, w, blank, "blank")


def test_header_errors(client_a, w):
    def extra(rows):
        for r in rows:
            r.append("x")

    bad(client_a, w, extra, "header")

    def missing(rows):
        for r in rows:
            r.pop()

    bad(client_a, w, missing, "missing column")


def test_header_case_insensitive(client_a, w):
    rows = table(download(client_a, w))
    rows[0] = [h.upper() for h in rows[0]]
    rows[1][2] = "3"
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    assert upload(client_a, w, out.getvalue()).status_code == 302


def test_file_type_and_size_and_encoding(client_a, w):
    assert upload(client_a, w, "a,b", name="s.xlsx").status_code == 400
    big = "x" * (1024 * 1024 + 1)
    assert upload(client_a, w, big).status_code == 400
    r = client_a.post(
        reverse("results:grid_upload", args=[w.offering.pk]),
        {"file": SimpleUploadedFile("s.csv", b"\xff\xfe\x00bad")},
    )
    assert r.status_code == 400
    assert client_a.post(reverse("results:grid_upload", args=[w.offering.pk])).status_code == 400


def test_upload_after_publish_refused_and_audited(client_a, w):
    rows = table(download(client_a, w))
    rows[1][2] = "5"
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    ClassResult.objects.update_or_create(
        arm=w.arm, term=w.term, defaults={"status": ClassResult.Status.PUBLISHED}
    )
    r = upload(client_a, w, out.getvalue())
    assert r.status_code == 400 and "locked" in r.content.decode()
    assert not Score.objects.filter(offering=w.offering).exists()
    assert AuditEvent.objects.filter(action="results.edit_refused").exists()
