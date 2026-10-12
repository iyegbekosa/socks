import io
from datetime import date, datetime

import openpyxl
import pytest

from apps.imports.parsing import (
    MAX_ROWS,
    ImportFileError,
    TooManyRows,
    cell_date,
    cell_phone,
    cell_text,
    read_rows,
)


def csv_bytes(lines: list[str], *, bom: bool = False) -> bytes:
    text = "\r\n".join(lines) + "\r\n"
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def xlsx_bytes(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_csv_with_bom_maps_headers_and_numbers_rows_from_two():
    data = csv_bytes(["First Name,last_name", "Ada,Eze", "Bola,Yusuf"], bom=True)
    rows = list(read_rows(io.BytesIO(data), "students.csv"))
    assert rows == [
        (2, {"first_name": "Ada", "last_name": "Eze"}),
        (3, {"first_name": "Bola", "last_name": "Yusuf"}),
    ]


def test_csv_without_bom():
    rows = list(read_rows(io.BytesIO(csv_bytes(["first_name", "Ada"])), "s.csv"))
    assert rows == [(2, {"first_name": "Ada"})]


@pytest.mark.parametrize("header", ["First Name", "first_name", " FIRST_NAME ", "first  name"])
def test_header_variants(header):
    rows = list(read_rows(io.BytesIO(csv_bytes([header, "Ada"])), "s.csv"))
    assert rows == [(2, {"first_name": "Ada"})]


def test_blank_rows_are_skipped_but_numbering_is_kept():
    data = csv_bytes(["first_name", "Ada", ",", "", "Bola"])
    rows = list(read_rows(io.BytesIO(data), "s.csv"))
    assert [n for n, _ in rows] == [2, 5]


def test_missing_required_columns():
    data = csv_bytes(["first_name", "Ada"])
    with pytest.raises(ImportFileError) as exc:
        list(read_rows(io.BytesIO(data), "s.csv", required=("first_name", "class")))
    assert "class" in exc.value.message


def test_non_utf8_csv_is_rejected():
    data = "first_name\r\nAdé\r\n".encode("latin-1")
    with pytest.raises(ImportFileError):
        list(read_rows(io.BytesIO(data), "s.csv"))


def test_unsupported_extension():
    with pytest.raises(ImportFileError):
        list(read_rows(io.BytesIO(b"x"), "s.xls"))


def test_xlsx_first_sheet_with_number_and_date_cells():
    data = xlsx_bytes(
        [
            ["First Name", "Guardian Phone", "Date of Birth"],
            ["Ada", 8031234567, datetime(2014, 9, 5)],
            ["Bola", 8031234567.0, date(2013, 1, 2)],
        ]
    )
    rows = list(read_rows(io.BytesIO(data), "s.xlsx"))
    assert [n for n, _ in rows] == [2, 3]
    first, second = rows[0][1], rows[1][1]
    assert cell_phone(first["guardian_phone"]) == "+2348031234567"
    assert cell_phone(second["guardian_phone"]) == "+2348031234567"
    assert cell_date(first["date_of_birth"]) == date(2014, 9, 5)
    assert cell_date(second["date_of_birth"]) == date(2013, 1, 2)


def test_corrupt_xlsx_is_a_file_error():
    with pytest.raises(ImportFileError):
        list(read_rows(io.BytesIO(b"not a zip"), "s.xlsx"))


def test_row_limit():
    ok = csv_bytes(["first_name"] + ["Ada"] * MAX_ROWS)
    assert len(list(read_rows(io.BytesIO(ok), "s.csv"))) == MAX_ROWS
    over = csv_bytes(["first_name"] + ["Ada"] * (MAX_ROWS + 1))
    with pytest.raises(TooManyRows) as exc:
        list(read_rows(io.BytesIO(over), "s.csv"))
    assert exc.value.message == "File has more than 2,000 rows"


def test_cell_text():
    assert cell_text(None) == ""
    assert cell_text(8031234567.0) == "8031234567"
    assert cell_text(8031234567) == "8031234567"
    assert cell_text(2.5) == "2.5"
    assert cell_text("  Ada  ") == "Ada"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("5/9/2014", date(2014, 9, 5)),
        ("05/09/2014", date(2014, 9, 5)),
        ("2014-09-05", date(2014, 9, 5)),
        (datetime(2014, 9, 5, 0, 0), date(2014, 9, 5)),
        (date(2014, 9, 5), date(2014, 9, 5)),
        (" 5/9/2014 ", date(2014, 9, 5)),
    ],
)
def test_cell_date(value, expected):
    assert cell_date(value) == expected


@pytest.mark.parametrize(
    "value", ["", None, "31/02/2014", "2014/09/05", "5-9-2014", 41000, "5/9/14"]
)
def test_cell_date_rejects(value):
    with pytest.raises(ValueError) as exc:
        cell_date(value)
    assert str(value) not in str(exc.value) or value in ("", None)


@pytest.mark.parametrize(
    "value",
    [8031234567, 8031234567.0, "8031234567", "08031234567", "0803 123 4567", "+2348031234567"],
)
def test_cell_phone(value):
    assert cell_phone(value) == "+2348031234567"


def test_cell_phone_blank_and_bad():
    assert cell_phone(None) == ""
    assert cell_phone("") == ""
    with pytest.raises(ValueError):
        cell_phone("12345")
