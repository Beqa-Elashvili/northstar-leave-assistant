# Architecture (Phase 1 design)

```
                     ┌──────────────────────┐
                     │  Georgian CLI (rich) │  northstar.cli
                     └──────────┬───────────┘
                                │
                     ┌──────────▼───────────┐
                     │ Agent: LLM + routing │  northstar.agent   (LLMProvider → GeminiProvider)
                     │  + conversation state│
                     └─────┬──────────┬─────┘
              policy       │          │      balance / requests / leave types
                ┌──────────▼──┐   ┌───▼──────────────┐
                │     RAG     │   │    MCP client    │  stdio session
                │ northstar.rag│  └───┬──────────────┘
                └──────┬──────┘       │  MCP protocol (typed tools)
                       │          ┌───▼──────────────┐
                       │          │    MCP server    │  northstar.mcp   (authN context + authZ)
                       │          └───┬──────────────┘
                       │          ┌───▼──────────────┐
                       │          │ Services / rules │  northstar.services, northstar.policies
                       │          └───┬──────────────┘
                       │          ┌───▼──────────────┐
                       │          │   Repositories   │  northstar.database (SQLAlchemy)
                       │          └───┬──────────────┘
                       ▼              ▼
              ┌────────────────────────────────────────┐
              │ Supabase PostgreSQL  (+ pgvector)      │
              │ HR tables · proposals · document_chunks│
              └────────────────────────────────────────┘
```

The core principles:

| Layer | Responsibility | Not trusted with |
|---|---|---|
| LLM | Language understanding, intent, leave type, date extraction, Georgian wording | Permissions, balances, dates arithmetic, rule checks |
| RAG | Company policy knowledge with citations | Operational employee data |
| MCP server | The only path for leave actions; resolves the caller identity and role | — |
| Services / policies | Deterministic leave rules (`LeaveDayCalculator`, validators) | — |
| Database | Operational truth; constraints as a last line of defence | — |

## Package layout

```
src/northstar/
  config.py        settings from env (.env), secrets as SecretStr
  clock.py         business date provider (APP_TODAY) – get_app_today()
  database/        engine (search_path), ORM models, migration runner, repositories
  domain/          enums, value objects, typed errors (with policy article + Georgian message)
  services/        LeaveDayCalculator, BalanceService, LeaveRequestService, ProposalService
  policies/        rule validators (probation, notice, max length, AUD periods, overlap, …)
  mcp/             MCP server (tools + typed schemas), auth context, MCP client wrapper
  rag/             DOCX/PDF extraction, structural chunking, embeddings, hybrid retrieval
  agent/           LLM provider abstraction, intent routing, conversation state, Georgian replies
  cli/             interactive terminal UI
migrations/        versioned SQL (source of truth for DDL)
scripts/           migrate, seed_database, ingest_documents, check_setup
```

## Database schema

| Table | Source | Notes |
|---|---|---|
| `employees` | employees.csv | Self-referencing `manager_id` FK (deferrable, so seed order does not matter) |
| `leave_types` | leave_types.csv | `day_unit` NULL for PARENTAL; check `assistant_supported ⇒ self_service` |
| `leave_entitlements` | leave_entitlements.csv | PK (employee, year, type); `carried_over_days` only for ANNUAL |
| `leave_requests` | leave_requests.csv + MCP | Identity PK (seed keeps CSV IDs, sequence advanced); `decided_by/at/reason` for HR decisions; `proposal_id UNIQUE`; single-year check (policy 2.1); `created_via='assistant'` requires a proposal |
| `public_holidays` | public_holidays.csv | The only holiday source (policy 2.3) |
| `leave_proposals` | runtime | Confirmation + idempotency, keyed by `employee_id`, `conversation_id`, `proposal_id`; expires |
| `leave_balances` (view) | derived | Policy 5.1 formula, SICK clamped at 0, also exposes the raw value |
| `rag_documents`, `document_chunks` | ingestion | `vector(768)` with HNSW cosine index; `tsvector('simple')` GIN index for exact Georgian terms and article numbers |

All tables have Row Level Security enabled with no policies: Supabase's REST API (anon /
authenticated keys) cannot read HR data, while the backend connects over `DATABASE_URL` as the
table owner.

Idempotency is enforced twice: the service locks the proposal row (`SELECT … FOR UPDATE`) and
returns the stored `created_request_id` on a repeated confirmation; the `UNIQUE (proposal_id)`
constraint on `leave_requests` makes a duplicate insert impossible even under a race.

Confirmation flow (spec 21, 29, 30): `propose_leave_request(conversation_id, leave_type, start_date,
end_date, comment)` checks every rule and stores a proposal (30-minute TTL) without creating anything.
The CLI shows the summary and waits for an explicit "დიახ". Only then does it call
`create_leave_request(leave_type, start_date, end_date, comment, proposal_id)`. The server checks
that the data equals the confirmed proposal (otherwise `proposal_mismatch`) and re-checks all rules.
A changed situation returns `proposal_rules_changed`, and an expired proposal returns
`proposal_not_confirmable`; for an expired proposal, the agent re-checks the draft and shows a new summary.

## Identity and role model (local development)

There is no real login in the supplied material, so identity is an explicit, documented
**development mechanism** and not production authentication:

- The MCP server process is started with a principal: `role=employee` and
  `employee_id=DEMO_EMPLOYEE_ID` (default for the CLI), or `role=hr` with an HR actor ID for HR use.
- Tools never take the caller's identity from tool arguments. `create_leave_request` has no
  `employee_id` argument at all; read tools accept an optional `employee_id` that must equal the
  caller unless the role is HR.

| Capability | employee assistant | HR |
|---|---|---|
| list_leave_types | ✔ | ✔ |
| get_leave_balance | own only | any employee |
| list_leave_requests | own only | any employee, filters |
| create_leave_request (via confirmed proposal) | own; ANNUAL/SICK/UNPAID; all rules | — (HR records requests in its own system) |
| approve / reject / cancel | ✘ (policy 4.8, 12.3) | ✔ |

The HR actor must be an active employee of the HR department (`HRS`), which is the department the
policy assigns HR duties to (13.3). Managers do not get extra rights through the assistant:
policy 5.2 says the assistant does not show team members' balances even to managers.

## Date handling

- `APP_TODAY` is required; all rules use `Clock.today()`. `created_at` of new requests is
  `APP_TODAY` + current wall time in `APP_TIMEZONE`, so it agrees with the submission date used
  for notice periods.
- CSV dates are `M/D/YYYY` (the data dictionary says ISO); the importer accepts both. Naive
  `created_at` values from the CSV are interpreted in `APP_TIMEZONE` (Asia/Tbilisi).

## Testing strategy

- Pure unit tests for calculators and validators (no DB).
- DB tests run in a disposable schema `test_<random>` on `TEST_DATABASE_URL` (may be the
  Supabase project) or, when that is unset, on an embedded PostgreSQL + pgvector (`pgserver`).
  Application data is never touched.
- LLM and embedding calls are mocked; the core suite needs no API key.
