# HROne Software Engineer Assignment — Complete Rulebook
## Employee Attendance & Analytics API | FastAPI + MongoDB

**Purpose:** A practical reference for implementing, testing, and submitting the assignment according to the provided contract.

---

# 1. Non-Negotiable Project Rules

1. Use **Python 3.11+, FastAPI, and MongoDB 6.0+**.
2. Use either PyMongo or Motor.
3. Keep **all application code in `app/main.py`**, as required by the assignment.
4. The application must start from the repository root using:

   `uvicorn app.main:app --port 8000`

5. `GET /health` must return HTTP 200 when MongoDB responds and HTTP 503 when it does not.
6. The application must be ready within 20 seconds.
7. Read `MONGO_URI` and `MONGO_DB` from environment variables. Environment variables must take priority over values in a local `.env`.
8. Never hard-code database credentials or connection details.
9. Create all required indexes automatically at startup. Index creation must be idempotent.
10. Do not make external network calls. MongoDB is the only permitted external service.
11. Handle simultaneous requests correctly.
12. Follow `openapi.yaml` exactly for endpoint paths, parameters, response fields, and status codes.
13. Analytics endpoints must use MongoDB aggregation pipelines rather than fetching all documents and calculating results in Python.
14. The system must remain efficient with approximately 100,000 attendance records.
15. Understand every line of the submitted code; a live walkthrough may be required.

# 2. Required Repository Structure

The submitted public GitHub repository must contain:

- `app/main.py` — all application code
- `requirements.txt` — every imported dependency
- `REVIEW.md` — Part A findings and fixes
- `DECISIONS.md` — answers to the five design questions
- `README.md` — setup, run instructions, and known limitations

Do **not** commit:

- `.env` files or secrets
- A Dockerfile
- Virtual environments
- Data dumps

The candidate kit also contains `openapi.yaml`, `DATA_MODEL.md`, sample data, a seed script, and templates. Use the actual supplied files as the source of truth for the precise data schema and API contract.

# 3. Data Model Rules

The database contains two collections:

- `employees`
- `attendance_logs`

## Employee identity

- `emp_code` is the permanent business identifier.
- Use `emp_code` to identify employees across the API and attendance records.
- MongoDB's internal `_id` must never be exposed in requests or responses.
- Attendance records are identified by the combination `(emp_code, date)`.
- Attendance records do not expose a separate record ID.

## Time and date conventions

- API instants such as `punched_at`, `punch_in`, `punch_out`, `created_at`, and history timestamps are **integer epoch milliseconds**.
- MongoDB stores instants as BSON datetimes in UTC.
- Convert timestamps at the API/database boundary.
- Reject timestamps supplied as seconds, floating-point numbers, or strings with HTTP 422.
- Calendar dates use `YYYY-MM-DD`.
- Months use `YYYY-MM`.
- Shift times use 24-hour `HH:MM` format.
- Calendar dates and shift times follow Indian Standard Time (IST, UTC+05:30).
- Handle PyMongo's default naive datetime objects consistently.

## Derived fields

The API calculates these fields:

- `work_hours`
- `late_minutes`
- `overtime_minutes`
- `half_day`

Clients must not provide these fields as trusted input. Store the computed values, and have analytics read the stored values rather than recalculate them.

## Missing fields

- Missing `history` means an empty list.
- Missing `half_day` means `false`.
- A weekday with no attendance record is **not** equivalent to an `ABSENT` record.

# 4. Attendance Status Rules

| Status | Meaning | Punch times | Counts as present? |
|---|---|---|---|
| `PRESENT` | Worked on site | Required for punch-in | Yes |
| `WFH` | Worked from home | Required for punch-in | Yes |
| `ON_DUTY` | Official duty away from office | Required for punch-in | Yes |
| `ABSENT` | Did not work, no approved leave | Both null | No |
| `LEAVE` | Approved leave | Both null | No |

Important:

- Punch-in creates records with `PRESENT`, `WFH`, or `ON_DUTY`.
- `ABSENT` and `LEAVE` records have both punch times null.
- Presence calculations must follow this table exactly.

# 5. The Ten Business Rules — R1 to R10

## R1 — Time zone, attendance date, and overnight shifts

1. Use IST for attendance dates and shift times.
2. The attendance date is the IST calendar date of `punch_in`, subject to the overnight-shift rule.
3. An overnight shift occurs when `shift_end <= shift_start`.
4. For an overnight shift, a punch-in earlier than `shift_end` belongs to the shift that started the previous day.
5. The overnight shift ends on the following calendar day.
6. Truncate all instants to whole seconds **before performing calculations**.
7. Store and return the truncated timestamps.

