# DECISIONS.md

### 1. Indexes
We created five indexes on `employees`:
- `{"emp_code": 1}` (unique) — Guarantees uniqueness and O(1) lookups for employee authentication and profile fetch.
- `{"department": 1, "emp_code": 1}` — Powers department filtering with cursor-based sorting on `emp_code`.
- `{"department": 1, "joined_on": 1}` & `{"joined_on": 1}` — Accelerates aggregation pipelines in `department_summary` to filter headcount eligibility without collection scans.

On `attendance_logs`:
- `{"emp_code": 1, "date": 1}` (unique) — Enforces single attendance per employee per date and supports employee monthly analytics.
- `{"date": -1, "emp_code": 1}` — Powers reverse chronological attendance listing and late leaderboard aggregations.

*Rejected*: An index on `{"status": 1}` alone was rejected due to very low cardinality (5 values), which causes index scan overhead without selectivity.

### 2. Punch-in Race
When two identical punch-in requests arrive simultaneously:
FastAPI processes them concurrently. Both may pass the initial existence check. However, because `{"emp_code": 1, "date": 1}` is a unique compound index, MongoDB's atomic document locking allows only the first `insert_one` to commit (returning HTTP 201). The second request immediately encounters a `pymongo.errors.DuplicateKeyError`, caught by our exception handler to return HTTP 409 Conflict. No duplicates can ever be created.

### 3. Ties
When employees tie on late minutes at the `limit` cutoff, we employ standard competition ranking (`1, 2, 2, 4`) via MongoDB's `$setWindowFields` with `rank`. When multiple employees share the cutoff rank, all qualifying tied employees are preserved up to that rank tier, then sorted deterministically by `emp_code` ascending per the OpenAPI specification.

### 4. Headcount
The department summary pipeline initiates from the `employees` collection rather than `attendance_logs`. It applies `$match` on `joined_on <= period_end`, includes all employees belonging to the department regardless of logs, and `$lookup`s attendance records. Unmatched employees result in zero logs but correctly increment the department headcount.

### 5. Scaling to 100x Data
At 100x scale (~10 million attendance records), we would shard `attendance_logs` using a hashed compound shard key `(emp_code, date)` to distribute write traffic, and pre-aggregate daily/monthly metrics into rollup collections via background change streams.
