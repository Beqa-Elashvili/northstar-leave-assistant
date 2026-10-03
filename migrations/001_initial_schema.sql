-- 001: core HR schema, conversation proposals and the RAG vector store.
--
-- Object names are intentionally NOT schema-qualified: the migration runner sets
-- `search_path` to the target schema (DB_SCHEMA, default `public`), then `public`
-- and `extensions` (where Supabase installs pgvector).

-- ---------------------------------------------------------------------------
-- Employees (employees.csv)
-- ---------------------------------------------------------------------------
CREATE TABLE employees (
    employee_id         varchar(10)  PRIMARY KEY CHECK (employee_id ~ '^E[0-9]{4}$'),
    full_name           text         NOT NULL,
    email               text         NOT NULL UNIQUE,
    department_code     varchar(10)  NOT NULL,
    department_name     text         NOT NULL,
    job_title           text         NOT NULL,
    employment_type     varchar(20)  NOT NULL,
    start_date          date         NOT NULL,
    -- Last day of probation, inclusive (data dictionary).
    probation_end_date  date         NOT NULL,
    -- Empty for department heads. Deferrable so a seed batch can be inserted in any order.
    manager_id          varchar(10)  REFERENCES employees (employee_id) DEFERRABLE INITIALLY DEFERRED,
    status              varchar(20)  NOT NULL,
    CONSTRAINT employees_probation_after_start CHECK (probation_end_date >= start_date),
    CONSTRAINT employees_not_own_manager CHECK (manager_id IS NULL OR manager_id <> employee_id)
);

CREATE INDEX employees_manager_idx ON employees (manager_id);

-- ---------------------------------------------------------------------------
-- Leave types (leave_types.csv)
-- ---------------------------------------------------------------------------
CREATE TABLE leave_types (
    code                 varchar(20)  PRIMARY KEY,
    name                 text         NOT NULL,
    -- NULL for PARENTAL (determined individually by HR).
    day_unit             varchar(10)  CHECK (day_unit IN ('working', 'calendar')),
    -- NULL when there is no fixed limit (ANNUAL is per employee; BEREAVEMENT/PARENTAL have no balance).
    annual_limit_days    integer      CHECK (annual_limit_days > 0),
    self_service         boolean      NOT NULL,
    assistant_supported  boolean      NOT NULL,
    policy_reference     text         NOT NULL,
    -- The assistant may only create types that are also self-service.
    CONSTRAINT leave_types_assistant_implies_self_service CHECK (NOT assistant_supported OR self_service)
);

-- ---------------------------------------------------------------------------
-- Annual entitlements (leave_entitlements.csv)
-- ---------------------------------------------------------------------------
CREATE TABLE leave_entitlements (
    employee_id        varchar(10) NOT NULL REFERENCES employees (employee_id),
    year               integer     NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    leave_type         varchar(20) NOT NULL REFERENCES leave_types (code),
    entitled_days      integer     NOT NULL CHECK (entitled_days >= 0),
    -- Initial carried-over amount (policy 4.7), NOT the remaining carried-over balance.
    carried_over_days  integer     NOT NULL DEFAULT 0 CHECK (carried_over_days >= 0),
    PRIMARY KEY (employee_id, year, leave_type),
    CONSTRAINT leave_entitlements_carry_over_annual_only
        CHECK (leave_type = 'ANNUAL' OR carried_over_days = 0)
);

