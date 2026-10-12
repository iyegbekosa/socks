"""Score sheet <-> CSV (design §5.4). Columns: admission_number, name, one per component.

Reading builds cells from the sheet's *current* versions and never echoes cell values
into messages: the file can hold children's names.
"""

import csv
import io
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError

from apps.accounts.identifiers import normalise_admission_number
from apps.results.scores import CellInput, SheetData

MAX_BYTES = 1024 * 1024
FIXED = ["admission_number", "name"]


@dataclass
class ParsedUpload:
    cells: list[CellInput] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # student_id -> spreadsheet row number, for mapping save errors back to rows.
    row_of: dict[int, int] = field(default_factory=dict)


def safe_cell(value: object) -> str:
    """Stop spreadsheet apps treating a cell as a formula (CSV injection)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def header_names(data: SheetData) -> list[str]:
    return FIXED + [c.name for c in data.components]


def sheet_to_csv(data: SheetData) -> str:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(header_names(data))
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

    expected = [h.strip().lower() for h in header_names(data)]
    got = [h.strip().lower() for h in rows[0]]
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
    for n, cols in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in cols):
            continue  # fully empty rows (e.g. trailing ",,,,") are skipped
        if len(cols) != width:
            result.errors.append(f"Row {n}: expected {width} columns, found {len(cols)}.")
            continue
        first = cols[0].strip()
        if first.startswith("'"):  # undo the download's formula guard
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