## R2 — Late arrival

- Grace period: 10 minutes.
- An employee is late only when punch-in is **strictly more than 10 minutes** after shift start.
- If late, `late_minutes` is the whole-minute floor since the original shift start—not since the end of the grace period.

Example for a 09:30 shift:

| Punch-in | `late_minutes` |
|---|---:|
| 09:40:00 | 0 |
| 09:40:01 | 10 |
| 10:15:59 | 45 |

Do not change the rule to “minutes after grace period.”

## R3 — Overtime

- Calculate whole minutes after shift end.
- Use floor rounding.
- Overtime is counted only when the duration is **at least 30 minutes**.
- If the duration is below 30 minutes, `overtime_minutes = 0`.

## R4 — Work hours

Calculate:

`work_hours = (punch_out - punch_in) in seconds / 3600`

- Round to two decimal places.
- Use **round-half-up**, not the language's default rounding where it may differ.
- Before punch-out, `work_hours` must be null.
- Apply timestamp truncation before calculating the duration.

## R5 — Half day

After punch-out:

- Set `half_day = true` if the **rounded** `work_hours` is less than `4.50`.
- Otherwise, set it to false.
- A half day counts as **0.5 present day**.

Do not compare the unrounded work duration to the threshold.

## R6 — Presence

- `PRESENT`, `WFH`, and `ON_DUTY` count as present.
- `ABSENT` and `LEAVE` do not.

## R7 — Working days

- Monday through Friday are working days.
- Do not apply a holiday calendar.
- For employees who join during a month, count working days starting from `joined_on`.
- `present_days` includes only qualifying Monday–Friday attendance records.
- Weekend records can still contribute to late-minute and overtime totals.

## R8 — Numeric rounding

- Report ordinary numeric values to two decimal places.
- Report rates to four decimal places.
- Use round-half-up.

## R9 — Headcount

Count employees whose `joined_on` is on or before the end of the reporting period.

- Include employees with no attendance records.
- Exclude employees who joined after the period ends.

## R10 — Pagination

- Page numbering starts at 1.
- Default `page_size` is 20.
- Maximum `page_size` is 100.
- `total` must represent all matching records **after filters are applied**, not just the current page.

# 6. Endpoint Rulebook

Follow the exact request and response schemas in `openapi.yaml`. The descriptions below summarize the required behaviour; they do not replace the contract.

## 6.1 `GET /health`

- Return 200 when MongoDB responds.
- Return 503 when MongoDB is unavailable.

## 6.2 `POST /employees`

- Create an employee.
- Duplicate `emp_code` must return 409.
- Invalid input must return 422.
- Do not expose MongoDB `_id`.

## 6.3 `GET /employees`

- Support the specified department filter.
- Apply pagination according to R10.
- Sort by `emp_code`.
- Ensure the list query is supported by appropriate indexes.

## 6.4 `POST /attendance/punch-in`

- Accept only the permitted punch-in statuses: `PRESENT`, `WFH`, or `ON_DUTY`.
- Apply R1 and R2.
- Use the correct attendance date and shift calculation.
- Make creation race-safe: simultaneous requests for the same employee and date must not create duplicate records.

## 6.5 `GET /attendance`

- Support the contract's employee, date, and status filters.
- Apply pagination and return the correct filtered total.
- Sort as specified by the API contract.
- Keep it efficient on approximately 100,000 records.

## 6.6 `POST /attendance/punch-out`

- Find the correct employee's attendance record.
- Return 404 when the required employee or record does not exist.
- Reject invalid or conflicting requests with the appropriate status code.
- Compute work hours, overtime, and half-day status according to R3, R4, and R5.
- Support overnight shifts.
- Ensure exactly one simultaneous punch-out succeeds; the others must conflict.
- Punch-in and punch-out must not create manual-correction history entries.

## 6.7 `PATCH /attendance/{emp_code}/{date}`

This endpoint regularizes an existing attendance record.

- Return 404 if the target record does not exist.
- Validate the requested changes.
- Recompute all affected derived fields.
- Record only the fields that actually changed in the audit entry.
- Append one history entry per successful manual correction.
- Include when the correction happened, who made it, why, and before/after values for changed fields.
- Never edit or remove existing history entries.
- Concurrent corrections must not silently overwrite or lose each other's history entries.

