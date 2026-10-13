# Employee Attendance & Analytics API

A robust, production-grade FastAPI service backed by MongoDB Atlas for managing employee work shifts, attendance punch-in/out cycles, regularization audit trails, and multi-dimensional departmental analytics.

Built to comply strictly with the HROne Engineering Assignment Contract v2.0 (`openapi.yaml`, `DATA_MODEL.md`, `rulebook.md`).

---

## Technology Stack

- **Python**: 3.11+
- **Framework**: FastAPI (v0.110+)
- **Server**: Uvicorn (v0.29+)
- **Database**: MongoDB (6.0+ / Atlas M0+) via PyMongo (v4.7+)
- **Data Validation**: Pydantic v2 (v2.6+)

---

## Prerequisites & Installation

### 1. Clone & Set Up Virtual Environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Environment Configuration

The application reads configuration from environment variables or a local `.env` file (existing OS environment variables always take precedence).

Create a `.env` file in the project root:
```ini
MONGO_URI=mongodb+srv://<username>:<password>@<cluster-address>/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```
*(Alternatively, for local Docker: `mongodb://localhost:27017`)*

---

## Seeding Sample Data

To populate the initial sample dataset (from `sample_data/employees.json` and `sample_data/attendance_logs.json`):

```bash
python sample_seed.py
```

---

## Starting the Service

Start the application from the repository root using the assignment-mandated command:

```bash
uvicorn app.main:app --port 8000
```

- **Health Check**: [http://localhost:8000/health](http://localhost:8000/health)
- **Interactive OpenAPI Documentation (Swagger UI)**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc Documentation**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## API Endpoints & Features

### Core Operations
- `GET /health` — Active ping against MongoDB Atlas (returns HTTP 200 when connected, HTTP 503 on failure).
- `POST /employees` — Register an employee (returns HTTP 409 on duplicate `emp_code`).
- `GET /employees` — Paginated list of employees with optional `department` filtering, sorted by `emp_code`.
- `POST /attendance/punch-in` — Register attendance punch-in; enforces IST shifts, overnight shift detection, R2 10-minute grace period floor calculation, and race condition prevention.
- `GET /attendance` — Reverse-chronological attendance records list with date, employee, and status filters.
- `POST /attendance/punch-out` — Atomic punch-out with overnight support, R3 overtime threshold (>=30 min), R4 round-half-up work hours, and R5 half-day calculation.
- `PATCH /attendance/{emp_code}/{date}` — Optimistically-locked regularization endpoint appending before/after audit records to `history` without losing concurrent updates.

### Analytics & Reporting
- `GET /analytics/employees/{emp_code}/monthly` — Monthly employee attendance metrics, expected vs actual working days (excluding weekends), total work hours, overtime, and late arrivals.
- `GET /analytics/departments/summary` — Cross-collection aggregation including all eligible employees (even with 0 attendance logs) as of period end.
- `GET /analytics/leaderboard/late` — Standard competition ranking (`1, 2, 2, 4`) for late arrivals with deterministic tie-breaking.
- `GET /analytics/departments/{department}/trend` — Daily attendance series with gap-filling (`$densify`) and rolling 7-day moving averages.
- `GET /admin/explain/{endpoint}` — Real query execution stats verifying index scans (`IXSCAN`) across all major listing and analytics pipelines.

---

## Running Project Verification Tests

Unit and integration test suites covering all business rules and endpoints are included.

To execute the test verification suite:
```powershell
.\.venv\Scripts\python.exe -c "
import glob, subprocess, sys
scripts = sorted(glob.glob('scratch/test_step*.py'))
for s in scripts:
    res = subprocess.run([sys.executable, s], capture_output=True, text=True)
    print(f'Ran {s}: {\"PASSED\" if res.returncode == 0 else \"FAILED\"}')
"
```

---

## Known Limitations

- The service requires MongoDB 6.0+ for `$densify` and `$setWindowFields` aggregation stages.
- External holiday calendars are not tracked; per rule R7, standard working days are strictly Monday through Friday.
