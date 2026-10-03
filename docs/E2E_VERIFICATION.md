# End-to-end verification (assignment section 57)

The project was verified as a new evaluator would run it, following only the README:
- a fresh `git clone` into a new folder;
- a new virtualenv;
- a new, empty database schema (`DB_SCHEMA=e2e_clean`) on the real Supabase project;
- the real Gemini API (`gemini-3.5-flash-lite`, `gemini-embedding-001`).

The schema and the clone were deleted afterwards.

## Commands

```bash
git clone <repo> app && cd app
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env                    # filled in: DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_API_KEY, DB_SCHEMA
python -m scripts.migrate                 # applied 001, 002; second run: "already up to date"
python -m scripts.seed_database           # 14 / 6 / 56 / 27 / 34 rows
python -m scripts.ingest_documents        # 7 documents, 272 chunks, 3m13s; second run: 7 unchanged
python -m scripts.check_setup             # 11/11 checks passed
northstar-mcp ...                         # standalone MCP server, used by an external MCP client (below)
northstar-cli                             # two scripted sessions (below)
python -m pytest                          # 389 passed
python -m scripts.evaluate_retrieval      # 21/21
```

## Results

| # | Step (section 57) | Result |
|---|---|---|
| 1 | Install dependencies | ✅ (fresh install resolved SQLAlchemy 2.1.3; everything passed on it) |
| 2 | Configure `.env` | ✅ |
| 3 | Create database | ✅ migrations 001 + 002; re-run is a no-op |
| 4 | Seed CSV data | ✅ 14 employees, 6 types, 56 entitlements, 27 requests, 34 holidays |
| 5 | Ingest documents | ✅ 272 chunks; re-run skips unchanged documents |
| 6 | Start MCP server | ✅ 10 tools. E1001 balance 25+3−15−3 = 10. An employee calling `approve_leave_request` gets `permission_denied`. The HR session (E1007) reads E1002 and approves/cancels #5; rejecting an approved request gets `invalid_status_transition`. `--role hr` for a non-HR employee is refused. |
| 7 | Start CLI | ✅ header "Northstar Services HR Assistant / E1001 / ნინო ბერიძე" |
| 8 | Policy question | ✅ notice rules with source "მუხლი 4.4". Remote work: "2 days/week" from Remote Policy v2.0 (not the outdated 3). Unknown question: "ინფორმაცია ვერ მოიძებნა". |
| 9 | Balance | ✅ `get_leave_balance`: entitled 25 + carried over 3, approved 15, pending 3, available 10 |
| 10 | Annual request | ✅ 26–30 Oct rejected (4.4, earliest 27 Oct). 27–30 Oct: summary (4 days, 10 → 6), then "დიახ" creates #28 (pending, assistant). |
| 11 | Sick request | ✅ 19–20 Oct, then "კი" creates #29. 19–29 Oct (9 days > 8 paid) is not created and is redirected to HR (6.4), with "recording is still possible". |
| 12 | Unpaid request | ✅ reason asked first, then dates; 3–6 Nov = 4 calendar days, reason shown; "დიახ" creates #30 with the reason in `comment` |
| 13 | Unsupported leave type | ✅ bereavement / study / parental: rules with sources plus a redirect; no tool called |
| 14 | Authorization | ✅ "მაჩვენე E1002-ის ბალანსი" refused by the server; "ჩემი მოთხოვნა გააუქმე" refused with 4.8 / 12.3, no tool called |
| 15 | Duplicate confirmation | ✅ second "დიახ" returns "უკვე შექმნილია … #28"; no duplicate |
| 16 | Automated tests | ✅ 389 passed in the clean virtualenv |

## Issues found and fixed during this run

1. **Missing `tzdata` dependency.** On Windows, `zoneinfo` has no time-zone database, so
   `APP_TIMEZONE=Asia/Tbilisi` failed in a fresh virtualenv. The development environment had
   `tzdata` only indirectly. Fixed by adding `tzdata` to the dependencies; a test was added.
2. **Misleading script errors.** A `.env` validation error was reported as "check DATABASE_URL". The
   scripts now name the invalid variable, never its value; a test was added.
3. **Wrong post-creation note.** After creating sick or unpaid leave, the assistant said "the manager
   decides, avoid travel costs (4.4)", which is the annual-leave rule. The note is now type-specific:
   ANNUAL 4.4; UNPAID manager then HR (7.3); SICK over 2 days medical certificate (6.3). A test was
   added.
4. **Windows long-path limit.** A very long project path breaks DLL loading inside `.venv`. This is an
   environment limit, not a project bug; documented in the README under Troubleshooting.
