"""Regenerate the downloadable import templates (header row only) under static/imports/."""

import csv
from pathlib import Path

import openpyxl
from django.core.management.base import BaseCommand

from apps.imports.students import TEMPLATE_HEADERS

STATIC_DIR = Path(__file__).resolve().parents[2] / "static" / "imports"


def write_templates(directory: Path = STATIC_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / "students_template.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\r\n").writerow(TEMPLATE_HEADERS)
    xlsx_path = directory / "students_template.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Students"
    ws.append(list(TEMPLATE_HEADERS))
    wb.save(xlsx_path)
    return [csv_path, xlsx_path]


class Command(BaseCommand):
    help = "Write the student import templates (CSV and XLSX) with the canonical headers."

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        for path in write_templates():
            self.stdout.write(str(path))
