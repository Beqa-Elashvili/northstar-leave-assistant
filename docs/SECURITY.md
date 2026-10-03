# Security review

This document reviews the system against assignment section 51 and the credential rules. For each
requirement it gives how it is implemented and which test proves it. Residual risks are listed at
the end.

## Section 51 checklist

| Requirement | Implementation | Evidence |
|---|---|---|
| Environment-based secrets | All secrets come from `.env` / environment via `pydantic-settings`. They are stored as `SecretStr`, so `repr`, logs and validation errors never show them. | `test_config.py::test_secrets_never_appear_in_repr` |
| `.gitignore` | `.env`, `.env.*` (except `.env.example`), virtualenvs, caches, `logs/` | `test_repository_hygiene.py::test_gitignore_excludes_env_and_caches` |
| No credentials in source | `.env.example` has empty values only. A test scans every tracked file for key/URL patterns. The full git history was also scanned during this review: 0 matches for the real keys, the DB password, and Google/JWT/`sb_secret_` patterns. | `test_repository_hygiene.py::test_env_example_has_no_secret_values`, `::test_no_hardcoded_credentials_in_source` |
| No credentials in README | The README uses placeholders only (see `.env.example`). | covered by the same repository scan |
| Parameterized DB access | Values always go through the SQLAlchemy ORM/Core or bound parameters (`:name`). The only interpolated SQL is the schema name from configuration, which must match `^[a-z_][a-z0-9_]{0,62}$`. It is validated in `Settings`, `search_path_for` and `apply_migrations` before any SQL is built. | `test_security.py::test_schema_names_cannot_inject_sql`, `::test_migrations_refuse_invalid_schema_before_any_sql` |
| Validated tool arguments | Each MCP tool has a typed pydantic schema: an enum of the 6 leave types, ISO dates, UUIDs, the `E\d{4}` pattern, numeric ranges and `max_length` on comments. Invalid input is rejected before any service code runs. Extra arguments (e.g. an injected `employee_id`) are ignored. | `test_security.py::test_invalid_tool_arguments_are_rejected`, `test_error_handling.py`, `test_mcp_server.py::test_injected_employee_id_is_ignored` |
| Server-side authorization | The MCP server fixes the caller (`Principal`) when it starts. Every tool call re-verifies the caller, and `LeaveService` enforces own-data-only for employees. Approve, reject and cancel require an active HR employee (policies 4.8 and 12.3). Nothing depends on the prompt. | `test_mcp_server.py` (scenario 8, approve/reject/cancel denied, other employee's proposal), `test_scenarios.py` |
| No arbitrary SQL from the LLM | The LLM only returns a fixed pydantic schema: intent enum, leave-type enum, dates, reason and search phrasings. It has no SQL, database or tool-name channel. Dates are re-parsed and every rule is re-checked in code. | `agent/intents.py`, `test_agent.py::test_invalid_date_from_llm_is_not_trusted` |
| No arbitrary tool invocation | The agent maps intents to a fixed allow-list of 7 employee tools (`EMPLOYEE_ASSISTANT_TOOLS`), and the LLM never chooses a tool name. HR tools cannot be sent from the agent and are also denied by the server. | `test_agent.py::test_tool_allow_list_excludes_hr_actions`, `test_error_handling.py::test_unsupported_operation_is_refused_by_the_agent_and_the_server` |
| Safe error messages | Domain errors are returned as Georgian messages with a code and policy article. Database errors become a generic Georgian message. Unexpected exceptions reach the MCP client only as "Error executing tool X". The CLI shows no tracebacks, URLs or keys; diagnostics go to `logs/` (git-ignored), which contains no secrets or user messages. | `test_error_handling.py`, `test_cli.py` (unreachable DB / missing key / unknown employee) |

## Supabase credentials

- **Backend only.** `DATABASE_URL` is used only by backend processes: the MCP server, the scripts and the
  RAG store. The CLI process never connects to the leave tables; it reaches them only through MCP
  tools.
- **Service-role key.** `SUPABASE_SERVICE_ROLE_KEY` is used in exactly one place,
  `scripts/check_setup.py`. There it is sent as a request header to the project's REST API to verify
  setup. It is never printed, logged, put in a URL, or passed to the CLI or the LLM.
  - **Evidence:** `test_security.py::test_rest_check_sends_key_only_as_headers`,
    `::test_rest_check_new_secret_key_and_failures_do_not_leak` and
    `::test_check_setup_script_reports_without_secrets`.
- **Public API roles.** Supabase exposes the `public` schema to the `anon` and `authenticated` roles. Two
  layers keep HR data off that path:
  1. RLS is enabled on every table, with no policies (`001_initial_schema.sql`);
  2. `002_revoke_public_api_roles.sql` revokes all table and sequence privileges from those roles,
     including default privileges for future tables.

  Verified on the real project: 0 privileges remain.
  - **Evidence:** `test_security.py::test_public_api_roles_have_no_table_privileges`, which ran on both
    PostgreSQL and Supabase.

## Other measures

- **Proposals and idempotency.** A request is created only from a stored proposal of the same
  employee. The data must equal what the employee confirmed. The proposal row is locked
  (`SELECT … FOR UPDATE`), and `UNIQUE(proposal_id)` blocks duplicates even under concurrency.
- **Personal data minimisation.** The unpaid-leave reason is limited to 500 characters and rejected
  if it contains health details (policy 7.2 / 12.4). The bereavement, study and parental flows never
  ask for personal details.
- **Logs.** Logs record error classes and stack traces of our own code paths. They do not record
  user messages, prompts, LLM output or keys. The server's stderr goes to `logs/mcp-server.log`.

## Residual risks and recommendations (not in scope of the assignment)

1. **Development identity.** `DEMO_EMPLOYEE_ID` / `--employee-id` is a local development mechanism
   (spec 21), not authentication: anyone running the CLI can choose an employee ID. Production would
   derive the principal from an authenticated session (e.g. Supabase Auth JWT) on the MCP server side.
2. **Database role.** The backend connects with the project's `postgres` role. That role owns the
   tables and bypasses RLS. Production should use a dedicated least-privilege role: `SELECT` on HR
   tables and `INSERT` on `leave_requests`/`leave_proposals` only.
3. **Local secrets.** `.env` is a plaintext file on the developer machine. Use a secret manager in
   deployed environments, and rotate keys if a machine is shared.
4. **Third-party processing.** The following are sent to the Gemini API for classification and wording:
   - employee messages and retrieved policy text;
   - a short conversation context: the draft dates and type, and the assistant's previous reply, cut to
     400 characters, which can contain the employee's own balance.

   Other employees' data is never available to the agent. Confirm that the Gemini data-processing terms
   are acceptable before production use.
