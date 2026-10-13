"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Optional

from bson import json_util
from bson.son import SON
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, ServerSelectionTimeoutError

# Load .env file without overriding existing OS environment variables
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently."""
    # employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True, name="idx_employees_emp_code")
    db.employees.create_index([("department", 1)], name="idx_employees_department")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="idx_employees_dept_emp_code")
    db.employees.create_index([("joined_on", 1)], name="idx_employees_joined_on")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_employees_dept_joined_on")

    # attendance_logs indexes
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="idx_attendance_emp_date"
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)], name="idx_attendance_date_emp"
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", -1)], name="idx_attendance_emp_date_desc"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: verify connectivity and ensure indexes exist
    try:
        client.admin.command("ping")
        init_indexes()
    except (ConnectionFailure, ServerSelectionTimeoutError, Exception):
        # Allow startup so /health can report 503 if MongoDB is down
        pass
    yield
    # Shutdown: clean up client connection pool
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Helpers (Attendance Rules R1 - R5)
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate datetime instant to whole seconds per R1."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0)


def ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def datetime_to_ms(dt: datetime) -> int:
    """Convert datetime to integer epoch milliseconds truncated to whole seconds."""
    truncated_dt = truncate_to_seconds(dt)
    return int(truncated_dt.timestamp() * 1000)


def get_shift_schedule(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> tuple[str, datetime, datetime]:
    """
    R1 — Attendance date and shift boundaries in IST (UTC+05:30).
    Returns (attendance_date_str, shift_start_dt, shift_end_dt).
    - If shift_end <= shift_start, the shift is an overnight shift.
    - For an overnight shift, a punch-in strictly earlier than shift_end belongs to the previous day.
    - Overnight shift end falls on the following calendar day.
    """
    punch_in = truncate_to_seconds(punch_in)
    punch_in_ist = punch_in.astimezone(IST)

    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)

    punch_time = punch_in_ist.time()
    shift_end_time = time(eh_h, eh_m)

    if is_overnight and punch_time < shift_end_time:
        att_date = punch_in_ist.date() - timedelta(days=1)
    else:
        att_date = punch_in_ist.date()

    attendance_date_str = att_date.isoformat()
    shift_start_dt = datetime.combine(att_date, time(sh_h, sh_m), tzinfo=IST)
    if is_overnight:
        shift_end_dt = datetime.combine(att_date + timedelta(days=1), time(eh_h, eh_m), tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(att_date, time(eh_h, eh_m), tzinfo=IST)

    return attendance_date_str, shift_start_dt, shift_end_dt


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str = "18:30",
) -> int:
    """
    R2 — Late arrival minutes.
    - Grace period: 10 minutes.
    - Late only if punch-in is strictly more than 10 minutes (600s) after shift start.
    - When late, late_minutes = floor whole minutes elapsed since shift_start.
    """
    _, shift_start_dt, _ = get_shift_schedule(punch_in, shift_start, shift_end)
    punch_in = truncate_to_seconds(punch_in)
    delta_seconds = int(punch_in.timestamp()) - int(shift_start_dt.timestamp())
    if delta_seconds > 600:
        return int(delta_seconds // 60)
    return 0


def compute_overtime(
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    attendance_date_str: str,
) -> int:
    """
    R3 — Overtime minutes.
    - Whole minutes after scheduled shift end.
    - Awarded only when overtime duration >= 30 minutes (1800s); otherwise 0.
    - Supports overnight shifts.
    """
    if punch_out is None:
        return 0
    punch_out = truncate_to_seconds(punch_out)
    att_date = date.fromisoformat(attendance_date_str)
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)

    if is_overnight:
        shift_end_dt = datetime.combine(att_date + timedelta(days=1), time(eh_h, eh_m), tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(att_date, time(eh_h, eh_m), tzinfo=IST)

    delta_seconds = int(punch_out.timestamp()) - int(shift_end_dt.timestamp())
    if delta_seconds >= 1800:
        return int(delta_seconds // 60)
    return 0


def compute_work_hours(
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
) -> Optional[float]:
    """
    R4 — Work hours.
    - (punch_out - punch_in) in seconds / 3600.
    - Round-half-up to 2 decimal places using Decimal.
    - Null (None) if punch_out is missing.
    """
    if punch_in is None or punch_out is None:
        return None
    in_sec = int(truncate_to_seconds(punch_in).timestamp())
    out_sec = int(truncate_to_seconds(punch_out).timestamp())
    elapsed_seconds = out_sec - in_sec
    if elapsed_seconds < 0:
        return 0.0
    hours_dec = Decimal(elapsed_seconds) / Decimal(3600)
    rounded = hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 — Half-day flag.
    - After punch-out, half_day = True if rounded work_hours < 4.50; otherwise False.
    - False if work_hours is None.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(effective_start: date, end_date: date) -> int:
    """R7 — Count Mon-Fri working days between effective_start and end_date inclusive."""
    if effective_start > end_date:
        return 0
    cur = effective_start
    working_days = 0
    while cur <= end_date:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def round_half_up(val: float, decimals: int = 2) -> float:
    """R8 — Round-half-up numeric value to specified decimal places."""
    d = Decimal(str(val))
    target = Decimal("1") if decimals == 0 else Decimal("0." + "0" * decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: list[Employee]
    total: int
    page: int
    page_size: int


def doc_to_employee(doc: dict) -> dict:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        epoch_ms = int(created_at.timestamp() * 1000)
    elif isinstance(created_at, (int, float)):
        epoch_ms = int(created_at)
    else:
        epoch_ms = 0

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": epoch_ms,
    }


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class AttendanceStatus(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100000000000,
        le=4102444800000,
        description="Epoch milliseconds. Defaults to now.",
    )
    status: PresenceStatus = Field(default=PresenceStatus.PRESENT)

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be an integer epoch timestamp in milliseconds")
            if v < 100000000000 or v > 4102444800000:
                raise ValueError("punched_at is out of range or in seconds")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100000000000,
        le=4102444800000,
        description="Epoch milliseconds. Defaults to now.",
    )

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be an integer epoch timestamp in milliseconds")
            if v < 100000000000 or v > 4102444800000:
                raise ValueError("punched_at is out of range or in seconds")
        return v


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100000000000,
        le=4102444800000,
    )
    punch_out: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100000000000,
        le=4102444800000,
    )

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_instants(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("Instant must be an integer epoch timestamp in milliseconds")
            if v < 100000000000 or v > 4102444800000:
                raise ValueError("Instant is out of range or in seconds")
        return v


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: dict[str, dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: AttendanceStatus
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntry] = []


class AttendancePage(BaseModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int = Field(..., ge=1)
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class Trend(BaseModel):
    department: str
    items: list[TrendItem]


class ExplainEndpointEnum(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]





def doc_to_attendance(doc: dict) -> dict:
    punch_in_val = doc.get("punch_in")
    punch_in_ms = None
    if isinstance(punch_in_val, datetime):
        punch_in_ms = datetime_to_ms(punch_in_val)
    elif isinstance(punch_in_val, (int, float)):
        punch_in_ms = int(punch_in_val // 1000 * 1000)

    punch_out_val = doc.get("punch_out")
    punch_out_ms = None
    if isinstance(punch_out_val, datetime):
        punch_out_ms = datetime_to_ms(punch_out_val)
    elif isinstance(punch_out_val, (int, float)):
        punch_out_ms = int(punch_out_val // 1000 * 1000)

    history_list = []
    for h in doc.get("history") or []:
        h_at = h.get("at")
        h_at_ms = datetime_to_ms(h_at) if isinstance(h_at, datetime) else h_at
        raw_changes = h.get("changes") or {}
        api_changes = {}
        for k, v in raw_changes.items():
            if isinstance(v, dict):
                v_from = v.get("from")
                v_to = v.get("to")
                if k in ("punch_in", "punch_out"):
                    if isinstance(v_from, datetime):
                        v_from = datetime_to_ms(v_from)
                    if isinstance(v_to, datetime):
                        v_to = datetime_to_ms(v_to)
                api_changes[k] = {"from": v_from, "to": v_to}
            else:
                api_changes[k] = v

        history_list.append({
            "at": h_at_ms,
            "by": h.get("by", ""),
            "reason": h.get("reason", ""),
            "changes": api_changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": punch_in_ms,
        "punch_out": punch_out_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_list,
    }


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except (ConnectionFailure, ServerSelectionTimeoutError, Exception):
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")


@app.post("/employees", status_code=201, tags=["employees"], response_model=Employee)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return doc_to_employee(doc)


@app.get("/employees", tags=["employees"], response_model=EmployeePage)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    items = [doc_to_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post(
    "/attendance/punch-in",
    status_code=201,
    tags=["attendance"],
    response_model=AttendanceRecord,
)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_dt = ms_to_datetime(body.punched_at)
    else:
        punch_dt = datetime.now(timezone.utc).replace(microsecond=0)

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    date_str, _, _ = get_shift_schedule(punch_dt, shift_start, shift_end)
    late_mins = compute_late_minutes(punch_dt, shift_start, shift_end)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status.value if isinstance(body.status, Enum) else body.status,
        "punch_in": punch_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return doc_to_attendance(doc)


@app.post(
    "/attendance/punch-out",
    status_code=200,
    tags=["attendance"],
    response_model=AttendanceRecord,
)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = ms_to_datetime(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the employee's most recent record whose punch_in is at or before punch_out_dt
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(
            status_code=404, detail="No matching attendance record found for punch-out"
        )

    # Check if already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409, detail="Attendance record is already punched out"
        )

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Check punch_out > punch_in and <= 24 hours
    delta_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if delta_sec <= 0:
        raise HTTPException(
            status_code=422, detail="punched_at must be strictly after punch_in"
        )
    if delta_sec > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_mins = compute_overtime(punch_out_dt, shift_start, shift_end, record["date"])
    half_day = compute_half_day(work_hours)

    # Atomic update ensuring punch_out is still None (concurrency safe)
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        # Concurrent request already closed this record
        raise HTTPException(
            status_code=409, detail="Attendance record was already punched out"
        )

    return doc_to_attendance(updated)


@app.get(
    "/attendance",
    tags=["attendance"],
    response_model=AttendancePage,
)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="date_from must be a valid date in YYYY-MM-DD format")
    if date_to:
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="date_to must be a valid date in YYYY-MM-DD format")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status.value if isinstance(status, Enum) else status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [doc_to_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch(
    "/attendance/{emp_code}/{date}",
    status_code=200,
    tags=["attendance"],
    response_model=AttendanceRecord,
)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    # 1. Validate date parameter
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="date parameter must be in YYYY-MM-DD format")

    # 2. Check employee existence
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    # 3. Retry loop for concurrency-safe atomic update
    max_retries = 3
    for attempt in range(max_retries):
        record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not record:
            raise HTTPException(status_code=404, detail="Attendance record not found")

        old_status = record.get("status")
        old_punch_in = record.get("punch_in")
        old_punch_out = record.get("punch_out")
        old_work_hours = record.get("work_hours")
        old_late_minutes = record.get("late_minutes", 0)
        old_overtime_minutes = record.get("overtime_minutes", 0)
        old_half_day = record.get("half_day", False)
        old_history = record.get("history") or []

        # Target status
        new_status = body.status.value if body.status is not None else old_status

        # Rule A: ABSENT or LEAVE clears both punch times
        if new_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(
                    status_code=422,
                    detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
                )
            new_punch_in = None
            new_punch_out = None
            new_late_minutes = 0
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False
        else:
            # Presence status: PRESENT, WFH, ON_DUTY
            if body.punch_in is not None:
                new_punch_in = ms_to_datetime(body.punch_in)
            else:
                new_punch_in = old_punch_in

            # Rule B: Presence status requires punch_in
            if new_punch_in is None:
                raise HTTPException(
                    status_code=422,
                    detail="A presence status requires punch_in",
                )

            # Rule C: punch_in must stay on the record's attendance date (R1)
            att_date, _, _ = get_shift_schedule(new_punch_in, shift_start, shift_end)
            if att_date != date:
                raise HTTPException(
                    status_code=422,
                    detail=f"punch_in belongs to attendance date {att_date}, but record date is {date}",
                )

            # punch_out
            if body.punch_out is not None:
                new_punch_out = ms_to_datetime(body.punch_out)
            else:
                new_punch_out = old_punch_out

            # Rule D: punch_out must be after punch_in and within 24 hours
            if new_punch_out is not None:
                p_in_tz = new_punch_in if new_punch_in.tzinfo else new_punch_in.replace(tzinfo=timezone.utc)
                p_out_tz = new_punch_out if new_punch_out.tzinfo else new_punch_out.replace(tzinfo=timezone.utc)
                delta_sec = (p_out_tz - p_in_tz).total_seconds()
                if delta_sec <= 0:
                    raise HTTPException(
                        status_code=422,
                        detail="punch_out must be strictly after punch_in",
                    )
                if delta_sec > 24 * 3600:
                    raise HTTPException(
                        status_code=422,
                        detail="punch_out cannot be more than 24 hours after punch_in",
                    )

            # Recalculate derived fields
            new_late_minutes = compute_late_minutes(new_punch_in, shift_start, shift_end)
            if new_punch_out is not None:
                new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
                new_overtime_minutes = compute_overtime(new_punch_out, shift_start, shift_end, date)
                new_half_day = compute_half_day(new_work_hours)
            else:
                new_work_hours = None
                new_overtime_minutes = 0
                new_half_day = False

        # Build changes dict
        changes = {}

        if old_status != new_status:
            changes["status"] = {"from": old_status, "to": new_status}

        old_in_ms = datetime_to_ms(old_punch_in) if isinstance(old_punch_in, datetime) else None
        new_in_ms = datetime_to_ms(new_punch_in) if isinstance(new_punch_in, datetime) else None
        if old_in_ms != new_in_ms:
            changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}

        old_out_ms = datetime_to_ms(old_punch_out) if isinstance(old_punch_out, datetime) else None
        new_out_ms = datetime_to_ms(new_punch_out) if isinstance(new_punch_out, datetime) else None
        if old_out_ms != new_out_ms:
            changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}

        if old_work_hours != new_work_hours:
            changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

        if old_late_minutes != new_late_minutes:
            changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}

        if old_overtime_minutes != new_overtime_minutes:
            changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}

        if old_half_day != new_half_day:
            changes["half_day"] = {"from": old_half_day, "to": new_half_day}

        # Rule E: A request that changes nothing is 422
        if not changes:
            raise HTTPException(status_code=422, detail="Request changes nothing")

        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        history_entry = {
            "at": now_utc,
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }

        filter_doc = {
            "_id": record["_id"],
            "history": {"$size": len(old_history)},
        }
        update_doc = {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        }

        updated = db.attendance_logs.find_one_and_update(
            filter_doc,
            update_doc,
            return_document=ReturnDocument.AFTER,
        )

        if updated is not None:
            return doc_to_attendance(updated)

    raise HTTPException(status_code=409, detail="Concurrent regularization conflict")


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    tags=["analytics"],
    response_model=EmployeeMonthly,
)
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    start_of_month = date(year, m, 1)
    if m == 12:
        end_of_month = date(year, 12, 31)
    else:
        end_of_month = date(year, m + 1, 1) - timedelta(days=1)

    # R7: For an employee who joined mid-month, working days are counted from joined_on
    emp_joined = date.fromisoformat(emp["joined_on"])
    effective_start = max(start_of_month, emp_joined)
    working_days = count_working_days(effective_start, end_of_month)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": f"{month}-01",
                    "$lte": end_of_month.isoformat(),
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "dayOfWeek": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                },
                "is_present_status": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                },
            }
        },
        {
            "$project": {
                "present_credit": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_present_status",
                                {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                            ]
                        },
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                },
                "is_leave": {
                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                },
                "is_late": {
                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                },
                "late_minutes": 1,
                "overtime_minutes": 1,
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_credit"},
                "leave_days": {"$sum": "$is_leave"},
                "late_count": {"$sum": "$is_late"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_results = list(db.attendance_logs.aggregate(pipeline))

    if agg_results:
        res = agg_results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late_minutes = int(res.get("total_late_minutes", 0))
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    present_days = round_half_up(present_days, 2)

    # R8: attendance_pct = present_days / working_days * 100, R8 rounding, or null if working_days is 0
    if working_days > 0:
        pct_raw = (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100)
        attendance_pct = float(pct_raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get(
    "/analytics/departments/summary",
    tags=["analytics"],
    response_model=DepartmentSummary,
)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    year, m = map(int, month.split("-"))
    if m == 12:
        end_of_month = date(year, 12, 31)
    else:
        end_of_month = date(year, m + 1, 1) - timedelta(days=1)

    month_start_str = f"{month}-01"
    month_end_str = end_of_month.isoformat()

    match_emp = {
        "joined_on": {"$lte": month_end_str}
    }
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lte": ["$date", month_end_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "dayOfWeek": {
                                "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                            },
                            "is_present_status": {
                                "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                            },
                        }
                    },
                    {
                        "$project": {
                            "present_credit": {
                                "$cond": [
                                    {
                                        "$and": [
                                            "$is_present_status",
                                            {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                        ]
                                    },
                                    {"$cond": ["$half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                            "has_work_hours": {
                                "$and": [
                                    "$is_present_status",
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            "work_hours": 1,
                            "late_minutes": 1,
                            "is_late": {
                                "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                            },
                            "is_leave": {
                                "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                            },
                            "is_on_duty": {
                                "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": "$present_credit"},
                            "late_count": {"$sum": "$is_late"},
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {"$sum": "$is_leave"},
                            "on_duty_count": {"$sum": "$is_on_duty"},
                            "sum_work_hours": {
                                "$sum": {
                                    "$cond": [
                                        "$has_work_hours",
                                        "$work_hours",
                                        0.0,
                                    ]
                                }
                            },
                            "count_work_hours": {
                                "$sum": {
                                    "$cond": [
                                        "$has_work_hours",
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "logs_summary",
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "metrics": {"$arrayElemAt": ["$logs_summary", 0]},
            }
        },
        {"$sort": {"department": 1}},
    ]

    agg_results = list(db.employees.aggregate(pipeline))

    items = []
    for doc in agg_results:
        dept_name = doc["department"]
        headcount = int(doc["headcount"])
        metrics = doc.get("metrics") or {}

        present_days = float(metrics.get("present_days", 0.0))
        present_days = round_half_up(present_days, 2)

        late_count = int(metrics.get("late_count", 0))
        total_late_minutes = int(metrics.get("total_late_minutes", 0))
        leave_count = int(metrics.get("leave_count", 0))
        on_duty_count = int(metrics.get("on_duty_count", 0))

        count_wh = int(metrics.get("count_work_hours", 0))
        sum_wh = float(metrics.get("sum_work_hours", 0.0))

        if count_wh > 0:
            raw_avg = Decimal(str(sum_wh)) / Decimal(count_wh)
            avg_work_hours = float(raw_avg.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        else:
            avg_work_hours = None

        items.append({
            "department": dept_name,
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {
        "month": month,
        "items": items,
    }


@app.get(
    "/analytics/leaderboard/late",
    tags=["analytics"],
    response_model=Leaderboard,
)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    year, m = map(int, month.split("-"))
    if m == 12:
        end_of_month = date(year, 12, 31)
    else:
        end_of_month = date(year, m + 1, 1) - timedelta(days=1)

    month_start_str = f"{month}-01"
    month_end_str = end_of_month.isoformat()

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start_str, "$lte": month_end_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {
            "$unwind": "$emp"
        },
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    results = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": results,
    }


@app.get(
    "/analytics/departments/{department}/trend",
    tags=["analytics"],
    response_model=Trend,
)
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    # 1. Verify department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    # 2. Validate date formats
    try:
        from_dt = date.fromisoformat(from_date)
        to_dt = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    if to_dt < from_dt:
        raise HTTPException(status_code=422, detail="to date must be >= from date")

    if (to_dt - from_dt).days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    # 3. Retrieve department employees
    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    emp_codes = [e["emp_code"] for e in dept_emps]
    joined_dates = sorted([e["joined_on"] for e in dept_emps if "joined_on" in e])

    # Date boundaries for $densify in UTC
    from_dt_utc = datetime.combine(from_dt, time.min, tzinfo=timezone.utc)
    to_dt_plus_1_utc = datetime.combine(to_dt + timedelta(days=1), time.min, tzinfo=timezone.utc)

    # 4. MongoDB Aggregation pipeline on attendance_logs
    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        # Anchor stage ensuring $densify has at least one document spanning bounds
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": {"$literal": from_date},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        # Deduplicate the anchor date
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"},
            }
        },
        {
            "$project": {
                "date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Gap-filling stage generating exactly one row per calendar day
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt_utc, to_dt_plus_1_utc],
                },
            }
        },
        {
            "$project": {
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dayOfWeek": {"$dayOfWeek": "$date"},
            }
        },
        {"$sort": {"date_str": 1}},
    ]

    agg_results = list(db.attendance_logs.aggregate(pipeline))

    # 5. Process daily metrics, attendance rate, and 7-day moving average
    items = []
    rates_window = []  # list of tuples (date_obj, attendance_rate_or_None)

    for doc in agg_results:
        d_str = doc["date_str"]
        cur_d = date.fromisoformat(d_str)

        # is_working_day: Mon-Fri (dayOfWeek 2=Mon, 6=Fri)
        dow = doc.get("dayOfWeek")
        is_working_day = dow in [2, 3, 4, 5, 6]

        # headcount: employees with joined_on <= current day (R9)
        headcount = sum(1 for jd in joined_dates if jd <= d_str)

        # present_count & late_count
        present_count = float(round_half_up(doc.get("present_count", 0.0), 2))
        late_count = int(doc.get("late_count", 0))

        # attendance_rate: present_count / headcount, 4 decimals half-up, only on working days with headcount > 0
        if is_working_day and headcount > 0:
            raw_rate = Decimal(str(present_count)) / Decimal(headcount)
            attendance_rate = float(raw_rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        else:
            attendance_rate = None

        # moving_avg_7d: mean of non-null attendance_rate among current row and up to 6 preceding rows inside range
        rates_window.append((cur_d, attendance_rate))
        window_start = cur_d - timedelta(days=6)
        valid_in_window = [r for (dt, r) in rates_window if dt >= window_start and dt >= from_dt and r is not None]

        if valid_in_window:
            raw_avg = Decimal(str(sum(valid_in_window))) / Decimal(len(valid_in_window))
            moving_avg_7d = float(raw_avg.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        else:
            moving_avg_7d = None

        items.append({
            "date": d_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })

    return {
        "department": department,
        "items": items,
    }


@app.get(
    "/admin/explain/{endpoint}",
    tags=["admin"],
    response_model=ExplainResponse,
)
def explain_endpoint(
    endpoint: ExplainEndpointEnum,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[AttendanceStatus] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(default=1, ge=1),
    page_size: Optional[int] = Query(default=20, ge=1, le=100),
):
    if endpoint == ExplainEndpointEnum.attendance_list:
        collection_name = "attendance_logs"
        if date_from and date_to:
            try:
                df = date.fromisoformat(date_from)
                dt = date.fromisoformat(date_to)
                if dt < df:
                    raise HTTPException(status_code=422, detail="date_to must be >= date_from")
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date format")

        filter_query = {}
        if emp_code:
            filter_query["emp_code"] = emp_code
        if status:
            filter_query["status"] = status.value if hasattr(status, "value") else status
        if date_from and date_to:
            filter_query["date"] = {"$gte": date_from, "$lte": date_to}
        elif date_from:
            filter_query["date"] = {"$gte": date_from}
        elif date_to:
            filter_query["date"] = {"$lte": date_to}

        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(filter_query)
            .sort([("date", -1), ("emp_code", 1)])
            .skip(skip)
            .limit(page_size)
        )
        raw_explain = cursor.explain()

    elif endpoint == ExplainEndpointEnum.employee_monthly:
        collection_name = "attendance_logs"
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        year, m = map(int, month.split("-"))
        if m == 12:
            end_of_month = date(year, 12, 31)
        else:
            end_of_month = date(year, m + 1, 1) - timedelta(days=1)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": f"{month}-01",
                        "$lte": end_of_month.isoformat(),
                    },
                }
            },
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "dayOfWeek": {
                        "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                    },
                    "is_present_status": {
                        "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                    },
                }
            },
            {
                "$project": {
                    "present_credit": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_present_status",
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    },
                    "is_leave": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    },
                    "is_late": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    },
                    "late_minutes": 1,
                    "overtime_minutes": 1,
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {"$sum": "$present_credit"},
                    "leave_days": {"$sum": "$is_leave"},
                    "late_count": {"$sum": "$is_late"},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        cmd = SON([
            ("explain", SON([("aggregate", "attendance_logs"), ("pipeline", pipeline), ("cursor", {})])),
            ("verbosity", "executionStats"),
        ])
        raw_explain = db.command(cmd)

    elif endpoint == ExplainEndpointEnum.department_summary:
        collection_name = "employees"
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        year, m = map(int, month.split("-"))
        if m == 12:
            end_of_month = date(year, 12, 31)
        else:
            end_of_month = date(year, m + 1, 1) - timedelta(days=1)

        month_start_str = f"{month}-01"
        month_end_str = end_of_month.isoformat()

        match_emp = {
            "joined_on": {"$lte": month_end_str}
        }
        if department:
            match_emp["department"] = department

        pipeline = [
            {"$match": match_emp},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "emp_codes": {"$push": "$emp_code"},
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"codes": "$emp_codes"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$in": ["$emp_code", "$$codes"]},
                                        {"$gte": ["$date", month_start_str]},
                                        {"$lte": ["$date", month_end_str]},
                                    ]
                                }
                            }
                        },
                        {
                            "$project": {
                                "status": 1,
                                "work_hours": 1,
                                "half_day": {"$ifNull": ["$half_day", False]},
                                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                                "dayOfWeek": {
                                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                                },
                                "is_present_status": {
                                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                                },
                            }
                        },
                        {
                            "$project": {
                                "present_credit": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                "$is_present_status",
                                                {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                                "has_work_hours": {
                                    "$and": [
                                        "$is_present_status",
                                        {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                    ]
                                },
                                "work_hours": 1,
                                "late_minutes": 1,
                                "is_late": {
                                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                                },
                                "is_leave": {
                                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                                },
                                "is_on_duty": {
                                    "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                                },
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "present_days": {"$sum": "$present_credit"},
                                "late_count": {"$sum": "$is_late"},
                                "total_late_minutes": {"$sum": "$late_minutes"},
                                "leave_count": {"$sum": "$is_leave"},
                                "on_duty_count": {"$sum": "$is_on_duty"},
                                "sum_work_hours": {
                                    "$sum": {
                                        "$cond": [
                                            "$has_work_hours",
                                            "$work_hours",
                                            0.0,
                                        ]
                                    }
                                },
                                "count_work_hours": {
                                    "$sum": {
                                        "$cond": [
                                            "$has_work_hours",
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "logs_summary",
                }
            },
            {
                "$project": {
                    "department": "$_id",
                    "headcount": 1,
                    "metrics": {"$arrayElemAt": ["$logs_summary", 0]},
                }
            },
            {"$sort": {"department": 1}},
        ]
        cmd = SON([
            ("explain", SON([("aggregate", "employees"), ("pipeline", pipeline), ("cursor", {})])),
            ("verbosity", "executionStats"),
        ])
        raw_explain = db.command(cmd)

    elif endpoint == ExplainEndpointEnum.late_leaderboard:
        collection_name = "attendance_logs"
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        year, m = map(int, month.split("-"))
        if m == 12:
            end_of_month = date(year, 12, 31)
        else:
            end_of_month = date(year, m + 1, 1) - timedelta(days=1)

        month_start_str = f"{month}-01"
        month_end_str = end_of_month.isoformat()

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start_str, "$lte": month_end_str},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {
                "$unwind": "$emp"
            },
        ]

        if department:
            pipeline.append({"$match": {"emp.department": department}})

        actual_limit = limit if limit is not None else 10
        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {
                            "$rank": {}
                        }
                    }
                }
            },
            {
                "$match": {
                    "rank": {"$lte": actual_limit}
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ])
        cmd = SON([
            ("explain", SON([("aggregate", "attendance_logs"), ("pipeline", pipeline), ("cursor", {})])),
            ("verbosity", "executionStats"),
        ])
        raw_explain = db.command(cmd)

    elif endpoint == ExplainEndpointEnum.department_trend:
        collection_name = "attendance_logs"
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="from and to are required for department_trend")

        try:
            from_dt = date.fromisoformat(from_date)
            to_dt = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

        if to_dt < from_dt:
            raise HTTPException(status_code=422, detail="to date must be >= from date")

        if (to_dt - from_dt).days > 92:
            raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

        dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
        emp_codes = [e["emp_code"] for e in dept_emps]

        from_dt_utc = datetime.combine(from_dt, time.min, tzinfo=timezone.utc)
        to_dt_plus_1_utc = datetime.combine(to_dt + timedelta(days=1), time.min, tzinfo=timezone.utc)

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$unionWith": {
                    "coll": "employees",
                    "pipeline": [
                        {"$match": {"department": department}},
                        {"$limit": 1},
                        {
                            "$project": {
                                "_id": {"$literal": from_date},
                                "present_count": {"$literal": 0.0},
                                "late_count": {"$literal": 0},
                            }
                        },
                    ],
                }
            },
            {
                "$group": {
                    "_id": "$_id",
                    "present_count": {"$max": "$present_count"},
                    "late_count": {"$max": "$late_count"},
                }
            },
            {
                "$project": {
                    "date": {"$dateFromString": {"dateString": "$_id"}},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$densify": {
                    "field": "date",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [from_dt_utc, to_dt_plus_1_utc],
                    },
                }
            },
            {
                "$project": {
                    "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                    "dayOfWeek": {"$dayOfWeek": "$date"},
                }
            },
            {"$sort": {"date_str": 1}},
        ]
        cmd = SON([
            ("explain", SON([("aggregate", "attendance_logs"), ("pipeline", pipeline), ("cursor", {})])),
            ("verbosity", "executionStats"),
        ])
        raw_explain = db.command(cmd)

    else:
        raise HTTPException(status_code=422, detail="Unsupported endpoint")

    clean_explain = json.loads(json_util.dumps(raw_explain))

    return {
        "endpoint": endpoint.value if hasattr(endpoint, "value") else str(endpoint),
        "collection": collection_name,
        "explain": clean_explain,
    }

