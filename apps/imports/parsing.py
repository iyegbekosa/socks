"""Reading uploaded CSV/XLSX files into rows, and normalising spreadsheet cells.

Cells can hold children's data (dates of birth, guardian contacts). Error messages raised
here never include a cell's value.
"""

import csv
import io
import os
import re
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from typing import IO, Any

import openpyxl

from apps.accounts.identifiers import normalise_ng_phone

MAX_ROWS = 2000
MAX_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = (".csv", ".xlsx")
DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d")
_BARE_MOBILE = re.compile(r"^[789]\d{9}$")
_PHONE_PUNCTUATION = re.compile(r"[\s\-().]")
_MIDNIGHT = re.compile(r"\s+00:00(:00)?$")


class ImportFileError(Exception):
    """The file as a whole cannot be imported. ``message`` is safe to show and store."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class TooManyRows(ImportFileError):
    def __init__(self) -> None:
        super().__init__(f"File has more than {MAX_ROWS:,} rows")


def canonical_header(value: Any) -> str:
    """``" First Name "`` / ``"FIRST_NAME"`` / ``"first-name"`` → ``"first_name"``."""
    text = cell_text(value).lower().replace("-", " ").replace("_", " ")
    return "_".join(text.split())


def cell_text(value: Any) -> str:
    """Spreadsheet cell as trimmed text; whole floats lose their ``.0``."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value).strip()


def cell_date(value: Any) -> date:
    """Excel date/datetime, ``DD/MM/YYYY``, ``D/M/YYYY`` or ``YYYY-MM-DD``; else ValueError."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = _MIDNIGHT.sub("", value.strip())
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    raise ValueError("Use a date like 25/12/2014 (DD/MM/YYYY).")


def cell_phone(value: Any) -> str:
    """Nigerian mobile in E.164, or ``""`` for an empty cell; ValueError if invalid.

    A number typed into Excel loses its leading zero (``8031234567``), so a bare 10-digit
    number starting 7, 8 or 9 gets the ``0`` back before normalising.
    """
    text = _PHONE_PUNCTUATION.sub("", cell_text(value))
    if not text:
        return ""
    if _BARE_MOBILE.match(text):
        text = "0" + text
    return normalise_ng_phone(text)


def _is_blank(values: Iterable[Any]) -> bool:
    return all(cell_text(v) == "" for v in values)


def _csv_rows(fileobj: IO[bytes]) -> Iterator[list[Any]]:
    try:
        text = fileobj.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportFileError("Save the file as CSV UTF-8 and upload it again.") from None
    try:
        yield from csv.reader(io.StringIO(text, newline=""))
    except csv.Error:
        raise ImportFileError(
            "The CSV file could not be read. Save it as CSV UTF-8 and try again."
        ) from None


def _xlsx_rows(fileobj: IO[bytes]) -> Iterator[list[Any]]:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(fileobj.read()), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - zip/XML errors of every kind mean "not a workbook"
        raise ImportFileError("The file could not be read as an Excel (.xlsx) workbook.") from None
    try:
        if not wb.worksheets:
            return
        for row in wb.worksheets[0].iter_rows(values_only=True):
            yield list(row)
    finally:
        wb.close()


def read_rows(
    fileobj: IO[bytes],
    filename: str,
    *,
    required: Iterable[str] = (),
    known: Iterable[str] = (),
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield ``(row_number, {canonical_column: raw_cell})`` for each non-blank data row.

    Row numbers are 1-based and count the header, so they match what the user sees in
    Excel. CSV values are strings; XLSX values are whatever the cell holds (str, int,
    float, datetime, None): use the ``cell_*`` normalisers. Columns that are not in the
    header are ignored. Messages only ever name ``required``/``known`` columns: a file
    without a header row would otherwise echo a child's data from its first row.
    Raises ``ImportFileError`` (bad file, missing ``required`` columns, repeated columns)
    or ``TooManyRows`` once more than ``MAX_ROWS`` data rows have been seen.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".csv":
        source = _csv_rows(fileobj)
    elif ext == ".xlsx":
        source = _xlsx_rows(fileobj)
    else:
        raise ImportFileError("Upload a .csv or .xlsx file.")
    required_cols = list(required)
    safe_names = set(required_cols) | set(known)
    try:
        header: list[str] | None = None
        count = 0
        for number, values in enumerate(source, start=1):
            if header is None:
                if _is_blank(values):
                    raise ImportFileError("The first row must hold the column names.")
                header = [canonical_header(v) for v in values]
                missing = [c for c in required_cols if c not in header]
                if missing:
                    raise ImportFileError(f"Missing columns: {', '.join(missing)}.")
                named = [h for h in header if h]
                dupes = {h for h in named if named.count(h) > 1}
                if dupes:
                    if dupes <= safe_names:
                        listed = ", ".join(sorted(dupes))
                        raise ImportFileError(f"Columns appear more than once: {listed}.")
                    raise ImportFileError("Some column names appear more than once.")
                continue
            if _is_blank(values):
                continue
            count += 1
            if count > MAX_ROWS:
                raise TooManyRows()
            row = {
                name: values[i] if i < len(values) else None
                for i, name in enumerate(header)
                if name
            }
            yield number, row
        if header is None:
            raise ImportFileError("The file is empty.")
    finally:
        close = getattr(source, "close", None)
        if close is not None:
            close()
