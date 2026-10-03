# Specification summary (Phase 0)

Derived from every file in `Junior_AI_Candidate_Package/`. Article numbers refer to
`Leave_and_Absence_Policy_v4.0.docx` unless another document is named.

## 1. Assignment (`RAG, MCP Tasks.docx`)

**Task 1: MCP server and database**
- Database stores at minimum: employees, leave types, leave requests (dates, status, created time),
  and each employee's annual limit or balance.
- MCP tools: create request (employee, type, start, end); list requests with filters (employee,
  status, date range); remaining days by year and leave type; approve or reject; cancel; list types.
- The server serves both the employee assistant and HR. The assistant's actions must match the
  authority the policy grants it.

**Task 2: Python CLI in Georgian**
- Answers policy questions with RAG and cites sources. If nothing relevant is found, it says so explicitly.
- Shows the employee's balance and creates leave requests **through the Task 1 MCP server**.
- Distinguishes three goals: information, own balance, or a leave request. For a request it also
  determines the leave type.
- Parsing and chunking the documents is part of the task. The PDFs are text-based, so no OCR is needed.
  Source documents must not be modified.

**General rules:** use Git, include a README, keep real keys out of the repo, document which model
or platform key is needed, and keep the repository private until the deadline.

## 2. Data dictionary (`data_dictionary.pdf`)

- CSVs are UTF-8 with a header row. The dictionary says ISO dates, but **the supplied files use
  `M/D/YYYY`** (`created_at` is ISO 8601). The importer parses both formats, and the files are not changed.
- The data reflects the state on **2026-10-19**, so `APP_TODAY=2026-10-19`. Historical balances are not reconstructed.
- The schema is ours to design: extra columns, tables, indexes and views are allowed.
- `probation_end_date` is the **last day of probation (inclusive)**. An empty `manager_id` marks a department head.
- `annual_limit_days` for SICK is the *paid* limit, not a cap on recorded sickness.
- `carried_over_days` is the initial carried-over amount after the forfeit; it is **not** a remaining balance. It applies to ANNUAL only (4.7).
- BEREAVEMENT and PARENTAL have no annual balance and no entitlement rows.
- Assistant-created requests are saved with `status=pending` and `created_via=assistant`.
- Only `public_holidays.csv` is used for day counting (2.3).
- Balance formula (5.1): `available = entitled + carried_over − approved − pending`, counting only requests
  with the same employee, type and leave year. Rejected and cancelled requests are excluded. SICK: a negative result shows as 0.
- The assistant recognises all 6 types and creates only ANNUAL, SICK and UNPAID. BEREAVEMENT, STUDY and PARENTAL get an explanation and a redirect.
- SICK: if a new request exceeds the remaining paid days, do not create it and send the employee to HR (6.4).
  Never say that sickness can no longer be recorded.
- UNPAID needs a short reason, stored in `comment`. No health details are collected.
- There are no approved learning plans in the data, so the assistant never assumes one.
- **Proposals**: a custom table linked to `employee_id`, `conversation_id` and `proposal_id`. Conversation history alone does
  **not** count as permission to write. Confirming the same proposal again returns the same request ID and creates no duplicate.

## 3. Leave policy rules to enforce in code

| Rule | Article | Enforcement |
|---|---|---|
| Request belongs to the year of its first day. A cross-year range must be split. | 2.1 | Reject ranges that span two years |
| Working day = Mon–Fri and not in the HR holiday list. Calendar day = every day. | 1.3, 2.2 | `LeaveDayCalculator` |
| Whole days only | 2.4 | Date-only inputs |
| ANNUAL blocked on any day ≤ `probation_end_date`. The HR 2-day exception is not available through the assistant. SICK, UNPAID and BEREAVEMENT are exempt. | 4.3 | Reject |
| ANNUAL notice: 1–5 days needs 5 working days; 6–15 days needs 15. Count full working days strictly between the submission date and the start date. | 4.4 | Reject. Late emergencies go straight to the manager. |
| ANNUAL max 15 working days per request. Adjacent requests separated only by weekends or holidays count as one continuous period. | 4.5 | Reject when a single request or the merged period is over 15 |
| AUD: any day in 12-01..12-20 or 01-15..03-15 | 4.6 | Reject and refer to the manager or project partner |
| ANNUAL and UNPAID: days ≤ available balance | 5.1, 7.1 | Reject |
| SICK: days > available paid days | 6.4 | Do not create. Redirect to HR; recording is still possible through HR. |
| SICK: submitted no later than 2 working days after the first day (first day not counted). Future dates only when the period is already known. | 6.2, 12.4 | Reject a late submission. Ask the user to confirm a future period is known in advance. |
| UNPAID: 10 working days notice (4.4 counting) and a short reason | 7.2 | Reject or ask for the reason |
| Assistant creates only ANNUAL, SICK and UNPAID, for the current year | 12.2, 12.3 | Reject next-year dates (the portal accepts them from 1 December) |
| No overlap with the employee's own approved or pending requests | 12.3 | Reject |
| Assistant cannot approve, reject, cancel or modify requests, change entitlements or employee data, or view or act for another employee. This includes managers. | 4.8, 5.2, 12.3 | Server-side role checks |
| Show approved and pending days together with the balance | 5.2 | Balance output |
| When creation fails: give the reason, cite the article, suggest an alternative | 12.3 | Error payload includes the article and a suggestion |
| Data-changing AI agents need explicit user confirmation | InfoSec 6.4 | Proposal plus confirmation |

## 4. Document precedence

| Topic | Authoritative | Outdated or general (conflicting statement) |
|---|---|---|
| Leave | Leave Policy v4.0 (1.4) | Handbook v3.1 §5: 5-day notice for all requests, 10-day carry-over until 30 June, medical certificate after >3 days. FAQ 2025 a.2, a.3, a.6: the same outdated rules, plus "24 days" in a.1. |
| Remote work | Remote & Hybrid v2.0 (1.3: replaces Handbook §6 and the FAQ) | Handbook §6 and FAQ b.1: "3 days/week". Current rule: **2**. |
| Information security / AI tools | InfoSec v3.2 | Handbook §9.3 and FAQ d.3 refer to it |
| Learning / study plans | L&D v1.2 (study-leave duration is in Leave Policy art. 9) | — |
| Travel and expenses / home equipment | Travel & Expense v2.3 | FAQ e.* (consistent: 300 GEL, 30 days, 10 working days) |

The Handbook (1.2) states that specialised policies override it, and the FAQ describes itself as reference-only and possibly outdated.

## 5. Test-data facts (as of APP_TODAY 2026-10-19, a Monday)

- E1001 Nino Beridze, AUD. ANNUAL: 25 + 3 − approved 15 − pending 3 = **10**. SICK: 10 − 2 = **8**.
- E1002, ADV. ANNUAL: 24 − 20 = **4** (rejected request #8 excluded). E1004, TEC: probation runs until **2026-11-30**, entitlement is 8.
- E1009: STUDY 5 − 1 = 4, SICK 8.
- **26–30 Oct 2026 = 5 working days, but only 4 notice days, so it violates 4.4.** The earliest valid start is 27 Oct.
- All 27 CSV `days` values match the calculator. Some holidays fall on weekends (e.g. 2026-03-08), which is handled.
