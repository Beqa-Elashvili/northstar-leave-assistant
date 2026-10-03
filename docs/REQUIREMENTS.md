# Requirement matrix

Each assignment requirement is listed with its implementation, the evidence that proves it and its
status. Test paths are relative to `tests/`. "Live" means it was verified against the real Supabase
project and Gemini (see the end-to-end section in the README).

## Assignment section 56 checklist

| # | Requirement | Implementation | Evidence (test) | Status |
|---|---|---|---|---|
| 1 | Database exists | `migrations/001_initial_schema.sql`, `002_revoke_public_api_roles.sql`, `database/migrations.py`, `scripts/migrate.py` | `test_schema.py::test_all_tables_and_view_exist`, `::test_rerun_is_noop`; live: `scripts/check_setup.py` | ✅ |
| 2 | Employees stored | `employees` table, `database/csv_loader.py`, `database/seed.py` | `test_seed_and_repositories.py::test_employee_import`, `::test_manager_relationships` | ✅ |
| 3 | Leave types stored | `leave_types` table | `test_seed_and_repositories.py::test_leave_type_import` | ✅ |
| 4 | Leave requests stored (dates, status, created time) | `leave_requests` table | `test_seed_and_repositories.py::test_request_import`, `test_schema.py::test_request_cannot_span_two_years` | ✅ |
| 5 | Annual entitlements stored | `leave_entitlements` table, `leave_balances` view | `test_seed_and_repositories.py::test_entitlement_import`, `test_schema.py::test_balance_view_formula_and_sick_clamp` | ✅ |
| 6 | Public holidays stored | `public_holidays` table | `test_seed_and_repositories.py::test_holiday_import`, `test_day_calculator.py::test_only_supplied_holiday_list_is_used` | ✅ |
| 7 | Create leave request MCP tool | `mcp/server.py::create_leave_request` (+ `propose_leave_request`), `services/leave_requests.py::confirm` | `test_mcp_server.py::test_scenario_2_annual_proposal_then_confirmation`, `::test_create_tool_takes_request_data_and_no_employee`, `test_mcp_stdio.py` | ✅ |
| 8 | List leave requests MCP tool (employee, status, date range) | `list_leave_requests` | `test_mcp_server.py::test_list_own_requests_and_filters`, `::test_hr_lists_everyone`, `::test_list_other_employee_rejected` | ✅ |
| 9 | Get balance MCP tool (year, type; entitled/carried/approved/pending/available) | `get_leave_balance`, `services/balance.py` | `test_mcp_server.py::test_scenario_1_own_annual_balance`, `test_balance.py` | ✅ |
| 10 | Approve MCP tool | `approve_leave_request` | `test_mcp_server.py::test_hr_approve`, `::test_employee_cannot_decide_or_cancel` | ✅ |
| 11 | Reject MCP tool (reason stored) | `reject_leave_request` | `test_mcp_server.py::test_hr_reject_stores_reason`, `::test_hr_reject_requires_reason` | ✅ |
| 12 | Cancel MCP tool | `cancel_leave_request` | `test_mcp_server.py::test_hr_cancel_approved`, `::test_employee_cannot_decide_or_cancel` | ✅ |
| 13 | List leave types MCP tool (6 types + capabilities) | `list_leave_types` | `test_mcp_server.py::test_list_leave_types` | ✅ |
| 14 | Employee authorization | `services/authorization.py`, `services/leave_requests.py`, `agent/tools.py` allow-list | `test_mcp_server.py::test_scenario_8_other_employee_balance_rejected`, `::test_injected_employee_id_is_ignored`, `::test_other_employee_cannot_confirm_my_proposal`, `test_scenarios.py::test_scenario_8_unauthorized_access` | ✅ |
| 15 | HR authorization | `require_hr`, `verify_principal` (HR = active `HRS` employee) | `test_mcp_server.py::test_hr_role_requires_hr_department`, `::test_hr_can_read_other_balances`, `::test_hr_role_cannot_create_from_employee_proposal` | ✅ |
| 16 | PDF extraction | `rag/extraction.py::extract_pdf` | `test_extraction.py::test_pdf_page_numbers_and_sections`, `::test_pdf_table_rows`, `::test_pdf_table_continued_on_next_page_is_merged`, `::test_pdf_running_header_and_footer_removed` | ✅ |
| 17 | DOCX extraction | `rag/extraction.py::extract_docx` | `test_extraction.py::test_docx_headings_and_article_numbers`, `::test_docx_tables_keep_cell_association`, `::test_docx_list_items_and_paragraph_order` | ✅ |
| 18 | Chunking | `rag/chunking.py` (one chunk per article/sub-article) | `test_chunking.py::test_one_chunk_per_sub_article`, `::test_every_document_chunked` | ✅ |
| 19 | Embeddings | `rag/embeddings.py` (Gemini, 768-d), `rag/ingest.py`, `rag/store.py` | `test_embeddings_and_ingest.py::test_ingest_all_documents`, `::test_gemini_batches_normalises_and_sets_task_type`, `::test_rerun_skips_unchanged_and_creates_no_duplicates`; live: 272 chunks | ✅ |
| 20 | Retrieval | `rag/retrieval.py` (hybrid vector + keyword + article reference) | `test_retrieval.py::test_leave_notice_question_prefers_leave_policy_over_faq`, `::test_article_reference`, `::test_alternative_queries_are_fused`; live: `scripts/evaluate_retrieval.py` 21/21 | ✅ |
| 21 | Source citations | `rag/citations.py`, `agent/policy_qa.py` (sources from metadata) | `test_retrieval.py::test_citation_formats`, `::test_source_metadata`, `test_agent.py::test_policy_question_uses_rag_with_sources`, `::test_grouped_citations_are_resolved` | ✅ |
| 22 | Policy precedence | `rag/catalog.py`, `rag/retrieval.py::_ensure_authoritative` | `test_retrieval.py::test_specialised_policy_wins_in_its_own_domain`, `::test_remote_work_question_uses_remote_policy` | ✅ |
| 23 | Outdated document handling | `rag/retrieval.py::_mark_superseded`, `build_context` status lines, answer prompt rule 3 | `test_retrieval.py::test_outdated_faq_never_ranks_above_authoritative_policy`, `::test_superseded_marking_and_ordering`, `test_extraction.py::test_faq_status_marks_it_as_reference` | ✅ |
| 24 | Georgian CLI | `cli/app.py`, `cli/ui.py` | `test_cli.py` (15 tests incl. real subprocess); live run | ✅ |
| 25 | Policy questions → RAG | `agent/agent.py` (POLICY_QUESTION), `agent/policy_qa.py` | `test_agent.py::test_policy_question_uses_rag_with_sources`, `::test_policy_question_without_information` | ✅ |
| 26 | Balance questions → MCP | `agent/agent.py` (BALANCE_QUERY → `get_leave_balance`) | `test_scenarios.py::test_scenario_1_balance` | ✅ |
| 27 | Leave request creation | `agent/agent.py` (draft → propose → confirm → create) | `test_scenarios.py::test_scenario_2_annual_request`, `::test_scenario_3_unpaid`, `test_agent.py::test_sick_request_is_created` | ✅ |
| 28 | Leave type identification (6 types, ambiguity) | `agent/intents.py`, `agent/agent.py` | `test_agent.py::test_ambiguous_request_asks_type_then_keeps_state`, `::test_vague_rest_wording_is_clarified`, `test_scenarios.py` (scenarios 3–7) | ✅ |
| 29 | Confirmation before creation | proposals + explicit "დიახ" (`_YES`/`_NO`, no LLM) | `test_scenarios.py::test_scenario_2_annual_request` (count before/after), `test_agent.py::test_nothing_created_before_confirmation`, `::test_decline_creates_nothing` | ✅ |
| 30 | Duplicate confirmation protection | `leave_proposals`, `SELECT … FOR UPDATE`, `UNIQUE(proposal_id)` | `test_scenarios.py::test_scenario_10_duplicate_confirmation`, `test_mcp_server.py::test_concurrent_confirmations_create_one_request`, `test_schema.py::test_one_request_per_proposal` | ✅ |
| 31 | Business-rule validation | `policies/rules.py`, `services/day_calculator.py`, `services/leave_rules.py` | `test_rules.py` (45 tests), `test_day_calculator.py`, `test_leave_rule_service.py`, `test_scenarios.py::test_scenario_2_every_rule_is_checked` | ✅ |
| 32 | Tests | `tests/` (393 tests, LLM mocked, no key needed) | `python -m pytest`; map: `docs/TESTING.md` | ✅ |
| 33 | README | `README.md` (all section 52 headings) | Manual review; followed during the end-to-end test | ✅ |
| 34 | `.env.example` | `.env.example` (all variables, no values for secrets) | `test_repository_hygiene.py::test_env_example_has_no_secret_values` | ✅ |
| 35 | `.gitignore` | `.gitignore` | `test_repository_hygiene.py::test_gitignore_excludes_env_and_caches` | ✅ |
| 36 | No secrets | settings from env, `SecretStr`, safe errors | `test_repository_hygiene.py::test_no_hardcoded_credentials_in_source`, `test_config.py::test_secrets_never_appear_in_repr`, `test_security.py`; full git-history scan (docs/SECURITY.md) | ✅ |
| 37 | Git-ready repository | Git history with one commit per phase; private GitHub repository | `git log` | ✅ |

