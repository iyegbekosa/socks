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


STAMPS: dict[int, str] = {}


def upload(client, w, text, name="s.csv", bom=False, stamp=True):
    """Upload ``text``; unless ``stamp`` is False, line 1 is the last download's version stamp
    (as the downloaded file has it)."""
    if stamp and w.offering.pk in STAMPS:
        text = STAMPS[w.offering.pk] + "\r\n" + text
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    return client.post(
        reverse("results:grid_upload", args=[w.offering.pk]),
        {"file": SimpleUploadedFile(name, data, content_type="text/csv")},
    )


def table(r, w=None):
    """The downloaded rows after the version stamp line (which is remembered for upload)."""
    rows = list(csv.reader(io.StringIO(r.content.decode())))
    assert len(rows[0]) == 1 and rows[0][0].startswith("# version: ")
    if w is not None:
        STAMPS[w.offering.pk] = rows[0][0]
    return rows[1:]


@pytest.fixture(autouse=True)
def _forget_stamps():
    STAMPS.clear()


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
    rows = table(download(client_a, w), w)
    rows[1][2], rows[1][3] = "7.5", "10"
    rows[2][2 + 4] = "55"
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    r = upload(client_a, w, out.getvalue(), bom=True)
    assert r.status_code == 302
    assert Score.objects.filter(offering=w.offering, term=w.term, value__isnull=False).count() == 3
    # Re-upload with the new versions (download again) and a change.
    rows = table(download(client_a, w), w)
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
    rows = table(download(client_a, w), w)
    rows[1][0] = rows[1][0].lower() + " "
    rows[1][1] = "Whatever"
    rows[1][2] = "5"
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    assert upload(client_a, w, out.getvalue()).status_code == 302


def bad(client, w, mutate, expect):
    rows = table(download(client, w), w)
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

    bad(client_a, w, m, f"Row 4, {w.comps[0].name}")


def test_unknown_admission_number(client_a, w):
    def m(rows):
        rows[1][0] = "NOPE"
        rows[2][2] = "5"

    bad(client_a, w, m, "Row 3")


def test_duplicate_rows(client_a, w):
    def dup(rows):
        rows[2][0] = rows[1][0]

    bad(client_a, w, dup, "more than once")


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
    rows = table(download(client_a, w), w)
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
    rows = table(download(client_a, w), w)
    rows[1][2] = "5"
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    ClassResult.objects.update_or_create(
        arm=w.arm, term=w.term, defaults={"status": ClassResult.Status.PUBLISHED}
    )
    r = upload(client_a, w, out.getvalue())
    assert r.status_code == 409 and "locked" in r.content.decode()
    assert not Score.objects.filter(offering=w.offering).exists()
    assert AuditEvent.objects.filter(action="results.edit_refused").exists()


def test_empty_rows_skipped_and_formula_guard_stripped(client_a, w):
    rows = table(download(client_a, w), w)
    rows[1][2] = "4"
    rows[1][0] = "'" + rows[1][0]
    rows.insert(2, [])
    n = len(rows[0])
    rows.append([""] * n)
    rows.append([""] * n)
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    assert upload(client_a, w, out.getvalue()).status_code == 302
    assert Score.objects.filter(offering=w.offering, value__isnull=False).count() == 1


# --- version stamp (final review M5) and header formula guard (M6) ----------------------


def edited(client, w, value="6"):
    rows = table(download(client, w), w)
    rows[1][2] = value
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue()


def test_download_starts_with_a_version_stamp(client_a, w):
    first = download(client_a, w).content.decode().splitlines()[0]
    assert first.startswith("# version: ") and len(first) > len("# version: ")
    again = download(client_a, w).content.decode().splitlines()[0]
    assert again == first  # stable while nothing changes


def test_stale_file_is_refused_and_saves_nothing(client_a, w):
    text = edited(client_a, w, "6")  # downloaded "on Monday"
    monday = STAMPS[w.offering.pk]
    newer = edited(client_a, w, "9")
    assert upload(client_a, w, newer).status_code == 302  # "Tuesday" edit
    r = upload(client_a, w, monday + "\r\n" + text, stamp=False)  # "Wednesday"
    assert r.status_code == 400
    assert "This file is out of date — download a fresh copy" in r.content.decode()
    values = set(Score.objects.filter(offering=w.offering).values_list("value", flat=True))
    assert values == {9}


def test_stale_after_a_grid_edit_elsewhere(client_a, w):
    from decimal import Decimal

    text = edited(client_a, w, "6")
    stamp = STAMPS[w.offering.pk]
    Score.objects.create(
        student=w.students[1],
        offering=w.offering,
        term=w.term,
        component=w.comps[0],
        value=Decimal("4"),
        version=1,
        entered_by=w.teacher,
    )
    r = client_a.post(
        reverse("results:grid_upload", args=[w.offering.pk]),
        {"file": SimpleUploadedFile("s.csv", (stamp + "\r\n" + text).encode())},
    )
    assert r.status_code == 400 and "out of date" in r.content.decode()
    assert Score.objects.filter(offering=w.offering).count() == 1


@pytest.mark.parametrize("first_line", [None, "# version: 0000", "# version: x", "# nothing"])
def test_missing_or_wrong_stamp_is_refused(client_a, w, first_line):
    text = edited(client_a, w)
    if first_line is not None:
        text = first_line + "\r\n" + text
    r = upload(client_a, w, text, stamp=False)
    assert r.status_code == 400 and "out of date" in r.content.decode()
    assert not Score.objects.filter(offering=w.offering).exists()


def test_stamp_row_padded_by_a_spreadsheet_is_accepted(client_a, w):
    text = edited(client_a, w)
    stamp = STAMPS[w.offering.pk]
    r = upload(client_a, w, stamp + ",,,,,,\r\n" + text, stamp=False)
    assert r.status_code == 302


def test_formula_like_component_name_is_guarded_and_round_trips(client_a, w):
    from apps.results.models import AssessmentComponent

    AssessmentComponent.objects.filter(pk=w.comps[0].pk).update(name="=X")
    rows = table(download(client_a, w), w)
    assert rows[0][2] == "'=X"
    rows[1][2] = "5"
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    assert upload(client_a, w, out.getvalue()).status_code == 302
    assert Score.objects.filter(offering=w.offering, value__isnull=False).count() == 1