## 6.8 `GET /analytics/employees/{emp_code}/monthly`

- Produce the employee's monthly summary.
- Use MongoDB aggregation.
- Follow R6–R8 and the exact response contract.
- Handle missing attendance records and employees correctly according to the contract.
- Use stored derived fields for analytics.

## 6.9 `GET /analytics/departments/summary`

- Produce a monthly department summary.
- Use a MongoDB aggregation pipeline across the employee and attendance collections.
- Attendance records reference employees through `emp_code`; department information is on the employee.
- Include eligible employees even when they have no attendance logs.
- Exclude employees who joined after the reporting period ends.
- Calculate averages over the underlying records—not by averaging department or employee averages.
- Apply the presence, working-day, and numeric-rounding rules.

## 6.10 `GET /analytics/leaderboard/late`

- Use MongoDB aggregation, including a window function for ranking.
- Employees with equal ranking values share a rank.
- Use **competition ranking**: `1, 2, 2, 4`.
- Do not assign rank using an array index.
- Apply the requested limit after calculating ranks.
- Order tied rows by `emp_code`.

## 6.11 `GET /analytics/departments/{department}/trend`

- Use a MongoDB aggregation pipeline.
- Return exactly one row for every calendar day in the requested date range.
- Include weekends and days with no attendance records.
- Fill gaps according to the contract.
- Calculate the moving average over the preceding seven-day window.
- Early rows use a shorter window when fewer than seven days are available.
- Perform gap filling and moving-average calculation in MongoDB, not with a Python loop.

## 6.12 `GET /admin/explain/{endpoint}`

Allowed endpoint identifiers:

- `attendance_list`
- `employee_monthly`
- `department_summary`
- `late_leaderboard`
- `department_trend`

Requirements:

- Run MongoDB `explain` using `executionStats` verbosity.
- Return the raw explain output.
- Build the query or pipeline the same way as the corresponding real endpoint.
- Use the same filters, sort, and pagination.
- Accept the same relevant query parameters as the endpoint being explained.
- The plan must demonstrate index use (`IXSCAN`) and avoid collection scans (`COLLSCAN`) on the supplied large dataset.

# 7. HTTP Status-Code Rules

| Situation | Expected status |
|---|---:|
| Successful request | Contract-defined success code |
| Health check with responsive MongoDB | 200 |
| MongoDB unavailable for health check | 503 |
| Duplicate employee code | 409 |
| Concurrent conflicting punch-in or punch-out | 409 for losing requests |
| Missing employee or required attendance record | 404 |
| Invalid input or timestamp | 422 |
| Unexpected internal server error | Not acceptable as normal behaviour |

A 500 response is treated as a failed test. Handle known errors deliberately and avoid returning success for failed database operations.

# 8. Concurrency Rules

The grader sends parallel requests. Correctness cannot depend on requests arriving one at a time.

1. **Punch-in:** For simultaneous requests targeting the same employee and attendance date, exactly one request returns 201; the remaining conflicting requests return 409.
2. **Punch-out:** Exactly one simultaneous punch-out succeeds with 200; competing requests conflict.
3. **Regularization:** Concurrent corrections must not silently lose audit history entries.
4. **Duplicate employees:** Database-level uniqueness must prevent duplicate `emp_code` values even under concurrent creation.
5. **Atomicity:** Prefer atomic database operations and suitable unique indexes over “check first, then insert” logic that can race.

# 9. MongoDB and Performance Rules

- Create indexes at application startup.
- Index creation must be repeatable without producing duplicate indexes.
- The seeded database may initially have only MongoDB's default `_id` index.
- Design indexes around actual filters, sorting, unique keys, and analytics access patterns.
- Use an appropriate unique index for attendance identity `(emp_code, date)` and uniqueness for employee `emp_code`.
- Ensure list queries and analytics are backed by suitable indexes.
- Use aggregation for all analytics endpoints.
- Avoid loading the entire attendance collection into application memory.
- Avoid calculating analytics through Python loops over fetched records.
- Check execution plans through the explain endpoint.
- Test query performance using a dataset around 100,000 attendance records.

# 10. Required Review: `REVIEW.md`

The starter application contains **more than ten defects**. Review it carefully rather than assuming the happy path is correct.

For every finding, document:

1. **Location:** The function, endpoint, or relevant code area.
2. **Problem:** What is wrong.
3. **Evidence:** An input, test, or symptom that demonstrates the issue.
4. **Fix:** What you changed and why.

