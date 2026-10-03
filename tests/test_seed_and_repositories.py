"""CSV import into PostgreSQL and the repository layer."""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from northstar.database.models import (
    Employee, LeaveEntitlement, LeaveProposal, LeaveRequest, LeaveType, PublicHoliday,
)
from northstar.database.repositories import (
    EmployeeRepository, EntitlementRepository, HolidayRepository, LeaveRequestRepository,
    LeaveTypeRepository, ProposalRepository, RequestFilter,
)
from northstar.database.seed import seed_database

pytestmark = pytest.mark.db
TZ = ZoneInfo("Asia/Tbilisi")


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


# --- import ------------------------------------------------------------------------------------

def test_row_counts(session):
    assert count(session, Employee) == 14
    assert count(session, LeaveType) == 6
    assert count(session, LeaveEntitlement) == 56
    assert count(session, LeaveRequest) == 27
    assert count(session, PublicHoliday) == 34


def test_employee_import(session):
    repo = EmployeeRepository(session)
    nino = repo.get("E1001")
    assert nino.full_name == "ნინო ბერიძე" and nino.email == "nino.beridze@northstar.example"
    assert nino.department_name == "აუდიტი და მარწმუნებელი მომსახურება"
    assert nino.probation_end_date == date(2019, 6, 3)
    assert repo.get("E9999") is None


def test_manager_relationships(session):
    managers = dict(session.execute(select(Employee.employee_id, Employee.manager_id)).all())
    heads = {e for e, m in managers.items() if m is None}
    assert heads == {"E1010", "E1011", "E1012", "E1013", "E1014"}
    assert all(m in managers for m in managers.values() if m)
    assert managers["E1004"] == "E1013" and managers["E1007"] == "E1014"


def test_leave_type_import(session):
    types = {t.code: t for t in LeaveTypeRepository(session).list_all()}
    assert len(types) == 6
    assert types["SICK"].annual_limit_days == 10 and types["SICK"].assistant_supported
    assert types["BEREAVEMENT"].self_service and not types["BEREAVEMENT"].assistant_supported
    assert types["STUDY"].annual_limit_days == 5 and not types["STUDY"].assistant_supported
    assert types["PARENTAL"].day_unit is None and not types["PARENTAL"].self_service


def test_entitlement_import(session):
    repo = EntitlementRepository(session)
    annual = repo.get("E1001", 2026, "ANNUAL")
    assert (annual.entitled_days, annual.carried_over_days) == (25, 3)
    assert repo.get("E1004", 2026, "ANNUAL").entitled_days == 8
    assert repo.get("E1001", 2026, "BEREAVEMENT") is None  # no annual balance
    assert [e.leave_type for e in repo.list_for_employee("E1001", 2026)] == ["ANNUAL", "SICK", "STUDY", "UNPAID"]


def test_request_import(session):
    repo = LeaveRequestRepository(session)
    r = repo.get(7)
    assert (r.employee_id, r.leave_type, r.start_date, r.end_date, r.days, r.status) == \
        ("E1002", "ANNUAL", date(2026, 8, 3), date(2026, 8, 21), 15, "approved")
    assert r.comment == "ხელმძღვანელის თანხმობით, 15 სამუშაო დღე"
    assert r.created_at == datetime(2026, 6, 15, 10, 22, tzinfo=TZ)
    assert r.created_via == "portal"


def test_holiday_import(session):
    dates = HolidayRepository(session).dates()
    assert date(2026, 10, 14) in dates and date(2026, 11, 23) in dates
    assert date(2027, 4, 30) in dates
    assert date(2026, 12, 25) not in dates  # only the official HR list


