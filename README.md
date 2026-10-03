# Northstar Services HR Assistant

An MCP leave-management server, RAG over the company's policy documents, and a Georgian
command-line assistant for the fictional company "Northstar Services" (ნორთსტარ სერვისეზი).

> **მოკლედ ქართულად.** პროექტი შედგება სამი ნაწილისგან:
> - **MCP სერვერი** შვებულების მოქმედებებისთვის. ის Supabase PostgreSQL ბაზაზე მუშაობს და პოლიტიკის ყველა წესს კოდში ამოწმებს.
> - **RAG** 7 ქართულ დოკუმენტზე (DOCX/PDF). პასუხს წყარო ახლავს, მოძველებული დოკუმენტები კი აღნიშნულია.
> - **ქართული CLI ასისტენტი** (Gemini). ის განასხვავებს პოლიტიკის კითხვას, ბალანსის ნახვას და შვებულების მოთხოვნას, ხოლო მოთხოვნას მხოლოდ თანამშრომლის მკაფიო დადასტურების შემდეგ ქმნის.
>
> გასაშვებად საჭიროა **Supabase-ის პროექტი** (PostgreSQL + pgvector) და **Google Gemini API-ის გასაღები** (Google AI Studio). ნაბიჯები მოცემულია ქვემოთ, [Installation](#installation)-დან.

## Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Stack](#stack)
- [Requirements](#requirements)
- [Installation](#installation)
- [Environment variables](#environment-variables)
- [Database setup](#database-setup)
- [CSV import](#csv-import)
- [RAG ingestion](#rag-ingestion)
- [MCP server](#mcp-server)
- [CLI](#cli)
- [Tests](#tests)
- [Example conversation](#example-conversation)
- [MCP tools](#mcp-tools)
- [Authorization](#authorization)
- [RAG](#rag)
- [Policy precedence](#policy-precedence)
- [Assumptions](#assumptions)
- [Limitations](#limitations)
- [Further documentation](#further-documentation)

## Overview

**Task 1 — MCP server and database.** The MCP server is the only way to read or change leave data.
It is used by two kinds of client:
- the employee assistant, which can act only on its own employee;
- HR, which can approve, reject or cancel requests.

The server stores employees, leave types, entitlements, requests and public holidays in Supabase
PostgreSQL. Every policy rule is enforced in deterministic code: notice periods, the 15-day
maximum, probation, audit-department blackout periods, balances, overlaps and the sick and unpaid
leave rules.

**Task 2 — Georgian CLI assistant.** The assistant talks to the employee in Georgian and recognises
what they want:
- a **policy question** is answered by RAG from the 7 supplied documents, with sources. If the
  documents do not contain the answer, it says so;
- a **balance question** is answered through the MCP tool `get_leave_balance`;
- a **leave request**: the assistant determines the leave type, asks for missing details, shows a
  summary and creates the request through MCP only after an explicit "დიახ". Confirming twice never
  creates a duplicate.

Bereavement, study and parental leave are explained and redirected to the HR portal or HR, as the
policy requires. Cancelling, changing or approving existing requests is refused.

The supplied data describes the company on **2026-10-19**. All date rules use this business date
(`APP_TODAY`) instead of the machine clock.

## Architecture

```text
CLI → Agent → RAG / MCP → Database
```

```text
┌──────────────────────┐
│ Georgian CLI (rich)  │  northstar.cli         labels: თქვენ / ასისტენტი / წყარო / ხელსაწყო
└──────────┬───────────┘
┌──────────▼───────────┐
│ Agent (LLM + state)  │  northstar.agent       LLMProvider → GeminiProvider (intent + extraction only)
└─────┬───────────┬────┘
      │ policy    │ balance / requests / create
┌─────▼─────┐ ┌───▼────────────┐
│    RAG    │ │   MCP client   │  stdio session to a server subprocess
│ (pgvector)│ └───┬────────────┘
└─────┬─────┘ ┌───▼────────────┐
      │       │   MCP server   │  northstar.mcp         identity fixed per session, typed tools
      │       └───┬────────────┘
      │       ┌───▼────────────┐
      │       │ Business rules │  northstar.services / northstar.policies (deterministic)
      │       └───┬────────────┘
      │       ┌───▼────────────┐
      │       │  Repositories  │  northstar.database (SQLAlchemy)
      ▼       └───┬────────────┘
┌─────────────────▼──────────────────────────┐
│ Supabase PostgreSQL + pgvector             │
└────────────────────────────────────────────┘
```

| Layer | Responsibility | Never trusted with |
|---|---|---|
| LLM | Intent, leave type and date extraction, Georgian wording of policy answers | Permissions, balances, date arithmetic, rule checks, tool names, SQL |
| RAG | Company policy knowledge with citations | Operational employee data |
| MCP server | The only path to leave data; fixes the caller's identity and role | — |
| Business rules | Day counting, all leave rules, authorization | — |
| Database | Operational truth; constraints are the last line of defence | — |

## Stack

| Concern | Technology |
|---|---|
| Language | Python 3.11+ |
| Database | Supabase PostgreSQL 17 + **pgvector** (any PostgreSQL ≥ 15 with pgvector also works) |
| DB access | SQLAlchemy 2.0, psycopg2; versioned SQL migrations run from code |
| MCP | Official MCP Python SDK (`mcp` 2.x, `MCPServer`), stdio transport |
| LLM | Google Gemini via `google-genai` (default `gemini-3.5-flash-lite`, structured JSON output, streaming) |
| Embeddings | `gemini-embedding-001`, 768 dimensions |
| Documents | `python-docx` (DOCX), `PyMuPDF` (PDF text, fonts, tables) |
| CLI | `rich` |
| Config | `pydantic-settings` + `.env` |
| Tests | `pytest`; disposable PostgreSQL schema (embedded `pgserver` or `TEST_DATABASE_URL`) |

## Requirements

- Python **3.11** or newer
- A **Supabase** project (free tier is enough). Alternatively, any PostgreSQL 15+ with the `vector` extension.
- A **Google Gemini API key** from [Google AI Studio](https://aistudio.google.com/apikey).
  - It is used for the chat model and for embeddings.
  - Billing or prepaid credits may be required, depending on the account.
  - The default model is the low-cost `gemini-3.5-flash-lite`; a full test session costs well under 1 USD.
- No API key is needed for the automated test suite.

## Installation

```bash
git clone <repository-url> northstar-leave-assistant
cd northstar-leave-assistant

python -m venv .venv
# Windows:        .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate

pip install -e ".[dev]"
```

Then create the configuration file and fill it in as described under [Environment variables](#environment-variables):

```bash
cp .env.example .env        # Windows (cmd): copy .env.example .env
```

### Troubleshooting

- **Windows: `DLL load failed … The filename or extension is too long`.** The project path is too long
  for the Windows 260-character path limit inside `.venv`. Clone it into a short path (for example
  `C:\src
orthstar`), or enable Windows long paths.
- **`invalid value in .env for X`.** The named variable has an invalid value; compare it with
  `.env.example`.
- **`python -m scripts.check_setup`** shows which setup step is missing.

## Environment variables

All configuration is read from `.env` in the project root, or from the environment. `.env` is
git-ignored; never commit it.

| Variable | Required | Meaning |
|---|---|---|
| `APP_TODAY` | yes | Business date for all leave rules. Keep `2026-10-19`, the state of the supplied data. |
| `APP_TIMEZONE` | no | Default `Asia/Tbilisi`. |
| `DATABASE_URL` | yes | PostgreSQL connection string. For Supabase use **Connect → Session pooler** (IPv4): `postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`. Backend only. |
| `DB_SCHEMA` | no | Schema for the application tables, default `public`. |
| `SUPABASE_URL` | no | `https://<ref>.supabase.co`. Used only by `scripts/check_setup.py`. |
| `SUPABASE_SERVICE_ROLE_KEY` | no | Supabase **service_role** (or `sb_secret_…`) key. Backend only: used solely by `scripts/check_setup.py` as a request header. It is never printed or given to the CLI. |
| `GEMINI_API_KEY` | yes (CLI, ingestion) | Google AI Studio API key. |
| `GEMINI_MODEL` | no | Chat model, default `gemini-3.5-flash-lite`. |
| `GEMINI_FALLBACK_MODELS` | no | Comma-separated models tried if the primary is overloaded or unavailable. |
| `GEMINI_THINKING_LEVEL` | no | `LOW` (default, fast) / `MEDIUM` / `HIGH`. |
| `EMBEDDING_PROVIDER` | no | `gemini` (default) or `local`. `local` is an offline hashing embedder used by the tests; it gives lower retrieval quality. |
| `EMBEDDING_MODEL` | no | Default `gemini-embedding-001`. |
| `EMBEDDING_DIM` | no | Default `768`. Must match the `vector(768)` column. |
| `EMBEDDING_RPM` | no | Embedding requests per minute during ingestion (default 90, under the free-tier limit of 100). |
| `DEMO_EMPLOYEE_ID` | yes | The employee the CLI acts as, default `E1001` (ნინო ბერიძე). This is a **local-development identity mechanism, not production authentication**; see [Authorization](#authorization). |
| `TEST_DATABASE_URL` | no | PostgreSQL used by the tests. Each run creates and drops its own temporary schema. If empty, an embedded PostgreSQL (`pgserver`) is started. |

## Database setup

1. Create a project at [supabase.com](https://supabase.com). No dashboard SQL is needed.
2. Copy the **Session pooler** connection string into `DATABASE_URL`. Optionally copy the project URL
   and service-role key into `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`.
3. Create the schema:

```bash
python -m scripts.migrate
```

This applies `migrations/*.sql` in order and records each file's checksum in `schema_migrations`.
Running it again is a no-op. The migrations:
- enable `pgvector` (in the `extensions` schema);
- create the HR tables, the `leave_proposals` table and the `leave_balances` view;
- create the RAG tables (`rag_documents`, `document_chunks` with an HNSW vector index and a GIN
  full-text index);
- enable Row Level Security on every table and revoke all privileges from Supabase's public API
  roles (`anon`, `authenticated`). HR data is reachable only through the backend.

## CSV import

```bash
python -m scripts.seed_database           # idempotent upsert of data/*.csv (also applies migrations)
python -m scripts.seed_database --reset   # reload exactly the supplied snapshot (removes requests created later)
```

The import validates every row and the cross-file references, and writes in one transaction.
Expected counts:

| Table | Rows |
|---|---|
| `employees` | 14 |
| `leave_types` | 6 |
| `leave_entitlements` | 56 |
| `leave_requests` | 27 |
| `public_holidays` | 34 |

New requests continue from ID 28. The CSV files are read as they are and never modified. Dates in
`M/D/YYYY` and ISO formats are both accepted.

## RAG ingestion

```bash
python -m scripts.ingest_documents            # only new or changed documents
python -m scripts.ingest_documents --force    # re-embed everything
```

The 7 documents in `documents/` produce **272 chunks**. With the free-tier rate limit, the first run
takes about 3–4 minutes; later runs skip unchanged files. After changing `EMBEDDING_MODEL`, run it
again: documents embedded with another model are detected and re-embedded.

Then verify the whole setup:

```bash
python -m scripts.check_setup
```

```text
✓ DATABASE_URL
✓ GEMINI_API_KEY
✓ DEMO_EMPLOYEE_ID: E1001
✓ APP_TODAY: 2026-10-19
✓ PostgreSQL connection: PostgreSQL 17.11
✓ pgvector extension: version 0.8.2
✓ Migrations: up to date
✓ Seed data: employees=14, leave_types=6, leave_entitlements=56, leave_requests=27, public_holidays=34
✓ Demo identity: E1001 is an active employee
✓ Policy documents (RAG): ingested with the configured embedding model
✓ Supabase REST API: project reachable, 6 leave types visible to the service role
```

## MCP server

The CLI starts the MCP server automatically as a subprocess over stdio, so you do not need to start
it yourself. To use it from another MCP client:

```bash
northstar-mcp --employee-id E1001          # employee session (default: DEMO_EMPLOYEE_ID)
northstar-mcp --role hr --employee-id E1007   # HR session (E1007 is in the HR department)
# equivalent: python -m northstar.mcp.server [--role employee|hr] [--employee-id EXXXX]
```

Example client configuration (e.g. Claude Desktop / MCP Inspector):

```json
{
  "mcpServers": {
    "northstar-leave": {
      "command": "python",
      "args": ["-m", "northstar.mcp.server", "--employee-id", "E1001"],
      "cwd": "/path/to/northstar-leave-assistant"
    }
  }
}
```

The server refuses to start (exit code 2) when the identity is unknown, inactive, or not in the HR
department for `--role hr`.

## CLI

```bash
northstar-cli                        # or: python -m northstar.cli
northstar-cli --employee-id E1002    # act as another (demo) employee
northstar-cli --no-stream            # print policy answers only when complete
```

Commands inside the chat:
- `/help` — help;
- `/new` — start a new conversation;
- `/exit` — quit (Ctrl+C also works).

Replies are labelled **თქვენ:** (You), **ასისტენტი:** (Assistant), **წყარო:** (Source) and
**ხელსაწყო:** (Tool, the MCP tool that was called). Technical details never appear on screen; they
go to `logs/northstar-cli.log` and `logs/mcp-server.log`.

## Tests

```bash
python -m pytest
```

The suite has 389 tests. It needs **no Gemini key and no network**:
- the LLM is mocked and the embeddings are local;
- the MCP server, the business rules, the SQL and the RAG retrieval are real.

Database tests run in a temporary schema on `TEST_DATABASE_URL`, or on an embedded PostgreSQL 16 +
pgvector (`pgserver`) when that is empty. Your data is never touched.

| File | Covers |
|---|---|
| `tests/test_scenarios.py` | The 10 required scenarios (assignment section 48), with the assignment's wording |
| `tests/test_error_handling.py` | Every listed error case (section 50) |
| `tests/test_mcp_server.py`, `tests/test_mcp_stdio.py` | Every MCP tool, authorization, confirmation and idempotency (including concurrent confirmations) |
| `tests/test_rules.py`, `tests/test_day_calculator.py`, `tests/test_balance.py` | Policy rules, day counting and balances |
| `tests/test_retrieval.py`, `tests/test_extraction.py`, `tests/test_chunking.py` | RAG, precedence and citations |
| `tests/test_security.py`, `tests/test_repository_hygiene.py` | Security checks, no secrets in the repository |

The full map is in [docs/TESTING.md](docs/TESTING.md). An optional live retrieval check against the
ingested documents (needs the Gemini key) scores 21/21:

```bash
python -m scripts.evaluate_retrieval
```

## Example conversation

These are real outputs (E1001, business date 2026-10-19), slightly shortened.

```text
┌─ Northstar Services HR Assistant ─┐
│ თანამშრომელი: E1001               │
│ სახელი: ნინო ბერიძე               │
│ თარიღი: 2026-10-19                │
└───────────────────────────────────┘

თქვენ: რამდენი დღით ადრე უნდა მოვითხოვო ყოველწლიური შვებულება?
ასისტენტი:
ყოველწლიური შვებულების მოთხოვნა წინასწარ უნდა წარდგეს შვებულების ხანგრძლივობის მიხედვით [1]:
• 1-დან 5 სამუშაო დღემდე შვებულებისთვის — მინიმუმ 5 სამუშაო დღით ადრე [1];
• 6-დან 15 სამუშაო დღემდე შვებულებისთვის — მინიმუმ 15 სამუშაო დღით ადრე [1].
წყარო: [1] შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლი 4.4 („მოთხოვნის წარდგენა და წინასწარი შეტყობინება“)

თქვენ: რამდენი ANNUAL დღე დამრჩა?
ხელსაწყო: get_leave_balance (ბალანსის მიღება)
ასისტენტი:
• ყოველწლიური ანაზღაურებადი შვებულება
    კუთვნილი: 25 + გადმოტანილი: 3
    დამტკიცებული: 15  |  განხილვის პროცესში: 3
    ხელმისაწვდომი: 10 სამუშაო დღე

თქვენ: 26 ოქტომბრიდან 30 ოქტომბრამდე შვებულება მინდა.
ხელსაწყო: propose_leave_request (მოთხოვნის წესებით შემოწმება)
ასისტენტი:
მოთხოვნის შექმნა ასისტენტით შეუძლებელია:
• 5 სამუშაო დღის ყოველწლიური შვებულებისთვის საჭიროა სულ მცირე 5 სამუშაო დღით ადრე წარდგენა …
  ყველაზე ადრეული შესაძლო დაწყების თარიღია 2026-10-27. (შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლი 4.4)
შეგიძლიათ მიუთითოთ სხვა თარიღები.

თქვენ: 27-დან 30-მდე იყოს
ხელსაწყო: propose_leave_request (მოთხოვნის წესებით შემოწმება)
ასისტენტი:
გთხოვთ დაადასტუროთ:

ტიპი: ყოველწლიური ანაზღაურებადი შვებულება
პერიოდი: 2026-10-27 – 2026-10-30
დღეების რაოდენობა: 4 სამუშაო დღე
ბალანსი: 10 → 6

შექმნილი მოთხოვნა მიიღებს სტატუსს „განხილვის პროცესში“ — ეს დამტკიცებას არ ნიშნავს (მუხლი 12.2).

შევქმნა მოთხოვნა? (დიახ/არა)

თქვენ: დიახ
ხელსაწყო: create_leave_request (მოთხოვნის შექმნა)
ასისტენტი:
მოთხოვნა შეიქმნა. მოთხოვნის ნომერი: #28.
ყოველწლიური ანაზღაურებადი შვებულება, 2026-10-27 – 2026-10-30 (4 დღე)
სტატუსი: განხილვის პროცესში — ეს დამტკიცებას არ ნიშნავს (მუხლი 12.2).
გადაწყვეტილებას უშუალო ხელმძღვანელი იღებს. დამტკიცებამდე მგზავრობისა და სხვა ხარჯების გადახდისგან თავი შეიკავეთ (მუხლი 4.4).

თქვენ: დიახ
ხელსაწყო: create_leave_request (მოთხოვნის შექმნა)
ასისტენტი:
ეს მოთხოვნა უკვე შექმნილია — ხელახლა არ შემიქმნია. მოთხოვნის ნომერი: #28. …

თქვენ: ბებიაჩემი გარდაიცვალა და შვებულება მინდა.
ასისტენტი:
გულწრფელად გიზიარებთ მწუხარებას.

… ბებიის ან ბაბუის გარდაცვალებისას — 1 სამუშაო დღე [3]. გლოვის შვებულება გამოიყენება გარდაცვალების
დღიდან 30 კალენდარული დღის განმავლობაში [1]. …

ამ მოთხოვნას ასისტენტი ვერ ქმნის. გლოვის შვებულების მოთხოვნა წარადგინეთ HR პორტალით ან ადამიანური
რესურსების სამსახურის მეშვეობით, არაუგვიანეს 2 სამუშაო დღისა შვებულების პირველი დღიდან
(შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლები 8.3 და 12.3).
წყარო: [3] შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლი 8.2 („სხვა ნათესავები“)
წყარო: [1] შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლი 8.3 („გამოყენების ვადა და წარდგენა“)

თქვენ: მაჩვენე E1002-ის ბალანსი.
ხელსაწყო: get_leave_balance (ბალანსის მიღება)
ასისტენტი:
სხვა თანამშრომლის მონაცემების ნახვა შეუძლებელია: ასისტენტი მხოლოდ თქვენს საკუთარ ბალანსსა და მოთხოვნებს
აჩვენებს … (შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლი 5.2, 12.3)

თქვენ: ჩემი მოთხოვნა გააუქმე.
ასისტენტი:
ასისტენტს არ შეუძლია უკვე წარდგენილი მოთხოვნის გაუქმება, შეცვლა, დამტკიცება ან უარყოფა.
• განხილვის პროცესში მყოფი მოთხოვნის გაუქმება ნებისმიერ დროს შეგიძლიათ HR პორტალით. …
წყარო: შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლი 4.8 („გაუქმება და ცვლილება“)
წყარო: შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლი 12.3 („რისი გაკეთება არ შეუძლია HR ასისტენტს“)

თქვენ: რა არის კომპანიის პოლიტიკა შინაური ცხოველების ოფისში მოყვანაზე?
ასისტენტი:
მოწოდებულ კომპანიის დოკუმენტებში ამ კითხვაზე საკმარისი ინფორმაცია ვერ მოიძებნა.
```

Other flows:
- **Unpaid leave:** the assistant asks for a short reason (without health details), checks the
  10-working-day notice and the 30-day limit, and stores the reason in `comment`.
- **Sick leave:** a request over the remaining paid days is not created. The assistant explains that
  the illness can still be recorded through HR. Future dates need confirmation that the period is
  known in advance.
- **Vague request:** "შვებულება მინდა" gets the question "რა ტიპის შვებულება გსურთ?", and details
  already given are never asked again.

To reset the demo data after trying requests: `python -m scripts.seed_database --reset`.

## MCP tools

Every tool has a typed input and output schema (pydantic → JSON Schema). Errors are structured:
`{code, message (Georgian), article, details}`.

| Tool | Inputs | Returns | Who |
|---|---|---|---|
| `list_leave_types` | — | All 6 types: day unit, annual limit, `self_service`, `assistant_supported` | everyone |
| `get_leave_balance` | `year?` (default current), `leave_type?`, `employee_id?` | Per type: entitled, carried over, approved, pending, available days, unit (SICK: available paid days, floored at 0, plus the raw value) | employee: own only · HR: anyone |
| `list_leave_requests` | `employee_id?`, `status[]?`, `leave_type?`, `date_from?`, `date_to?`, `limit` | Requests overlapping the date range | employee: own only · HR: anyone |
| `create_leave_request` | `leave_type`, `start_date`, `end_date`, `comment?`, `proposal_id` | The created request (`pending`, `created_via=assistant`), or the existing one on a repeat (`already_existed=true`) | employee, for **themselves only** (no `employee_id` input) |
| `approve_leave_request` | `request_id`, `comment?` | Decision record | **HR only** |
| `reject_leave_request` | `request_id`, `reason` (stored) | Decision record | **HR only** |
| `cancel_leave_request` | `request_id`, `reason?` | Decision record | **HR only** (assistant may not cancel, policy 4.8 / 12.3) |
| `propose_leave_request` | `conversation_id`, `leave_type`, `start_date`, `end_date`, `comment?`, `sick_period_known_in_advance?` | Validation result: `awaiting_confirmation` (+ `proposal_id`, days, balance before/after), `needs_input` or `not_allowed` (Georgian reasons, articles, redirect channel). Creates nothing. | employee |
| `decline_leave_proposal` | `proposal_id` | Proposal status | employee |
| `get_my_profile` | — | The session's own employee (name for the CLI header) | everyone |

**Creation flow (confirmation and idempotency).**
1. `propose_leave_request` checks every rule and stores a proposal with a 30-minute lifetime.
2. The CLI shows the summary and waits for an explicit "დიახ".
3. `create_leave_request` must carry the same data plus the `proposal_id`. Otherwise it fails with
   `proposal_mismatch`.
4. All rules are re-checked at creation time.
5. Confirming the same proposal again returns the original request ID. Even under concurrent
   confirmations exactly one row is inserted: the proposal row is locked (`SELECT … FOR UPDATE`) and
   `leave_requests.proposal_id` is `UNIQUE`.

## Authorization

Authorization is enforced **in the MCP server and service layer**, never in prompts.

- **Identity.** Each MCP server session is started for a fixed principal: the role (`employee` or
  `hr`) and the employee ID. Tools never take the caller's identity from arguments.
- **Employee assistant** — own data only:
  - profile, leave types, own balance, own requests;
  - proposing and creating own ANNUAL, SICK and UNPAID requests in the current year, after every
    rule has passed.
  - It **cannot** approve, reject, cancel or modify requests, see another employee's data (managers
    included), act for another employee, change entitlements or employee data, or create
    BEREAVEMENT, STUDY or PARENTAL leave (policy 4.8, 5.2, 12.3).
  - The agent also uses a fixed allow-list of 7 tools, and the LLM never chooses tool names.
- **HR** (`--role hr`; the principal must be an active employee of the HR department, `HRS`):
  - reads balances and requests of any employee;
  - approves or rejects pending requests (a rejection reason is required and stored);
  - cancels pending or approved requests.
- **Development identity.** `DEMO_EMPLOYEE_ID` / `--employee-id` is an explicit **local-development
  identity mechanism**, as allowed by the assignment. It is **not production authentication**: a
  real deployment would derive the principal from an authenticated session (for example a Supabase
  Auth JWT validated by the server).

## RAG

1. **Extraction** keeps the document structure.
   - **DOCX** (`python-docx`): heading styles give the article numbers. List items and tables
     (rows with cell labels) are kept in order.
   - **PDF** (`PyMuPDF`, text-based, no OCR): headings are detected from font size and weight.
     Running headers, footers and page numbers are removed, wrapped lines are re-joined, and tables
     are extracted as tables, including one that continues on the next page. Page numbers are kept
     for citations.
2. **Chunking** follows the articles: one chunk per article or sub-article (e.g. 4.4), never
   arbitrary character cuts.
   - Every chunk starts with a context line (document, article, title).
   - Each chunk carries metadata: document, title, version, effective date, domain, authority,
     section, section title, page.
3. **Embeddings** are produced by `gemini-embedding-001` (768 dimensions, normalised) and stored in
   `document_chunks.embedding` (pgvector, HNSW cosine index). Ingestion is incremental, based on
   content hashes, and rate-limited.
4. **Retrieval** is hybrid:
   - candidate passages come from vector similarity, Georgian word-prefix full-text search
     (rare terms only) and exact article references ("მუხლი 4.4");
   - they are fused with reciprocal-rank fusion, with a boost for the policy that is authoritative
     for the question's topic;
   - the LLM's rephrasings of the question are fused in as additional queries.
5. **Answering.** The LLM receives only the numbered passages and must cite them as [1], [2].
   - The **"წყარო:" lines are built from stored metadata**, never written by the LLM. Example:
     `შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლი 4.4 („…“)`; PDF sources also show
     `გვერდი N`.
   - If no passage is similar enough, the assistant answers exactly "მოწოდებულ კომპანიის
     დოკუმენტებში ამ კითხვაზე საკმარისი ინფორმაცია ვერ მოიძებნა." without calling the LLM.

## Policy precedence

The precedence rules come from the documents themselves:
- Leave Policy 1.4: it prevails over the Handbook and the FAQ;
- Handbook 1.2: specialised policies prevail over it;
- Remote Work Policy 1.3: it replaces Handbook §6 and the FAQ overview;
- the FAQ header: reference-only, may be outdated.

| Topic | Authoritative | Outdated statements it overrides |
|---|---|---|
| Leave | Leave and Absence Policy v4.0 | Handbook v3.1 §5 and FAQ 2025: 5-day notice for all requests, 10-day carry-over until 30 June, medical certificate after more than 3 days, "24 days" |
| Remote work | Remote and Hybrid Work Policy v2.0 | Handbook §6 / FAQ: "3 days per week" (current rule: 2) |
| Information security and AI tools | Information Security Policy v3.2 | — |
| Learning | Learning and Development Policy v1.2 (study-leave days: Leave Policy art. 9) | — |
| Travel, expenses, home equipment | Travel and Expense Policy v2.3 | — |

How this is applied:
- Each chunk is tagged with its document's domain and authority (`authoritative_policy` >
  `general_handbook` > `reference_faq`).
- Retrieval always includes the best passage of the authoritative policy for the question's topic.
- A Handbook or FAQ passage on the same topic is marked **superseded**, and the LLM is told to
  follow the current policy and to say that the FAQ or Handbook statement is outdated.
- Business rules in code implement only the authoritative Leave Policy v4.0.

## Assumptions

Where the supplied files leave something open, the implementation chooses the conservative option:

- **Dates.** The data dictionary says ISO dates, but the CSVs use `M/D/YYYY`. Both are parsed, and
  the files are left unchanged.
- **Probation.** `probation_end_date` is the last day of probation (inclusive). The HR exception for
  probation (4.3) and the manager's late-notice exception (4.4) are not available through the
  assistant; it redirects instead.
- **Current year only.** The assistant creates requests only for the current leave year (12.2).
  Next-year dates are redirected to the HR portal, which opens on 1 December.
- **Sick leave in the future.** Future sick-leave dates are accepted only after the employee confirms
  the period is known in advance (6.2 / 12.4).
- **Unpaid-leave reason.** A reason that looks like a health detail is refused (7.2: no health
  information).
- **Proposal lifetime.** A shown summary is valid for 30 minutes (our choice). After that the rules
  are re-checked and a new summary is shown.
- **HR role.** It is granted to active employees of the `HRS` department. The documents describe HR
  decisions but no technical role model.
- **Study leave.** No approved learning plans exist in the data, so the assistant never assumes one.
  HR checks the plan.

## Limitations

- **Identity.** It is a development mechanism (`DEMO_EMPLOYEE_ID`), not real authentication.
- **Database role.** The backend uses the project's `postgres` role. A least-privilege role is
  recommended for production; see [docs/SECURITY.md](docs/SECURITY.md).
- **Language understanding.** It depends on the LLM. Wrong extractions are caught by deterministic
  validation and the confirmation step, but an unusual phrasing may need rewording.
- **Data sent to Gemini.** Employee messages, retrieved policy text and a short conversation context
  are sent to the Gemini API.
- **Tests and embeddings.** The offline `local` embedding provider used by the tests is less
  accurate than Gemini embeddings. Retrieval quality was measured with Gemini:
  `scripts/evaluate_retrieval.py`, 21/21.
- **Not implemented.** No HR user interface is included: HR actions are available as MCP tools only,
  as the assignment requires. Voice and other non-text interfaces are intentionally not implemented.

## Further documentation

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — requirement matrix: requirement → file → test → status
- [docs/SPECIFICATION.md](docs/SPECIFICATION.md) — rules extracted from the supplied files
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — layers, schema, role model
- [docs/SECURITY.md](docs/SECURITY.md) — security review
- [docs/TESTING.md](docs/TESTING.md) — test map