-- ---------------------------------------------------------------------------
-- Conversation proposals (confirmation + idempotency, data dictionary)
-- ---------------------------------------------------------------------------
CREATE TABLE leave_proposals (
    proposal_id         uuid         PRIMARY KEY,
    conversation_id     uuid         NOT NULL,
    employee_id         varchar(10)  NOT NULL REFERENCES employees (employee_id),
    leave_type          varchar(20)  NOT NULL REFERENCES leave_types (code),
    start_date          date         NOT NULL,
    end_date            date         NOT NULL,
    days                integer      NOT NULL CHECK (days > 0),
    comment             text,
    status              varchar(20)  NOT NULL DEFAULT 'proposed'
                        CHECK (status IN ('proposed', 'confirmed', 'declined', 'expired')),
    -- Set when the proposal is confirmed; see FK added after leave_requests exists.
    created_request_id  bigint       UNIQUE,
    created_at          timestamptz  NOT NULL,
    expires_at          timestamptz  NOT NULL,
    resolved_at         timestamptz,
    CONSTRAINT leave_proposals_dates CHECK (end_date >= start_date),
    CONSTRAINT leave_proposals_expiry CHECK (expires_at > created_at),
    CONSTRAINT leave_proposals_confirmed_has_request
        CHECK ((status = 'confirmed') = (created_request_id IS NOT NULL))
);

CREATE INDEX leave_proposals_conversation_idx ON leave_proposals (employee_id, conversation_id);

-- ---------------------------------------------------------------------------
-- Leave requests (leave_requests.csv + requests created through MCP)
-- ---------------------------------------------------------------------------
CREATE TABLE leave_requests (
    -- Seeded IDs are inserted explicitly; the identity sequence is advanced by the seeder.
    request_id       bigint       GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    employee_id      varchar(10)  NOT NULL REFERENCES employees (employee_id),
    leave_type       varchar(20)  NOT NULL REFERENCES leave_types (code),
    start_date       date         NOT NULL,
    end_date         date         NOT NULL,
    days             integer      NOT NULL CHECK (days > 0),
    status           varchar(20)  NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
    created_at       timestamptz  NOT NULL,
    created_via      varchar(20)  NOT NULL CHECK (created_via IN ('portal', 'assistant', 'hr')),
    comment          text,
    -- Decision trail for approve / reject / cancel performed through the HR tools.
    decided_at       timestamptz,
    decided_by       varchar(10)  REFERENCES employees (employee_id),
    decision_reason  text,
    -- Database-level idempotency: one request per confirmed proposal.
    proposal_id      uuid         UNIQUE REFERENCES leave_proposals (proposal_id),
    CONSTRAINT leave_requests_dates CHECK (end_date >= start_date),
    -- Policy 2.1: a request belongs to the year of its first day; cross-year leave is split.
    CONSTRAINT leave_requests_single_year CHECK (date_part('year', start_date) = date_part('year', end_date)),
    -- Assistant requests may only originate from an explicitly confirmed proposal
    -- (they are inserted as status = 'pending' by the service layer).
    CONSTRAINT leave_requests_assistant_origin
        CHECK (created_via <> 'assistant' OR proposal_id IS NOT NULL)
);

CREATE INDEX leave_requests_balance_idx ON leave_requests (employee_id, leave_type, status, start_date);
CREATE INDEX leave_requests_dates_idx ON leave_requests (employee_id, start_date, end_date);

ALTER TABLE leave_proposals
    ADD CONSTRAINT leave_proposals_created_request_fk
    FOREIGN KEY (created_request_id) REFERENCES leave_requests (request_id);

-- ---------------------------------------------------------------------------
-- Official HR holiday list (public_holidays.csv, policy 2.3)
-- ---------------------------------------------------------------------------
CREATE TABLE public_holidays (
    holiday_date  date  PRIMARY KEY,
    name          text  NOT NULL
);