## Official assignment text (`RAG, MCP Tasks.docx`)

| Requirement | Where | Status |
|---|---|---|
| Runnable and checkable project; README with run steps | README: Installation → CLI; `scripts/check_setup.py` | ✅ |
| Git + hosting platform; repository private until the deadline | Private GitHub repository | ✅ |
| No real keys in code or repository; state which model or platform key is needed | `.env.example`; README: Requirements (Supabase + Google Gemini key) | ✅ |
| Database: employees, leave types, requests (dates, status, created time), yearly limit or balance | rows 1–6 | ✅ |
| MCP tools: create, list with filters, remaining days by year and type, approve/reject, cancel, list types | rows 7–13 | ✅ |
| Employee-assistant actions match the policy's authority | rows 14–15; scenarios 8–9 | ✅ |
| Python CLI talking Georgian; distinguishes information / own balance / leave request, and the leave type | rows 24–28 | ✅ |
| RAG over the 7 documents: own parsing and chunking, no OCR, documents unchanged | rows 16–23; `test_repository_hygiene.py::test_supplied_source_files_are_unchanged` | ✅ |
| Answers grounded in retrieved passages, with a source; says so clearly when not found | rows 20–21, 25 | ✅ |
| Balance and request creation through the Task 1 MCP server | rows 26–27 (CLI ↔ MCP server over stdio) | ✅ |