Also mention areas you inspected and determined were correct.

Investigate normal inputs, invalid inputs, boundary values, concurrent requests, and performance. Do not invent defects: each reported issue should correspond to the actual starter code and be supported by evidence.

# 11. Required Design Notes: `DECISIONS.md`

Answer these five questions in your own words, approximately **150–200 words total**:

1. Which indexes did you create, and why?
2. What happens when two identical punch-ins arrive at the same instant?
3. What does the leaderboard return when there is a tie at the cutoff?
4. How does the department summary count employees with no attendance logs?
5. What would you change if the dataset grew to 100 times its current size?

Be ready to explain your answers and the relevant code during a live walkthrough.

# 12. Testing Rulebook

Do not rely only on the nine sample documents. Build test data for awkward cases.

## Essential business-rule tests

- Punch-in exactly at shift start.
- Punch-in exactly at the end of the 10-minute grace period.
- Punch-in one second after the grace period.
- Overtime just below 30 minutes and exactly at 30 minutes.
- Work hours near rounding boundaries.
- Rounded work hours immediately below and at 4.50.
- Invalid timestamps supplied as strings, floats, or seconds.
- An overnight shift crossing midnight.
- A punch-in close to an IST/UTC calendar-date boundary.
- Missing `history` and missing `half_day`.
- `ABSENT` and `LEAVE` records with null punch times.
- Weekend attendance records.
- A mid-month joiner.
- A weekday with no attendance record.
- Employees with no logs.
- Tied leaderboard values, including a tie at the limit.
- Department trend days with no records.
- Multiple simultaneous punch-ins.
- Multiple simultaneous punch-outs.
- Concurrent corrections to the same record.
- Pagination at the first page, final page, and maximum page size.
- MongoDB unavailable during the health check.

## Analytics tests

Compare API output against independently calculated expected results for:

- Monthly employee summaries.
- Department headcount and averages.
- Late leaderboard ranks and tie handling.
- Department daily trend, missing dates, and moving averages.
- Numeric precision and rounding.

# 13. Evaluation Weights

| Evaluation area | Weight |
|---|---:|
| Part A — Review and fixes | 10% |
| CRUD, business rules, and concurrency | 30% |
| Analytics correctness | 30% |
| MongoDB usage and performance | 15% |
| Design decisions and code quality | 15% |

Correct analytics and edge-case handling are especially important. A plausible result is not enough if it differs from the contract.

# 14. Final Submission Checklist

## Code and runtime
- [ ] `app/main.py` contains all application code.
- [ ] `requirements.txt` contains every required dependency.
- [ ] A clean clone can run after installing requirements.
- [ ] `uvicorn app.main:app --port 8000` starts successfully.
- [ ] `/health` becomes ready within 20 seconds.
- [ ] Database configuration comes from environment variables.
- [ ] Indexes are created automatically at startup.
- [ ] All required endpoints are implemented.
- [ ] Request and response schemas match `openapi.yaml`.
- [ ] Correct HTTP status codes are returned.
- [ ] R1–R10 have been tested.
- [ ] Concurrent operations have been tested.
- [ ] Analytics use MongoDB aggregation pipelines.
- [ ] Explain plans show index scans without collection scans.

## Documentation and repository
- [ ] `REVIEW.md` contains evidence-based findings and fixes.
- [ ] `DECISIONS.md` answers all five questions in approximately 150–200 words.
- [ ] `README.md` explains setup and execution.
- [ ] No `.env`, secrets, Dockerfile, virtual environment, or data dumps are committed.
- [ ] Repository visibility is public.
- [ ] Submission link is submitted through the specified form.

# 15. Deadline and Submission Conduct

According to the hiring email shared for this assignment:

- **Deadline:** Thursday, 15 October 2026, at 11:59 PM IST.
- Submit the public GitHub repository link through the Microsoft form provided in the email.
- Keep the repository public through 31 October 2026.
- Do not publicly post the problem statement or solution before 15 November 2026.
- Work independently. Reference material and AI tools may be used, but you are responsible for understanding and explaining your submission.
- Partial submissions are permitted; document anything unfinished in `README.md`.

---

## Golden Rule

**The actual `openapi.yaml`, `DATA_MODEL.md`, starter code, and assignment statement are the authority.** This rulebook summarizes the supplied assignment; when a detail depends on a precise field name, query parameter, response shape, or status code, verify it against those files before implementing it.
