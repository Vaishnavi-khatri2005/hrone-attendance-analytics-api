# Employee Attendance & Analytics API

A FastAPI backend service backed by MongoDB for managing employee work shifts, attendance punch-in/out cycles, regularization audit trails, and departmental analytics.

The service provides complete RESTful endpoints for attendance tracking, compliance business rules, and MongoDB aggregation pipelines.

---

## Technology Stack

- **Python**: 3.11+
- **Framework**: FastAPI (v0.110+)
- **Server**: Uvicorn (v0.29+)
- **Database**: MongoDB (6.0+ / Atlas) via PyMongo (v4.7+)
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

The application requires two configuration parameters:
- `MONGO_URI`: The MongoDB connection string (e.g., MongoDB Atlas or local MongoDB).
- `MONGO_DB`: The target database name (e.g., `attendance_db`).

Configuration can be set directly via shell environment variables or via an optional local `.env` file. **Existing environment variables always take precedence over a `.env` file, and `.env` files must never be committed to source control** (protected via `.gitignore`).

#### Option A: Direct Shell Environment Variables

**Windows (PowerShell):**
```powershell
$env:MONGO_URI = "mongodb+srv://<username>:<password>@<cluster-address>/?retryWrites=true&w=majority"
$env:MONGO_DB = "attendance_db"
```
*(Or for a local MongoDB instance: `$env:MONGO_URI = "mongodb://localhost:27017"`)*

**macOS / Linux (Bash / Zsh):**
```bash
export MONGO_URI="mongodb+srv://<username>:<password>@<cluster-address>/?retryWrites=true&w=majority"
export MONGO_DB="attendance_db"
```
*(Or for a local MongoDB instance: `export MONGO_URI="mongodb://localhost:27017"`)*

#### Option B: Optional Local `.env` File
Alternatively, create an untracked `.env` file in the project root:
```ini
MONGO_URI=mongodb+srv://<username>:<password>@<cluster-address>/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

---

## Seeding Sample Data

The repository includes sample starter datasets in `sample_data/`:
- `sample_data/employees.json`
- `sample_data/attendance_logs.json`

To populate these records into your configured MongoDB database:

```bash
python sample_seed.py
```

---

## Starting the Service

Start the application from the repository root using the command:

```bash
uvicorn app.main:app --port 8000
```

- **Health Check**: [http://localhost:8000/health](http://localhost:8000/health)
- **Interactive API Documentation (Swagger UI)**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Alternative Documentation (ReDoc)**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## API Endpoints & Features

### Core Operations
- `GET /health` — Verifies connectivity to MongoDB (HTTP 200 on success, HTTP 503 if unreachable).
- `POST /employees` — Creates an employee record (HTTP 409 on duplicate `emp_code`).
- `GET /employees` — Paginated list of employees with optional `department` filtering, sorted by `emp_code`.
- `POST /attendance/punch-in` — Records attendance punch-in; handles IST shift boundaries, overnight shifts, 10-minute grace period floor calculation (R2), and duplicate punch prevention.
- `GET /attendance` — Reverse-chronological attendance records list with date, employee, and status filters.
- `POST /attendance/punch-out` — Updates punch-out time, computing overtime (R3, threshold >=30 min), work hours (R4, round-half-up), and half-day status (R5).
- `PATCH /attendance/{emp_code}/{date}` — Regularization endpoint appending before/after audit records to `history` with optimistic concurrency checks.

### Analytics & Reporting
- `GET /analytics/employees/{emp_code}/monthly` — Monthly employee attendance metrics, expected working days (excluding weekends), total work hours, overtime, and late arrivals.
- `GET /analytics/departments/summary` — Cross-collection aggregation including all eligible employees (including those with zero attendance logs) as of period end.
- `GET /analytics/leaderboard/late` — Standard competition ranking (`1, 2, 2, 4`) for late arrivals with deterministic tie-breaking.
- `GET /analytics/departments/{department}/trend` — Daily attendance series with gap-filling (`$densify`) and 7-day moving averages.
- `GET /admin/explain/{endpoint}` — MongoDB `explain("executionStats")` verifying index usage across supported queries and aggregation pipelines.

---

## Running Verification Tests

The repository includes a standalone unit test suite in `tests/test_rules.py` verifying business rules R1 through R5:

```bash
python tests/test_rules.py
```

---

## Known Limitations

- Requires MongoDB 6.0 or newer to support aggregation pipeline stages `$densify` and `$setWindowFields`.
- Per business rules, working days are strictly Monday through Friday; company holiday calendars are not tracked.
