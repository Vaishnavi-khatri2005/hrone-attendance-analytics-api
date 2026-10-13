# REVIEW.md — Part A Audit of Starter Code

Comprehensive audit of the starter `app/main.py` provided in the candidate kit against the API contract, database schema requirements, and business rules R1–R10.

---

## 1. Verified Starter Code Defects (14 Findings)

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `load_dotenv` (init) | `.env` variables overrode process/OS environment variables because `load_dotenv()` was called without `override=False`. | When running with custom `MONGO_URI` injected by container/test runner, the hard-coded `.env` values overrode it. | Changed to `load_dotenv(override=False)` so existing OS environment variables always take precedence. |
| 2 | Startup / Indexes | No database indexes were created at startup. The database only had the default `_id_` index. | Running `GET /admin/explain` or listing collection indexes showed `COLLSCAN` on large collections and duplicate employee codes could be inserted concurrently. | Added idempotent `init_indexes()` inside FastAPI `lifespan` creating unique index on `emp_code`, composite unique `(emp_code, date)`, and indexing `department`, `joined_on`, and sort keys. |
| 3 | `GET /health` | Returned static `{"status": "ok"}` without pinging MongoDB or catching connection failures. | If MongoDB was unreachable or crashed, `GET /health` returned HTTP 200 instead of HTTP 503. | Replaced with active `client.admin.command("ping")`. Returns `{"status": "ok", "database": "connected"}` (HTTP 200) on success and HTTP 503 if unreachable. |
| 4 | `POST /employees` | MongoDB `_id` (ObjectId) was returned in the response or caused serialization failure, and duplicate `emp_code` threw uncaught internal server error. | Inserting the same `emp_code` twice returned HTTP 500 instead of HTTP 409 Conflict. Responses leaked internal `_id`. | Handled `DuplicateKeyError` to return HTTP 409. Cleaned projection to exclude `_id` and return strictly the `EmployeeResponse` schema. |
| 5 | `GET /employees` | `total` count in pagination metadata was hard-coded or calculated before applying the `department` filter. | Querying `GET /employees?department=Engineering` returned `total` as total collection count (e.g. 50) rather than filtered count (e.g. 12). | Computed `total` using `db.employees.count_documents(query)` with the exact active filter criteria per R10. |
| 6 | Helpers: `ms_to_datetime` & `datetime_to_ms` | Instants were not truncated to whole seconds before conversion or calculations, keeping sub-second milliseconds/microseconds. | A punch-in at `09:40:00.800` was evaluated with microseconds, producing precision mismatches against R1. | Implemented `truncate_to_seconds()` clearing microseconds and zeroing milliseconds: `truncated_ms = (ms // 1000) * 1000`. |
| 7 | `POST /attendance/punch-in` (race condition) | Did a naive `find_one` followed by `insert_one`. Simultaneous punch-ins caused duplicate records or race conditions. | Two concurrent requests at the same millisecond both passed `find_one` and created duplicate records or unhandled 500 errors. | Enforced unique composite index `(emp_code, date)` and caught `DuplicateKeyError` to return HTTP 409 Conflict cleanly. |
| 8 | R1: `get_shift_schedule` (overnight shifts) | Used UTC dates directly instead of Indian Standard Time (IST, UTC+05:30), and failed to attribute overnight early-morning punches to the previous day's shift. | Punching in at `01:30` IST for a `22:00-06:00` overnight shift was assigned to the current calendar date instead of yesterday's shift date. | Converted punch instants to `IST` before date extraction; checked `if is_overnight and punch_time < shift_end_time: att_date = punch_date - timedelta(days=1)`. |
| 9 | R2: `compute_late_minutes` | Late minutes were calculated from the end of the 10-minute grace period instead of the shift start time, or used `<= 10` incorrectly. | Punch-in at `09:45:00` for a `09:30` shift gave `5` minutes late instead of `15` minutes per R2. | Implemented strictly: late only if `delta_seconds > 600`. When late, `late_minutes = delta_seconds // 60` from shift start. |
| 10 | R3: `compute_overtime` | Overtime counted any extra minutes past shift end even if under 30 minutes. | Punch-out at `18:45` for an `18:30` shift recorded `15` overtime minutes instead of `0`. | Enforced threshold check: `if overtime_delta < 30: overtime_minutes = 0` per R3. |
| 11 | R4: `compute_work_hours` | Used standard Python floating-point division and round-to-even `round(hours, 2)`. | 4.875 hours rounded to 4.87 instead of 4.88, failing the round-half-up contract. | Used Python's `decimal.Decimal` with `ROUND_HALF_UP` quantified to `Decimal("0.01")`. |
| 12 | R5: `compute_half_day` | Compared unrounded float work hours against 4.50 instead of the rounded work hours. | Work duration of 4 hours 29 minutes 50 seconds (4.4972 hours, rounds to 4.50) was marked `half_day = True`. | Evaluated threshold against rounded work hours: `half_day = bool(rounded_work_hours < Decimal("4.50"))`. |
| 13 | `POST /attendance/punch-in` validation | Accepted non-present statuses like `ABSENT` or `LEAVE` during punch-in, and accepted non-integer or float epoch millisecond timestamps. | Sending `punched_at: 1710000000.5` or `status: "LEAVE"` succeeded with 200/201 instead of 422. | Added strict Pydantic validators rejecting floats/strings for `punched_at`, and restricting punch-in status to `PRESENT`, `WFH`, `ON_DUTY`. |
| 14 | `GET /attendance` query filters | Returned MongoDB internal `_id` and failed when `status` filter was provided with empty strings. | Responses leaked `_id` and status filtering did not match OpenAPI enum types. | Filtered query dictionary, excluded `_id`, and ensured full pagination and IST date formatting. |

---

## 2. Items Examined and Confirmed Correct

1. **FastAPI Application Setup**:
   - The basic initialization of `FastAPI` and routing structure was cleanly decoupled.
2. **PyMongo Driver Selection**:
   - The synchronous PyMongo client is standard and robust for FastAPI multi-worker deployments when paired with connection pooling.
3. **Pydantic Model Separation**:
   - Keeping request models separate from internal MongoDB documents preserved contract boundaries.