def test_identity_sequence_continues_after_seeded_ids(session):
    proposal = LeaveProposal(
        proposal_id=uuid.uuid4(), conversation_id=uuid.uuid4(), employee_id="E1001", leave_type="ANNUAL",
        start_date=date(2026, 11, 2), end_date=date(2026, 11, 2), days=1, status="proposed",
        created_at=datetime(2026, 10, 19, 10, tzinfo=TZ), expires_at=datetime(2026, 10, 19, 11, tzinfo=TZ),
    )
    ProposalRepository(session).add(proposal)
    new = LeaveRequestRepository(session).add(LeaveRequest(
        employee_id="E1001", leave_type="ANNUAL", start_date=date(2026, 11, 2), end_date=date(2026, 11, 2),
        days=1, status="pending", created_at=datetime(2026, 10, 19, 10, tzinfo=TZ), created_via="assistant",
        proposal_id=proposal.proposal_id,
    ))
    assert new.request_id == 28


def test_upsert_rerun_is_idempotent_and_keeps_new_requests(seeded_engine, seed_data):
    with Session(seeded_engine) as s:
        s.add(LeaveRequest(
            employee_id="E1003", leave_type="ANNUAL", start_date=date(2026, 12, 7), end_date=date(2026, 12, 7),
            days=1, status="pending", created_at=datetime(2026, 10, 19, 10, tzinfo=TZ), created_via="portal",
        ))
        s.commit()
    seed_database(seeded_engine, seed_data)  # second run, upsert mode
    seed_database(seeded_engine, seed_data)
    with Session(seeded_engine) as s:
        assert count(s, Employee) == 14 and count(s, LeaveEntitlement) == 56
        assert count(s, LeaveRequest) == 28  # 27 CSV rows + the one added above, no duplicates


def test_reset_restores_csv_snapshot(seeded_engine, seed_data):
    with seeded_engine.begin() as conn:
        conn.execute(text("UPDATE leave_requests SET status = 'approved' WHERE request_id = 5"))
    seed_database(seeded_engine, seed_data, reset=True)
    with Session(seeded_engine) as s:
        assert LeaveRequestRepository(s).get(5).status == "pending"
        assert count(s, LeaveRequest) == 27


def test_failed_seed_writes_nothing(seeded_engine, seed_data):
    from dataclasses import replace

    broken = replace(seed_data, requests=seed_data.requests + [replace(seed_data.requests[0], request_id=99, days=0)])
    with pytest.raises(Exception):
        seed_database(seeded_engine, broken, reset=True)  # days > 0 check fails on the last statement
    with Session(seeded_engine) as s:
        assert count(s, LeaveRequest) == 27  # transaction rolled back, previous state intact


# --- repository queries ------------------------------------------------------------------------

def test_used_days_counts_only_approved_and_pending_same_year(session):
    repo = LeaveRequestRepository(session)
    used = repo.used_days("E1001", "ANNUAL", 2026)
    assert (used.approved, used.pending) == (15, 3)       # #1 + #4 approved, #5 pending, #3 cancelled ignored
    used = repo.used_days("E1002", "ANNUAL", 2026)
    assert (used.approved, used.pending) == (20, 0)       # #8 rejected ignored
    assert repo.used_days("E1001", "ANNUAL", 2027).approved == 0


def test_list_filters(session):
    repo = LeaveRequestRepository(session)
    assert [r.request_id for r in repo.list(RequestFilter(employee_id="E1001"))] == [1, 2, 3, 4, 5]
    assert [r.request_id for r in repo.list(RequestFilter(employee_id="E1001", statuses=("pending",)))] == [5]
    in_july = repo.list(RequestFilter(date_from=date(2026, 7, 1), date_to=date(2026, 7, 31)))
    assert {r.request_id for r in in_july} == {4, 14, 17, 25, 27}
    assert all(r.leave_type == "SICK" for r in repo.list(RequestFilter(leave_type="SICK")))


def test_active_overlapping_ignores_rejected_and_cancelled(session):
    repo = LeaveRequestRepository(session)
    assert [r.request_id for r in repo.active_overlapping("E1001", date(2026, 11, 11), date(2026, 11, 13))] == [5]
    assert repo.active_overlapping("E1001", date(2026, 5, 4), date(2026, 5, 5)) == []      # #3 cancelled
    assert repo.active_overlapping("E1002", date(2026, 10, 26), date(2026, 10, 30)) == []  # #8 rejected
