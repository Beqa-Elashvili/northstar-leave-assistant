"""CSV parsing and validation (no database needed)."""

import shutil
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from northstar.config import DATA_DIR
from northstar.database.csv_loader import CsvValidationError, load_seed_data, parse_bool, parse_date

TZ = ZoneInfo("Asia/Tbilisi")


@pytest.mark.parametrize("raw, expected", [
    ("3/4/2019", date(2019, 3, 4)),     # supplied files: M/D/YYYY
    ("11/30/2026", date(2026, 11, 30)),
    ("2026-10-19", date(2026, 10, 19)),  # data dictionary: ISO
])
def test_parse_date_formats(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["13/1/2026", "2/30/2026", "2026/10/19", "", "yesterday"])
def test_parse_date_rejects_invalid(raw):
    with pytest.raises(ValueError):
        parse_date(raw)


def test_parse_bool():
    assert parse_bool("1") is True and parse_bool("0") is False
    with pytest.raises(ValueError):
        parse_bool("yes")


def test_supplied_files_load_and_validate():
    data = load_seed_data(DATA_DIR, TZ)
    assert (len(data.employees), len(data.leave_types), len(data.entitlements),
            len(data.requests), len(data.holidays)) == (14, 6, 56, 27, 34)


def test_employee_fields(seed_data):
    e = {r.employee_id: r for r in seed_data.employees}
    nino = e["E1001"]
    assert nino.full_name == "ნინო ბერიძე"
    assert nino.department_code == "AUD" and nino.manager_id == "E1010"
    assert nino.start_date == date(2019, 3, 4) and nino.probation_end_date == date(2019, 6, 3)
    assert e["E1010"].manager_id is None  # department head
    assert e["E1004"].probation_end_date == date(2026, 11, 30)
    assert all(r.employment_type == "full_time" and r.status == "active" for r in seed_data.employees)


def test_leave_type_fields(seed_data):
    t = {r.code: r for r in seed_data.leave_types}
    assert set(t) == {"ANNUAL", "SICK", "UNPAID", "BEREAVEMENT", "STUDY", "PARENTAL"}
    assert t["ANNUAL"].annual_limit_days is None and t["ANNUAL"].day_unit == "working"
    assert t["SICK"].annual_limit_days == 10
    assert t["UNPAID"].day_unit == "calendar" and t["UNPAID"].annual_limit_days == 30
    assert t["PARENTAL"].day_unit is None and not t["PARENTAL"].self_service
    assert {c for c, r in t.items() if r.assistant_supported} == {"ANNUAL", "SICK", "UNPAID"}
    assert "მუხლი 4" in t["ANNUAL"].policy_reference


def test_request_fields(seed_data):
    r = {x.request_id: x for x in seed_data.requests}
    assert r[7].comment == "ხელმძღვანელის თანხმობით, 15 სამუშაო დღე"  # quoted comma
    assert r[2].comment is None
    assert r[1].created_at == datetime(2026, 1, 20, 10, 16, tzinfo=TZ)
    assert r[5].status == "pending" and r[8].status == "rejected" and r[3].status == "cancelled"
    assert all(x.created_via == "portal" for x in seed_data.requests)


def _copy_data(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    shutil.copytree(DATA_DIR, target)
    return target


def _rewrite(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")


def test_unknown_manager_detected(tmp_path):
    d = _copy_data(tmp_path)
    _rewrite(d / "employees.csv", ",E1010,active", ",E1999,active")
    with pytest.raises(CsvValidationError, match="unknown manager"):
        load_seed_data(d, TZ)


def test_unknown_employee_in_requests_detected(tmp_path):
    d = _copy_data(tmp_path)
    _rewrite(d / "leave_requests.csv", "1,E1001,", "1,E9999,")
    with pytest.raises(CsvValidationError, match="request 1"):
        load_seed_data(d, TZ)


def test_bad_row_reports_file_and_line(tmp_path):
    d = _copy_data(tmp_path)
    _rewrite(d / "public_holidays.csv", "1/7/2026", "1/77/2026")
    with pytest.raises(CsvValidationError, match=r"public_holidays.csv line 4"):
        load_seed_data(d, TZ)


def test_wrong_header_detected(tmp_path):
    d = _copy_data(tmp_path)
    _rewrite(d / "leave_types.csv", "day_unit", "unit")
    with pytest.raises(CsvValidationError, match="unexpected header"):
        load_seed_data(d, TZ)


def test_duplicate_request_id_detected(tmp_path):
    d = _copy_data(tmp_path)
    _rewrite(d / "leave_requests.csv", "\n2,E1001,", "\n1,E1001,")
    with pytest.raises(CsvValidationError, match="duplicate request_id"):
        load_seed_data(d, TZ)