-- ---------------------------------------------------------------------------
-- Balance view (policy 5.1 / data dictionary formula)
-- available = entitled + carried_over - approved - pending, same employee/type/year;
-- rejected and cancelled excluded; SICK never shows a negative paid balance.
-- ---------------------------------------------------------------------------
CREATE VIEW leave_balances AS
SELECT
    e.employee_id,
    e.year,
    e.leave_type,
    e.entitled_days,
    e.carried_over_days,
    COALESCE(SUM(r.days) FILTER (WHERE r.status = 'approved'), 0)::integer AS approved_days,
    COALESCE(SUM(r.days) FILTER (WHERE r.status = 'pending'), 0)::integer  AS pending_days,
    (e.entitled_days + e.carried_over_days
        - COALESCE(SUM(r.days) FILTER (WHERE r.status IN ('approved', 'pending')), 0))::integer
        AS calculated_available_days,
    CASE
        WHEN e.leave_type = 'SICK' THEN GREATEST(
            e.entitled_days + e.carried_over_days
                - COALESCE(SUM(r.days) FILTER (WHERE r.status IN ('approved', 'pending')), 0), 0)
        ELSE e.entitled_days + e.carried_over_days
                - COALESCE(SUM(r.days) FILTER (WHERE r.status IN ('approved', 'pending')), 0)
    END::integer AS available_days
FROM leave_entitlements e
LEFT JOIN leave_requests r
       ON r.employee_id = e.employee_id
      AND r.leave_type = e.leave_type
      AND date_part('year', r.start_date) = e.year
GROUP BY e.employee_id, e.year, e.leave_type, e.entitled_days, e.carried_over_days;

-- ---------------------------------------------------------------------------
-- RAG: ingested documents and their chunks (pgvector)
-- ---------------------------------------------------------------------------
CREATE TABLE rag_documents (
    document          text         PRIMARY KEY,          -- file name, e.g. Leave_and_Absence_Policy_v4.0.docx
    title             text         NOT NULL,
    doc_code          text,                               -- e.g. HR-POL-02
    version           text,
    file_type         varchar(10)  NOT NULL CHECK (file_type IN ('docx', 'pdf')),
    domain            varchar(30)  NOT NULL,              -- leave, remote_work, security, learning, travel, general
    authority         varchar(30)  NOT NULL,              -- authoritative_policy, general_handbook, reference_faq
    authority_rank    integer      NOT NULL,              -- lower = higher precedence
    sha256            char(64)     NOT NULL,
    embedding_model   text         NOT NULL,
    chunk_count       integer      NOT NULL,
    ingested_at       timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE document_chunks (
    chunk_id       bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document       text         NOT NULL REFERENCES rag_documents (document) ON DELETE CASCADE,
    chunk_index    integer      NOT NULL,
    section        text,                                   -- article number, e.g. 4.4
    section_title  text,
    page           integer,                                -- PDFs only; NULL for DOCX
    content        text         NOT NULL,
    content_hash   char(64)     NOT NULL,
    embedding      vector(768)  NOT NULL,                  -- must equal EMBEDDING_DIM
    -- Georgian has no PostgreSQL stemmer: the 'simple' configuration gives exact-term matching.
    search_tsv     tsvector     GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    metadata       jsonb        NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (document, chunk_index)
);

CREATE INDEX document_chunks_embedding_idx ON document_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX document_chunks_tsv_idx ON document_chunks USING gin (search_tsv);
CREATE INDEX document_chunks_section_idx ON document_chunks (document, section);

-- ---------------------------------------------------------------------------
-- Supabase exposes the `public` schema through its REST API. Enable RLS without
-- policies so anon/authenticated API keys cannot read HR data; the backend connects
-- as the table owner over DATABASE_URL and is not affected.
-- ---------------------------------------------------------------------------
ALTER TABLE employees          ENABLE ROW LEVEL SECURITY;
ALTER TABLE leave_types        ENABLE ROW LEVEL SECURITY;
ALTER TABLE leave_entitlements ENABLE ROW LEVEL SECURITY;
ALTER TABLE leave_proposals    ENABLE ROW LEVEL SECURITY;
ALTER TABLE leave_requests     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public_holidays    ENABLE ROW LEVEL SECURITY;
ALTER TABLE rag_documents      ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks    ENABLE ROW LEVEL SECURITY;
-- Views run with the owner's rights by default; make this one respect the caller's RLS.
ALTER VIEW leave_balances SET (security_invoker = true);