## Section 48 scenarios

All ten are in `tests/test_scenarios.py`, one test per scenario, with an assertion per expected bullet.

| Scenario | Test | Status |
|---|---|---|
| 1 Balance | `test_scenario_1_balance` | ✅ |
| 2 Annual request | `test_scenario_2_annual_request`, `test_scenario_2_every_rule_is_checked[balance, overlap, probation, aud_restriction]` | ✅ |
| 3 Unpaid | `test_scenario_3_unpaid`, `test_scenario_3_unpaid_checks[notice_10_days, limit_30_days]`, `test_scenario_3_health_details_are_not_collected` | ✅ |
| 4 Sick | `test_scenario_4_sick_over_paid_balance` | ✅ |
| 5 Bereavement | `test_scenario_5_bereavement` | ✅ |
| 6 Study | `test_scenario_6_study`, `test_scenarios_5_to_7_explain_rules_even_without_llm_answer[STUDY]` | ✅ |
| 7 Parental | `test_scenario_7_parental` | ✅ |
| 8 Unauthorized access | `test_scenario_8_unauthorized_access` | ✅ |
| 9 Existing request mutation | `test_scenario_9_existing_request_mutation` | ✅ |
| 10 Duplicate confirmation | `test_scenario_10_duplicate_confirmation` | ✅ |
