"""Migrations apply cleanly, match the ORM mapping and enforce the key invariants."""

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from northstar.database.migrations import MigrationError, apply_migrations, load_migrations
from northstar.database.models import Base

pytestmark = pytest.mark.db

TABLES = {
    "employees", "leave_types", "leave_entitlements", "leave_requests",
    "public_holidays", "leave_proposals", "rag_documents", "document_chunks",
}
NOW = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)


def test_migration_files_are_ordered():
    names = [m.name for m in load_migrations()]
    assert names == sorted(names) and names[0].startswith("001_")


def test_all_tables_and_view_exist(engine, db_schema):
    insp = inspect(engine)
    assert TABLES <= set(insp.get_table_names(schema=db_schema))
    assert "leave_balances" in insp.get_view_names(schema=db_schema)


def test_orm_mapping_matches_database(engine, db_schema):
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        db_cols = {c["name"] for c in insp.get_columns(table.name, schema=db_schema)}
        orm_cols = {c.name for c in table.columns}
        assert orm_cols == db_cols, f"{table.name}: ORM {orm_cols ^ db_cols}"


def test_rerun_is_noop(db_url, db_schema):
    from northstar.database.engine import make_engine

    eng = make_engine(db_url)
    assert apply_migrations(eng, db_schema) == []
    eng.dispose()


def test_modified_migration_is_detected(db_url, db_schema, tmp_path: Path):
    from northstar.database.engine import make_engine

    for m in load_migrations():
        (tmp_path / m.name).write_text(m.sql + "\n-- edited", encoding="utf-8")
    eng = make_engine(db_url)
    with pytest.raises(MigrationError):
        apply_migrations(eng, db_schema, tmp_path)
    eng.dispose()


def test_row_level_security_enabled(engine, db_schema):
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                 "WHERE n.nspname = :s AND c.relkind = 'r'"),
            {"s": db_schema},
        ).all()
    assert {name for name, rls in rows if rls} >= TABLES


# --- invariants ------------------------------------------------------------------------------

@pytest.fixture()
def conn(engine):
    """A connection whose changes are rolled back after each test."""
    with engine.connect() as c:
        tx = c.begin()
        c.execute(text(
            "INSERT INTO leave_types VALUES "
            "('ANNUAL','a','working',NULL,true,true,'4'),('SICK','s','working',10,true,true,'6'),"
            "('PARENTAL','p',NULL,NULL,false,false,'10')"))
        c.execute(text(
            "INSERT INTO employees VALUES ('E9001','Test','t@x','AUD','A','J','full_time',"
            "'2020-01-01','2020-03-31',NULL,'active')"))
        yield c
        tx.rollback()


def expect_violation(conn, sql, params=None):
    nested = conn.begin_nested()
    with pytest.raises(IntegrityError):
        conn.execute(text(sql), params or {})
    nested.rollback()


def test_assistant_request_requires_proposal(conn):
    expect_violation(conn,
        "INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, days, status, created_at, created_via)"
        " VALUES ('E9001','ANNUAL','2026-11-02','2026-11-02',1,'pending',:t,'assistant')", {"t": NOW})


def test_request_cannot_span_two_years(conn):
    expect_violation(conn,
        "INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, days, status, created_at, created_via)"
        " VALUES ('E9001','ANNUAL','2026-12-28','2027-01-08',5,'pending',:t,'portal')", {"t": NOW})


def test_invalid_status_rejected(conn):
    expect_violation(conn,
        "INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, days, status, created_at, created_via)"
        " VALUES ('E9001','ANNUAL','2026-11-02','2026-11-02',1,'approvedd',:t,'portal')", {"t": NOW})


def test_carry_over_only_for_annual(conn):
    expect_violation(conn, "INSERT INTO leave_entitlements VALUES ('E9001',2026,'SICK',10,2)")


