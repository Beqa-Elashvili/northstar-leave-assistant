"""SQLAlchemy ORM mapping of the schema defined in `migrations/`.

The SQL migrations are the source of truth for DDL; these classes only map the tables
for the repositories. `tests/test_schema.py` checks the two stay in sync.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, Computed, Date, DateTime, ForeignKey, Identity, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 768


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = "employees"

    employee_id: Mapped[str] = mapped_column(String(10), primary_key=True)
    full_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True)
    department_code: Mapped[str] = mapped_column(String(10))
    department_name: Mapped[str] = mapped_column(Text)
    job_title: Mapped[str] = mapped_column(Text)
    employment_type: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[date] = mapped_column(Date)
    probation_end_date: Mapped[date] = mapped_column(Date)
    manager_id: Mapped[str | None] = mapped_column(String(10), ForeignKey("employees.employee_id"))
    status: Mapped[str] = mapped_column(String(20))


class LeaveType(Base):
    __tablename__ = "leave_types"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    day_unit: Mapped[str | None] = mapped_column(String(10))
    annual_limit_days: Mapped[int | None] = mapped_column(Integer)
    self_service: Mapped[bool] = mapped_column(Boolean)
    assistant_supported: Mapped[bool] = mapped_column(Boolean)
    policy_reference: Mapped[str] = mapped_column(Text)


class LeaveEntitlement(Base):
    __tablename__ = "leave_entitlements"

    employee_id: Mapped[str] = mapped_column(String(10), ForeignKey("employees.employee_id"), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    leave_type: Mapped[str] = mapped_column(String(20), ForeignKey("leave_types.code"), primary_key=True)
    entitled_days: Mapped[int] = mapped_column(Integer)
    carried_over_days: Mapped[int] = mapped_column(Integer, default=0)


class LeaveProposal(Base):
    __tablename__ = "leave_proposals"

    proposal_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    employee_id: Mapped[str] = mapped_column(String(10), ForeignKey("employees.employee_id"))
    leave_type: Mapped[str] = mapped_column(String(20), ForeignKey("leave_types.code"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    created_request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("leave_requests.request_id", use_alter=True), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    request_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(10), ForeignKey("employees.employee_id"))
    leave_type: Mapped[str] = mapped_column(String(20), ForeignKey("leave_types.code"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_via: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(10), ForeignKey("employees.employee_id"))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("leave_proposals.proposal_id"), unique=True
    )


class PublicHoliday(Base):
    __tablename__ = "public_holidays"

    holiday_date: Mapped[date] = mapped_column(Date, primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class RagDocument(Base):
    __tablename__ = "rag_documents"

    document: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    doc_code: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(Text)
    file_type: Mapped[str] = mapped_column(String(10))
    domain: Mapped[str] = mapped_column(String(30))
    authority: Mapped[str] = mapped_column(String(30))
    authority_rank: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    embedding_model: Mapped[str] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    chunk_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    document: Mapped[str] = mapped_column(Text, ForeignKey("rag_documents.document", ondelete="CASCADE"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(Text)
    section_title: Mapped[str | None] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    search_tsv: Mapped[str] = mapped_column(TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
