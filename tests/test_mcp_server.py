"""Every MCP tool exercised through a real MCP client (in-memory transport), incl. authorization."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from northstar.clock import FrozenClock
from northstar.database.models import LeaveProposal, LeaveRequest
from northstar.services.authorization import Role
from tests.mcp_helpers import CLOCK, TZ, McpHarness, ToolCallError

pytestmark = pytest.mark.db

EXPECTED_TOOLS = {
    "get_my_profile", "list_leave_types", "get_leave_balance", "list_leave_requests", "propose_leave_request",
    "create_leave_request", "decline_leave_proposal", "approve_leave_request", "reject_leave_request",
    "cancel_leave_request",
}


@pytest.fixture()
def nino(seeded_engine):
    return McpHarness(seeded_engine, "E1001")


@pytest.fixture()
def hr(seeded_engine):
    return McpHarness(seeded_engine, "E1007", Role.HR)


def request_count(engine) -> int:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(LeaveRequest))


def propose(h, **kw):
    args = {"conversation_id": str(uuid.uuid4()), "leave_type": "ANNUAL",
            "start_date": "2026-10-27", "end_date": "2026-10-30"}
    args.update(kw)
    return h.call("propose_leave_request", **args)


def error_code(h, tool, **kw):
    with pytest.raises(ToolCallError) as exc:
        h.call(tool, **kw)
    return exc.value


# --- tool catalogue / schemas ------------------------------------------------------------------

def test_tool_catalogue_and_typed_schemas(nino):
    tools = {t.name: t for t in nino.list_tools()}
    assert set(tools) == EXPECTED_TOOLS
    for t in tools.values():
        assert t.output_schema is not None, t.name
    # Identity is never an argument of the creation tools.
    assert "employee_id" not in tools["propose_leave_request"].input_schema["properties"]
    assert "employee_id" not in tools["create_leave_request"].input_schema["properties"]
    assert tools["get_leave_balance"].annotations.read_only_hint is True


def test_get_my_profile(nino):
    p = nino.call("get_my_profile")
    assert (p["employee_id"], p["full_name"], p["role"]) == ("E1001", "ნინო ბერიძე", "employee")


def test_list_leave_types(nino):
    types = {t["code"]: t for t in nino.call("list_leave_types")["leave_types"]}
    assert set(types) == {"ANNUAL", "SICK", "UNPAID", "BEREAVEMENT", "STUDY", "PARENTAL"}
    assert {c for c, t in types.items() if t["assistant_supported"]} == {"ANNUAL", "SICK", "UNPAID"}
    assert types["PARENTAL"]["self_service"] is False


# --- balance (Scenario 1 & 8) -------------------------------------------------------------------

def test_scenario_1_own_annual_balance(nino):
    out = nino.call("get_leave_balance", leave_type="ANNUAL")
    assert out["employee_id"] == "E1001" and out["year"] == 2026
    (b,) = out["balances"]
    assert (b["entitled_days"], b["carried_over_days"], b["approved_days"], b["pending_days"], b["available_days"]) \
        == (25, 3, 15, 3, 10)


def test_all_own_balances(nino):
    out = nino.call("get_leave_balance", year=2026)
    assert {b["leave_type"]: b["available_days"] for b in out["balances"]} == \
        {"ANNUAL": 10, "SICK": 8, "STUDY": 5, "UNPAID": 30}


def test_scenario_8_other_employee_balance_rejected(nino):
    err = error_code(nino, "get_leave_balance", employee_id="E1002")
    assert err.code == "permission_denied"
    assert "24" not in str(err.payload) and "available" not in str(err.payload)


def test_explicit_own_id_allowed(nino):
    assert nino.call("get_leave_balance", employee_id="e1001", leave_type="SICK")["balances"][0]["available_days"] == 8


def test_hr_can_read_other_balances(hr):
    assert hr.call("get_leave_balance", employee_id="E1002", leave_type="ANNUAL")["balances"][0]["available_days"] == 4


def test_balance_errors(nino):
    assert error_code(nino, "get_leave_balance", leave_type="BEREAVEMENT").code == "no_balance_for_leave_type"
    assert error_code(nino, "get_leave_balance", year=2027).code == "entitlement_not_found"
    assert error_code(nino, "get_leave_balance", leave_type="HOLIDAY").code == "tool_error"  # schema validation


# --- list requests -----------------------------------------------------------------------------

def test_list_own_requests_and_filters(nino):
    assert [r["request_id"] for r in nino.call("list_leave_requests")["requests"]] == [1, 2, 3, 4, 5]
    assert [r["request_id"] for r in nino.call("list_leave_requests", status=["pending"])["requests"]] == [5]
    july = nino.call("list_leave_requests", date_from="2026-07-01", date_to="2026-07-31")["requests"]
    assert [r["request_id"] for r in july] == [4]


def test_list_other_employee_rejected(nino):
    assert error_code(nino, "list_leave_requests", employee_id="E1002").code == "permission_denied"


def test_hr_lists_everyone(hr):
    assert hr.call("list_leave_requests", limit=200)["count"] == 27
    assert [r["request_id"] for r in hr.call("list_leave_requests", employee_id="E1002")["requests"]] == [6, 7, 8]


def test_invalid_date_range(nino):
    assert error_code(nino, "list_leave_requests", date_from="2026-08-01", date_to="2026-07-01").code \
        == "invalid_date_range"


# --- create via proposal (Scenario 2, 3, 4, 10) ------------------------------------------------

def test_scenario_2_annual_proposal_then_confirmation(nino, seeded_engine):
    p = propose(nino)
    assert p["outcome"] == "awaiting_confirmation" and p["days"] == 4
    assert (p["available_days_before"], p["available_days_after"]) == (10, 6)
    assert request_count(seeded_engine) == 27  # nothing created before confirmation

    created = nino.call("create_leave_request", proposal_id=p["proposal_id"])
    r = created["request"]
    assert created["already_existed"] is False
    assert (r["request_id"], r["employee_id"], r["status"], r["created_via"], r["days"]) == \
        (28, "E1001", "pending", "assistant", 4)
    assert nino.call("get_leave_balance", leave_type="ANNUAL")["balances"][0]["pending_days"] == 3 + 4


def test_scenario_2_original_dates_violate_notice(nino):
    p = propose(nino, start_date="2026-10-26")
    assert p["outcome"] == "not_allowed" and p["proposal_id"] is None
    (v,) = p["violations"]
    assert (v["code"], v["article"], v["details"]["earliest_start_date"]) == ("notice_period", "4.4", "2026-10-27")


def test_scenario_10_duplicate_confirmation_is_idempotent(nino, seeded_engine):
    p = propose(nino)
    first = nino.call("create_leave_request", proposal_id=p["proposal_id"])
    second = nino.call("create_leave_request", proposal_id=p["proposal_id"])
    assert second["already_existed"] is True
    assert second["request"]["request_id"] == first["request"]["request_id"]
    assert request_count(seeded_engine) == 28


def test_scenario_3_unpaid_reason_flow(nino, seeded_engine):
    conv = str(uuid.uuid4())
    p = propose(nino, conversation_id=conv, leave_type="UNPAID", start_date="2026-11-03", end_date="2026-11-06")
    assert p["outcome"] == "needs_input" and p["violations"][0]["code"] == "reason_required"
    p = propose(nino, conversation_id=conv, leave_type="UNPAID", start_date="2026-11-03", end_date="2026-11-06",
                comment="ოჯახური მიზეზი")
    assert p["outcome"] == "awaiting_confirmation" and p["days"] == 4 and p["day_unit"] == "calendar"
    r = nino.call("create_leave_request", proposal_id=p["proposal_id"])["request"]
    assert r["comment"] == "ოჯახური მიზეზი" and r["leave_type"] == "UNPAID"


def test_scenario_4_sick_over_paid_balance_not_created(nino, seeded_engine):
    p = propose(nino, leave_type="SICK", start_date="2026-10-19", end_date="2026-10-29")
    assert p["outcome"] == "not_allowed" and p["proposal_id"] is None
    v = p["violations"][0]
    assert (v["code"], v["redirect_to"], v["article"]) == ("sick_paid_limit_exceeded", "hr", "6.4")
    assert request_count(seeded_engine) == 27


def test_sick_within_balance_is_created(nino):
    p = propose(nino, leave_type="SICK", start_date="2026-10-19", end_date="2026-10-20")
    assert p["outcome"] == "awaiting_confirmation"
    assert nino.call("create_leave_request", proposal_id=p["proposal_id"])["request"]["leave_type"] == "SICK"


@pytest.mark.parametrize("lt", ["BEREAVEMENT", "STUDY", "PARENTAL"])
def test_unsupported_types_never_create(nino, seeded_engine, lt):
    p = propose(nino, leave_type=lt, start_date="2026-11-16", end_date="2026-11-16")
    assert p["outcome"] == "not_allowed" and p["proposal_id"] is None
    assert p["violations"][0]["code"] == "assistant_unsupported_type"
    with Session(seeded_engine) as s:
        assert s.scalar(select(func.count()).select_from(LeaveProposal)) == 0


def test_injected_employee_id_is_ignored(nino, seeded_engine):
    p = propose(nino, employee_id="E1002")  # not part of the schema -> dropped by the server
    r = nino.call("create_leave_request", proposal_id=p["proposal_id"])["request"]
    assert r["employee_id"] == "E1001"


def test_other_employee_cannot_confirm_my_proposal(nino, seeded_engine):
    p = propose(nino)
    ana = McpHarness(seeded_engine, "E1005")
    assert error_code(ana, "create_leave_request", proposal_id=p["proposal_id"]).code == "proposal_not_found"
    assert request_count(seeded_engine) == 27


def test_unknown_proposal(nino):
    assert error_code(nino, "create_leave_request", proposal_id=str(uuid.uuid4())).code == "proposal_not_found"


def test_declined_proposal_cannot_be_confirmed(nino):
    p = propose(nino)
    assert nino.call("decline_leave_proposal", proposal_id=p["proposal_id"])["status"] == "declined"
    assert error_code(nino, "create_leave_request", proposal_id=p["proposal_id"]).code == "proposal_not_confirmable"


def test_new_proposal_supersedes_previous_in_same_conversation(nino):
    conv = str(uuid.uuid4())
    first = propose(nino, conversation_id=conv)
    propose(nino, conversation_id=conv, start_date="2026-11-02", end_date="2026-11-03")
    assert error_code(nino, "create_leave_request", proposal_id=first["proposal_id"]).code == "proposal_not_confirmable"


def test_expired_proposal(nino, seeded_engine):
    p = propose(nino)
    later = McpHarness(seeded_engine, "E1001", clock=FrozenClock(CLOCK.now() + timedelta(minutes=31)))
    assert error_code(later, "create_leave_request", proposal_id=p["proposal_id"]).code == "proposal_not_confirmable"


def test_rules_rechecked_at_confirmation(nino, seeded_engine):
    p = propose(nino)
    with seeded_engine.begin() as conn:  # e.g. the employee booked overlapping leave in the portal meanwhile
        conn.execute(text("INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, days, status,"
                          " created_at, created_via) VALUES ('E1001','ANNUAL','2026-10-29','2026-10-29',1,"
                          " 'pending', now(), 'portal')"))
    err = error_code(nino, "create_leave_request", proposal_id=p["proposal_id"])
    assert err.code == "proposal_rules_changed"
    assert err.payload["details"]["violations"][0]["code"] == "overlapping_request"


# --- HR-only actions (Scenario 9) --------------------------------------------------------------

@pytest.mark.parametrize("tool, extra", [
    ("approve_leave_request", {}),
    ("reject_leave_request", {"reason": "x"}),
    ("cancel_leave_request", {}),
])
def test_employee_cannot_decide_or_cancel(nino, seeded_engine, tool, extra):
    err = error_code(nino, tool, request_id=5, **extra)
    assert err.code == "permission_denied" and "4.8" in err.payload["article"]
    with Session(seeded_engine) as s:
        assert s.get(LeaveRequest, 5).status == "pending"


def test_hr_approve(hr):
    out = hr.call("approve_leave_request", request_id=5, comment="ok")
    assert (out["previous_status"], out["request"]["status"], out["request"]["decided_by"]) == \
        ("pending", "approved", "E1007")
    assert error_code(hr, "approve_leave_request", request_id=5).code == "invalid_status_transition"


def test_hr_reject_stores_reason(hr):
    out = hr.call("reject_leave_request", request_id=15, reason="კლიენტის პროექტის ვადა")
    assert out["request"]["status"] == "rejected"
    assert out["request"]["decision_reason"] == "კლიენტის პროექტის ვადა"


def test_hr_reject_requires_reason(hr):
    assert error_code(hr, "reject_leave_request", request_id=15, reason="   ").code == "invalid_status_transition"


def test_hr_cancel_approved(hr):
    assert hr.call("cancel_leave_request", request_id=4)["request"]["status"] == "cancelled"
    assert error_code(hr, "cancel_leave_request", request_id=3).code == "invalid_status_transition"  # already cancelled
    assert error_code(hr, "cancel_leave_request", request_id=9999).code == "request_not_found"


# --- identity ------------------------------------------------------------------------------------

def test_hr_role_requires_hr_department(seeded_engine):
    fake_hr = McpHarness(seeded_engine, "E1001", Role.HR)
    assert error_code(fake_hr, "approve_leave_request", request_id=5).code == "authentication_error"


def test_unknown_identity_rejected(seeded_engine):
    ghost = McpHarness(seeded_engine, "E9999")
    assert error_code(ghost, "get_my_profile").code == "authentication_error"
