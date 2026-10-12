"""Regenerate the downloadable import templates (header row only) under static/imports/."""

import csv
from pathlib import Path

import openpyxl
from django.core.management.base import BaseCommand

from apps.imports.staff import TEMPLATE_HEADERS as STAFF_TEMPLATE_HEADERS
from apps.imports.students import TEMPLATE_HEADERS

STATIC_DIR = Path(__file__).resolve().parents[2] / "static" / "imports"


def _write(directory: Path, stem: str, title: str, headers: tuple[str, ...]) -> list[Path]:
    csv_path = directory / f"{stem}_template.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\r\n").writerow(headers)
    xlsx_path = directory / f"{stem}_template.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    ws.append(list(headers))
    wb.save(xlsx_path)
    return [csv_path, xlsx_path]


def write_templates(directory: Path = STATIC_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    return _write(directory, "students", "Students", TEMPLATE_HEADERS) + _write(
        directory, "staff", "Staff", STAFF_TEMPLATE_HEADERS
    )


class Command(BaseCommand):
    help = "Write the student and staff import templates (CSV and XLSX) with the canonical headers."

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        for path in write_templates():
            self.stdout.write(str(path))
