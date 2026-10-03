"""Deterministic import of the supplied CSV files into the database.

Modes:
- default (upsert): inserts or refreshes every CSV row; rows created later through the MCP
  server (requests with IDs beyond the CSV, proposals) are kept. Safe to re-run.
- reset: empties the HR tables first and reloads exactly the CSV state (useful to restart
  the evaluation scenarios from the documented 2026-10-19 snapshot).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import Engine, text
from sqlalchemy.dialects.postgresql import insert

from northstar.database.csv_loader import SeedData
from northstar.database.models import Employee, LeaveEntitlement, LeaveRequest, LeaveType, PublicHoliday


@dataclass(frozen=True)
class SeedSummary:
    employees: int
    leave_types: int
    entitlements: int
    requests: int
    holidays: int


def _upsert(conn, model, rows: list[dict], keys: list[str]) -> None:
    if not rows:
        return
    stmt = insert(model).values(rows)
    updates = {c: stmt.excluded[c] for c in rows[0] if c not in keys}
    conn.execute(stmt.on_conflict_do_update(index_elements=keys, set_=updates))


def seed_database(engine: Engine, data: SeedData, *, reset: bool = False) -> SeedSummary:
    """Load `data` in a single transaction; nothing is written if any step fails."""
    with engine.begin() as conn:
        if reset:
            conn.execute(text(
                "TRUNCATE leave_requests, leave_proposals, leave_entitlements, public_holidays,"
                " employees, leave_types RESTART IDENTITY CASCADE"
            ))
        _upsert(conn, LeaveType, [asdict(r) for r in data.leave_types], ["code"])
        # manager_id FK is DEFERRABLE INITIALLY DEFERRED, so order within the batch does not matter.
        _upsert(conn, Employee, [asdict(r) for r in data.employees], ["employee_id"])
        _upsert(conn, PublicHoliday, [asdict(r) for r in data.holidays], ["holiday_date"])
        _upsert(conn, LeaveEntitlement, [asdict(r) for r in data.entitlements], ["employee_id", "year", "leave_type"])
        _upsert(conn, LeaveRequest, [asdict(r) for r in data.requests], ["request_id"])
        # Seeded IDs were inserted explicitly: move the identity past them so new requests get 28, 29, ...
        conn.execute(text(
            "SELECT setval(pg_get_serial_sequence('leave_requests', 'request_id'),"
            " GREATEST((SELECT COALESCE(MAX(request_id), 0) FROM leave_requests), 1),"
            " (SELECT COUNT(*) > 0 FROM leave_requests))"
        ))
    return SeedSummary(
        employees=len(data.employees), leave_types=len(data.leave_types), entitlements=len(data.entitlements),
        requests=len(data.requests), holidays=len(data.holidays),
    )