def test_unknown_manager_rejected_at_commit(engine):
    with engine.connect() as c:
        tx = c.begin()
        c.execute(text(
            "INSERT INTO employees VALUES ('E9002','T','t2@x','AUD','A','J','full_time',"
            "'2020-01-01','2020-03-31','E9999','active')"))
        with pytest.raises(IntegrityError):
            tx.commit()


def test_one_request_per_proposal(conn):
    pid = uuid.uuid4()
    conn.execute(text(
        "INSERT INTO leave_proposals (proposal_id, conversation_id, employee_id, leave_type, start_date, end_date,"
        " days, status, created_at, expires_at) VALUES (:p, :c, 'E9001','ANNUAL','2026-11-02','2026-11-02',1,"
        " 'proposed', :t, :t + interval '30 minutes')"), {"p": pid, "c": uuid.uuid4(), "t": NOW})
    insert = ("INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, days, status, created_at,"
              " created_via, proposal_id) VALUES ('E9001','ANNUAL','2026-11-02','2026-11-02',1,'pending',:t,"
              " 'assistant', :p)")
    conn.execute(text(insert), {"t": NOW, "p": pid})
    expect_violation(conn, insert, {"t": NOW, "p": pid})


def test_balance_view_formula_and_sick_clamp(conn):
    conn.execute(text("INSERT INTO leave_entitlements VALUES ('E9001',2026,'ANNUAL',25,3),('E9001',2026,'SICK',10,0)"))
    rows = [
        ("ANNUAL", "2026-02-02", "2026-02-06", 5, "approved"),
        ("ANNUAL", "2026-03-02", "2026-03-03", 2, "pending"),
        ("ANNUAL", "2026-04-01", "2026-04-01", 1, "rejected"),
        ("ANNUAL", "2026-05-04", "2026-05-04", 1, "cancelled"),
        ("ANNUAL", "2025-12-01", "2025-12-05", 5, "approved"),  # other leave year
        ("SICK", "2026-01-05", "2026-01-16", 10, "approved"),
        ("SICK", "2026-02-09", "2026-02-11", 3, "approved"),
    ]
    for lt, s, e, d, st in rows:
        conn.execute(text(
            "INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, days, status, created_at,"
            " created_via) VALUES ('E9001', :lt, :s, :e, :d, :st, :t, 'portal')"),
            {"lt": lt, "s": date.fromisoformat(s), "e": date.fromisoformat(e), "d": d, "st": st, "t": NOW})
    bal = {r.leave_type: r for r in conn.execute(text(
        "SELECT * FROM leave_balances WHERE employee_id='E9001' AND year=2026")).all()}
    a = bal["ANNUAL"]
    assert (a.approved_days, a.pending_days, a.available_days) == (5, 2, 25 + 3 - 5 - 2)
    s = bal["SICK"]
    assert s.calculated_available_days == -3 and s.available_days == 0


def test_vector_and_keyword_search_columns(conn):
    conn.execute(text(
        "INSERT INTO rag_documents (document, title, file_type, domain, authority, authority_rank, sha256,"
        " embedding_model, chunk_count) VALUES ('d.docx','t','docx','leave','authoritative_policy',1,"
        " repeat('a',64),'m',1)"))
    vec = "[" + ",".join(["0.1"] * 768) + "]"
    conn.execute(text(
        "INSERT INTO document_chunks (document, chunk_index, section, content, content_hash, embedding)"
        " VALUES ('d.docx', 0, '4.4', 'წინასწარი შეტყობინება 5 სამუშაო დღე', repeat('b',64), CAST(:v AS vector))"),
        {"v": vec})
    row = conn.execute(text(
        "SELECT embedding <=> CAST(:v AS vector) AS dist, search_tsv @@ plainto_tsquery('simple','შეტყობინება') AS hit"
        " FROM document_chunks"), {"v": vec}).one()
    assert row.dist == pytest.approx(0.0, abs=1e-6)
    assert row.hit is True
