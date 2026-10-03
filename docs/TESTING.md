# Tests

```bash
python -m pytest            # whole suite; no Gemini key and no network needed
```

- **Database:** tests run against `TEST_DATABASE_URL` if it is set, for example Supabase. Otherwise they use an
  embedded PostgreSQL 16 + pgvector (`pgserver`, a dev dependency).
  - Every run creates a throw-away schema `test_<random>`, applies the migrations, seeds the CSVs and
    drops the schema at the end. Application data is never touched.
- **LLM:** mocked with a scripted `FakeLLM` that only classifies and extracts.
- **Embeddings:** an offline hashing provider (`EMBEDDING_PROVIDER=local`).
- **Everything else is real:** the MCP server runs in memory, and also as a stdio subprocess. Business
  rules, SQL and RAG retrieval run for real.

## Assignment section 47 → tests

| Requirement | Tests |
|---|---|
| Database: employee / leave type / entitlement / request / holiday import | `test_seed_and_repositories.py` (`test_*_import`), `test_csv_loader.py`, `test_schema.py` |
| Day calculation: weekdays, weekends, public holidays, calendar days | `test_day_calculator.py` |
| Balance: approved, pending, rejected, cancelled, carry-over, SICK negative | `test_balance.py` |
| Policy: probation, annual notice, 15-day limit, AUD periods, unpaid notice and 30-day limit, supported/unsupported types | `test_rules.py`, `test_leave_rule_service.py` |
| Authorization: own balance, no other balance, no request for others, no approve/reject/cancel, no unsupported types | `test_mcp_server.py`, `test_error_handling.py`, `test_agent.py::test_tool_allow_list_excludes_hr_actions` |
| MCP: every required tool | `test_mcp_server.py`, `test_mcp_stdio.py` (real subprocess) |
| RAG: authoritative policy, outdated FAQ conflict, source metadata, unknown question | `test_retrieval.py`, `test_extraction.py`, `test_chunking.py`, `test_embeddings_and_ingest.py` |
| Agent: policy→RAG, balance→MCP, annual/sick/unpaid→MCP, bereavement/study/parental→redirect, ambiguous→clarification | `test_agent.py`, `test_scenarios.py` |

## Section 48 → `test_scenarios.py`

There is one test per scenario, using the assignment's own wording and asserting each "Expected" bullet:
`test_scenario_1_balance` … `test_scenario_10_duplicate_confirmation`. Scenario 2 has one additional
case for each rule it must check: balance, overlap, probation and AUD restriction.

## Section 50 → `test_error_handling.py`

Covered errors: invalid dates, invalid employee, unknown leave type, unsupported operation, MCP failure,
database failure, RAG failure (not ingested, embedding service, document store) and LLM failure.
Every message is Georgian and free of technical details. Insufficient balance, overlap, notice,
restricted period and probation are covered end to end in `test_scenarios.py`.
CLI start-up failures are tested in `test_cli.py`.

## Optional live checks (need a Gemini key)

```bash
python -m scripts.evaluate_retrieval   # 21 labelled retrieval questions against the ingested documents
```
