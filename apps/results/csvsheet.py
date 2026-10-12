"""Score sheet <-> CSV (design §5.4). Columns: admission_number, name, one per component.

Line 1 is a version stamp, ``# version: <token>``, derived from every cell's version on the
sheet. An upload whose stamp is missing or no longer matches the sheet is refused, so a file
downloaded before later grid edits cannot silently revert them. Reading builds cells from the
sheet's *current* versions (equal to the download's when the stamp matches; ``save_cells``
re-checks them under its lock) and never echoes cell values into messages: the file can hold
children's names.
"""

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError

from apps.accounts.identifiers import normalise_admission_number
from apps.results.scores import CellInput, SheetData

MAX_BYTES = 1024 * 1024
FIXED = ["admission_number", "name"]
STALE = "This file is out of date — download a fresh copy"
_STAMP = re.compile(r"#\s*version:\s*([0-9a-f]{1,64})", re.IGNORECASE)
_GUARDED = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class ParsedUpload:
    cells: list[CellInput] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # student_id -> spreadsheet row number, for mapping save errors back to rows.
    row_of: dict[int, int] = field(default_factory=dict)


def safe_cell(value: object) -> str:
    """Stop spreadsheet apps treating a cell as a formula (CSV injection)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in _GUARDED else text


def unguard(text: str) -> str:
    """Undo ``safe_cell``: drop the leading "'" it adds before a formula character."""
    return text[1:] if text[:1] == "'" and text[1:2] in _GUARDED else text


def header_names(data: SheetData) -> list[str]:
    return FIXED + [c.name for c in data.components]


def sheet_version(data: SheetData) -> str:
    """A token that changes whenever any cell of the sheet (or its list of students) does."""
    h = hashlib.sha256()
    for row in data.rows:
        for c in row["cells"]:
            h.update(f"{row['student_id']}:{c['component_id']}:{c['version']};".encode())
    return h.hexdigest()[:20]


def sheet_to_csv(data: SheetData) -> str:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow([f"# version: {sheet_version(data)}"])
    writer.writerow([safe_cell(h) for h in header_names(data)])
    for row in data.rows:
        writer.writerow(
            [safe_cell(row["admission_number"]), safe_cell(row["name"])]
            + [safe_cell(c["value"]) for c in row["cells"]]
        )
    return out.getvalue()


def parse_upload(data: SheetData, raw: bytes) -> ParsedUpload:
    result = ParsedUpload()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        result.errors.append("Save the file as CSV UTF-8 and upload it again.")
        return result
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error:
        result.errors.append("The CSV file could not be read. Save it as CSV UTF-8 and try again.")
        return result
    if not rows:
        result.errors.append("The file is empty.")
        return result
    stamp = _STAMP.fullmatch(rows[0][0].strip()) if rows[0] else None
    if (
        stamp is None
        or any(c.strip() for c in rows[0][1:])
        or stamp.group(1).lower() != sheet_version(data)
    ):
        result.errors.append(STALE)
        return result
    if len(rows) < 2:
        result.errors.append("The file has no header row.")
        return result

    expected = [h.strip().lower() for h in header_names(data)]
    got = [unguard(h.strip()).lower() for h in rows[1]]
    if got != expected:
        missing = [h for h in header_names(data) if h.strip().lower() not in got]
        extra = [h for h in got if h not in expected]
        parts = []
        if missing:
            parts.append("missing column(s): " + ", ".join(missing))
        if extra:
            parts.append(f"{len(extra)} unexpected column(s)")
        if not parts:
            parts.append("columns must be in this order: " + ", ".join(header_names(data)))
        result.errors.append("The header does not match this sheet: " + "; ".join(parts) + ".")
        return result

    by_admission = {normalise_admission_number(r["admission_number"]): r for r in data.rows}
    seen: set[str] = set()
    width = len(expected)
    for n, cols in enumerate(rows[2:], start=3):  # spreadsheet row numbers
        if not any(c.strip() for c in cols):
            continue  # fully empty rows (e.g. trailing ",,,,") are skipped
        if len(cols) != width:
            result.errors.append(f"Row {n}: expected {width} columns, found {len(cols)}.")
            continue
        first = cols[0].strip()
        if first.startswith("'"):  # undo the download's formula guard (or Excel's text mark)
            first = first[1:]
        adm = normalise_admission_number(first)
        row = by_admission.get(adm)
        if row is None:
            result.errors.append(f"Row {n}: this admission number is not in this class's list.")
            continue
        if adm in seen:
            result.errors.append(f"Row {n}: this admission number appears more than once.")
            continue
        seen.add(adm)
        result.row_of[row["student_id"]] = n
        for cell, raw_value in zip(row["cells"], cols[2:], strict=True):
            value = raw_value.strip()
            result.cells.append(
                CellInput(
                    student_id=row["student_id"],
                    component_id=cell["component_id"],
                    value=value or None,
                    version=cell["version"],
                )
            )
    if not result.errors and not result.cells:
        result.errors.append("The file has no score rows.")
    return result


def describe_validation_error(
    data: SheetData, parsed: ParsedUpload, exc: ValidationError
) -> list[str]:
    names = {c.pk: c.name for c in data.components}
    messages: list[str] = []
    if not hasattr(exc, "error_dict"):
        return list(exc.messages)
    for key, errs in exc.message_dict.items():
        try:
            sid, cid = (int(p) for p in key.split(":"))
        except ValueError:
            messages.extend(errs)
            continue
        where = f"Row {parsed.row_of.get(sid, '?')}, {names.get(cid, 'component')}"
        messages.extend(f"{where}: {m}" for m in errs)
    return messages
