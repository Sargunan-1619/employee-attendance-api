"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml.  Stored data: DATA_MODEL.md.  Everything lives in this one file.
"""
import calendar
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

load_dotenv()  # override=False: variables already set in the environment take priority

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo hands back UTC-aware datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MS_MIN = 100_000_000_000  # below this a value looks like seconds (openapi EpochMillis)
MS_MAX = 4_102_444_800_000
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_SHIFT_SECONDS = 24 * 3600  # punch-out / correction window

# [0-9] rather than \d: the pydantic regex engine treats \d as any Unicode digit.
DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
EMP_CODE_RE = r"^EMP[0-9]{4,6}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

# Strict: floats, strings and booleans are rejected, as are seconds-looking and out-of-range values.
EpochMillis = Annotated[int, Field(strict=True, ge=MS_MIN, le=MS_MAX)]
StatusName = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PageQ = Annotated[int, Query(ge=1, le=2**31 - 1)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]

# --------------------------------------------------------------------------- #
# Indexes (created at startup; create_index is idempotent)
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        ([("emp_code", 1)], {"name": "emp_code_unique", "unique": True}),  # identity + race-safe create
        ([("department", 1), ("emp_code", 1)], {"name": "department_emp_code"}),  # filtered list, sorted
        ([("department", 1), ("joined_on", 1)], {"name": "department_joined_on"}),  # headcount per department
        ([("joined_on", 1)], {"name": "joined_on"}),  # headcount with no department filter
    ],
    "attendance_logs": [
        ([("emp_code", 1), ("date", 1)], {"name": "emp_code_date_unique", "unique": True}),  # one record per day
        ([("emp_code", 1), ("punch_in", -1)], {"name": "emp_code_punch_in"}),  # punch-out lookup
        ([("date", -1), ("emp_code", 1)], {"name": "date_emp_code"}),  # list order + month range scans
        ([("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp_code"}),  # status filter
    ],
}


def ensure_indexes() -> None:
    for coll, specs in INDEXES.items():
        for keys, opts in specs:
            try:
                db[coll].create_index(keys, **opts)
            except ServerSelectionTimeoutError:
                log.error("MongoDB is unreachable; skipping index creation")
                return
            except PyMongoError:
                # Keep the service up (and /health honest) even if one index cannot be built.
                log.exception("could not create index %s on %s", opts.get("name"), coll)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers: validation, time, serialization
# --------------------------------------------------------------------------- #
def invalid(loc: list, msg: str):
    """Raise a 422 with FastAPI's standard validation body."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def parse_date(value: str, loc: list) -> date_cls:
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        invalid(loc, "not a valid calendar date")


def parse_month(value: str, loc: list) -> tuple[str, str]:
    """'YYYY-MM' -> (first day, last day) as 'YYYY-MM-DD' strings."""
    year, month = int(value[:4]), int(value[5:7])
    if year < 1:
        invalid(loc, "not a valid month")
    last = calendar.monthrange(year, month)[1]
    return f"{value}-01", f"{value}-{last:02d}"


def truncate_ms(ms: int) -> int:
    """R1: instants are truncated to whole seconds before any calculation."""
    return ms - ms % 1000


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def half_up(value: Decimal, places: int) -> Decimal:
    """R4/R8: round half up (Python's round() and MongoDB's $round are not half-up)."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def employee_out(doc: dict) -> dict:
    created = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(created) if created is not None else None,
    }


# --------------------------------------------------------------------------- #
# Business rules R1-R5 (all instants are tz-aware UTC datetimes truncated to whole seconds)
# --------------------------------------------------------------------------- #
def minutes_of(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return minutes_of(shift_end) <= minutes_of(shift_start)


def at_ist(day: date_cls, hhmm: str) -> datetime:
    return datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[3:5]), tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in belongs to the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
    if is_overnight(shift_start, shift_end) and seconds_of_day < minutes_of(shift_end) * 60:
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, day: date_cls, shift_start: str) -> int:
    """R2: late only when strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    seconds = int((punch_in - at_ist(day, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, day: date_cls, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift_end (next day for overnight shifts), counted only when >= 30."""
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    seconds = int((punch_out - at_ist(end_day, shift_end)).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600, half-up to 2 decimals."""
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up(Decimal(seconds) / Decimal(3600), 2)


def derived_fields(
    status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: date_cls, shift_start: str,
    shift_end: str,
) -> dict:
    """The four stored derived values for a record (DATA_MODEL.md, 'Stored derived values')."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, day, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": float(hours),
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out, day, shift_start, shift_end),
        "half_day": hours < HALF_DAY_LIMIT,
    }


def history_value(key: str, value: Any) -> Any:
    return to_ms(value) if key in ("punch_in", "punch_out") and value is not None else value


def record_out(doc: dict) -> dict:
    """Stored attendance document -> API shape. Missing legacy fields get their documented defaults."""
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    history = [
        {
            "at": to_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": {
                k: {"from": history_value(k, v.get("from")), "to": history_value(k, v.get("to"))}
                for k, v in h.get("changes", {}).items()
            },
        }
        for h in doc.get("history") or []
    ]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(pin) if pin is not None else None,
        "punch_out": to_ms(pout) if pout is not None else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=EMP_CODE_RE)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_RE)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        try:
            date_cls.fromisoformat(v)
        except ValueError:
            raise ValueError("not a valid calendar date")
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


# Defaults are not validated by pydantic, so omitted -> None while an explicit null is still rejected.
class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = None


class RegularizeIn(BaseModel):
    # Derived fields are not declared here, so a client that sends them is simply ignored.
    status: StatusName = None
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created = from_ms(now_ms())
    doc["created_at"] = created
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code decides, not a prior read
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-in, punch-out, list
# --------------------------------------------------------------------------- #
def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ms = truncate_ms(body.punched_at if body.punched_at is not None else now_ms())
    punched = from_ms(ms)
    day = attendance_date(punched, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index arbitrates concurrent requests
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    punched = from_ms(truncate_ms(body.punched_at if body.punched_at is not None else now_ms()))
    # Most recent record whose punch_in is at or before punched_at (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if punched <= pin:
        invalid(["body", "punched_at"], "punched_at must be after punch_in")
    if (punched - pin).total_seconds() > MAX_SHIFT_SECONDS:
        invalid(["body", "punched_at"], "punched_at is more than 24 hours after punch_in")
    fields = derived_fields(
        rec["status"], pin, punched, date_cls.fromisoformat(rec["date"]), emp["shift_start"], emp["shift_end"]
    )
    # Conditional on the record still being open: of simultaneous punch-outs exactly one matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": punched, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_filter(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    lo = parse_date(date_from, ["query", "date_from"]) if date_from else None
    hi = parse_date(date_to, ["query", "date_to"]) if date_to else None
    if lo and hi and lo > hi:
        invalid(["query", "date_from"], "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if lo or hi:
        q["date"] = {}
        if lo:
            q["date"]["$gte"] = date_from
        if hi:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[StatusName] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Manual correction (regularization) + audit trail
# --------------------------------------------------------------------------- #
CORRECTED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def stored_value(rec: dict, key: str) -> Any:
    """A stored field with the documented defaults for legacy documents that omit it."""
    default = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}.get(key)
    value = rec.get(key)
    return default if value is None and key in ("late_minutes", "overtime_minutes", "half_day") else value


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, body: RegularizeIn, date: str = Path(pattern=DATE_RE)):
    parse_date(date, ["path", "date"])
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for this date")

    status = body.status or rec["status"]
    punch_in, punch_out = rec.get("punch_in"), rec.get("punch_out")
    if status in PRESENCE:
        if body.punch_in is not None:
            punch_in = from_ms(truncate_ms(body.punch_in))
            if attendance_date(punch_in, emp["shift_start"], emp["shift_end"]).isoformat() != date:
                invalid(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if body.punch_out is not None:
            punch_out = from_ms(truncate_ms(body.punch_out))
        if punch_in is None:
            invalid(["body", "punch_in"], f"status {status} requires a punch_in")
        if (body.punch_in is not None or body.punch_out is not None) and punch_out is not None:
            if punch_out <= punch_in:
                invalid(["body", "punch_out"], "punch_out must be after punch_in")
            if (punch_out - punch_in).total_seconds() > MAX_SHIFT_SECONDS:
                invalid(["body", "punch_out"], "punch_out must be within 24 hours of punch_in")
    else:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            invalid(["body"], f"status {status} cannot carry punch times")
        punch_in = punch_out = None

    new = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        **derived_fields(status, punch_in, punch_out, date_cls.fromisoformat(date), emp["shift_start"], emp["shift_end"]),
    }
    changes = {}
    for key in CORRECTED_FIELDS:
        old = stored_value(rec, key)
        if old != new[key]:
            changes[key] = {"from": old, "to": new[key]}
    if not changes:
        invalid(["body"], "request changes nothing")

    entry = {"at": from_ms(now_ms()), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: apply only if the record is still exactly what this change was computed from.
    # A concurrent correction that got there first makes this one match nothing -> 409, no history is lost.
    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
    }
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics (MongoDB aggregation pipelines; they read the stored derived values)
# --------------------------------------------------------------------------- #
MONGO_NUMERIC = ["double", "int", "long", "decimal"]


def weekend_dates(first_day: str, last_day: str) -> list:
    """The Saturdays and Sundays of a date range, as 'YYYY-MM-DD' strings (at most ~10 for a month)."""
    first, last = date_cls.fromisoformat(first_day), date_cls.fromisoformat(last_day)
    days = (first + timedelta(days=i) for i in range((last - first).days + 1))
    return [d.isoformat() for d in days if d.weekday() >= 5]


def weekday(date_expr: str, weekends: list) -> dict:
    """True for Mon-Fri. Testing against the month's few weekend dates is much cheaper than parsing every log date."""
    return {"$not": [{"$in": [date_expr, weekends]}]}


def presence_day_weight(prefix: str) -> dict:
    """1 for a present-status record, 0.5 for a present half day (R5/R6), else 0. `prefix` is '$' or '$$l.'."""
    half = {"$eq": [{"$ifNull": [f"{prefix}half_day", False]}, True]}
    return {"$cond": [{"$in": [f"{prefix}status", PRESENCE]}, {"$cond": [half, 0.5, 1]}, 0]}


def late_value(prefix: str) -> dict:
    return {"$ifNull": [f"{prefix}late_minutes", 0]}


def half_up_expr(value: Any, places: int) -> dict:
    """Exact half-up rounding of a Decimal128 expression ($round would round halves to even)."""
    scale = 10**places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [value, scale]}, Decimal128("0.5")]}}, scale]}


def working_days(joined_on: str, first_day: str, last_day: str) -> int:
    """R7: Mon-Fri days of the month, counted from joined_on for a mid-month joiner."""
    first = max(date_cls.fromisoformat(first_day), date_cls.fromisoformat(joined_on))
    last = date_cls.fromisoformat(last_day)
    if first > last:
        return 0
    return sum(1 for i in range((last - first).days + 1) if (first + timedelta(days=i)).weekday() < 5)


def monthly_pipeline(emp_code: str, first_day: str, last_day: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [weekday("$date", weekend_dates(first_day, last_day)), presence_day_weight("$"), 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [late_value("$"), 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": late_value("$")},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    first_day, last_day = parse_month(month, ["query", "month"])
    emp = get_employee(emp_code)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first_day, last_day)))
    r = rows[0] if rows else {}
    days = working_days(emp["joined_on"], first_day, last_day)
    present = float(r.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": present,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": float(half_up(Decimal(str(present)) * 100 / days, 2)) if days else None,
    }


def summary_pipeline(first_day: str, last_day: str, department: Optional[str]) -> list:
    """employees (headcount, R9) left-joined to that month's logs. Each employee's logs are totalled inside the
    $lookup (no logs -> empty result, the employee still counts), then the totals are summed per department."""
    match: dict = {"joined_on": {"$lte": last_day}}
    if department is not None:
        match["department"] = department

    present = {"$in": ["$status", PRESENCE]}
    has_hours = {"$in": [{"$type": "$work_hours"}, MONGO_NUMERIC]}
    late = late_value("$")
    weekends = weekend_dates(first_day, last_day)
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": {"$cond": [weekday("$date", weekends), presence_day_weight("$"), 0]}},
                            "hours_sum": {"$sum": {"$cond": [{"$and": [present, has_hours]}, {"$toDecimal": "$work_hours"}, 0]}},
                            "hours_n": {"$sum": {"$cond": [{"$and": [present, has_hours]}, 1, 0]}},
                            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
                            "late_total": {"$sum": {"$cond": [{"$gt": [late, 0]}, late, 0]}},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "totals",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},  # one per employee, whether or not they have logs
                "present_days": {"$sum": {"$sum": "$totals.present_days"}},
                "hours_sum": {"$sum": {"$sum": "$totals.hours_sum"}},
                "hours_n": {"$sum": {"$sum": "$totals.hours_n"}},
                "late_count": {"$sum": {"$sum": "$totals.late_count"}},
                "late_total": {"$sum": {"$sum": "$totals.late_total"}},
                "leave_count": {"$sum": {"$sum": "$totals.leave_count"}},
                "on_duty_count": {"$sum": {"$sum": "$totals.on_duty_count"}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    first_day, last_day = parse_month(month, ["query", "month"])
    items = []
    for r in db.employees.aggregate(summary_pipeline(first_day, last_day, department)):
        total = r["hours_sum"].to_decimal() if isinstance(r["hours_sum"], Decimal128) else Decimal(str(r["hours_sum"]))
        n = int(r["hours_n"])
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": float(r["present_days"]),
                "avg_work_hours": float(half_up(total / n, 2)) if n else None,  # mean over records
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["late_total"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


def leaderboard_pipeline(first_day: str, last_day: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of employees that do not exist are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # ranking happens inside the department
    pipeline += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff all stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
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
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = parse_month(month, ["query", "month"])
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(first_day, last_day, limit, department)))
    return {"month": month, "items": items}


def trend_range(frm: str, to: str) -> tuple[date_cls, date_cls]:
    start, end = parse_date(frm, ["query", "from"]), parse_date(to, ["query", "to"])
    if end < start:
        invalid(["query", "to"], "to must not be before from")
    if (end - start).days + 1 > 92:
        invalid(["query", "to"], "range must not exceed 92 days")
    return start, end


def trend_pipeline(department: str, start: date_cls, end: date_cls) -> list:
    """One row per calendar day. Events (an employee joining, a log) are grouped by day, the gaps are filled with
    $densify, headcount is a running sum of joins and the moving average is a 7-row window."""
    first, last = start.isoformat(), end.isoformat()
    day_start = datetime(start.year, start.month, start.day, tzinfo=UTC)
    day_after_end = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    join_day = {"$cond": [{"$and": [{"$gt": ["$joined_on", first]}, {"$lte": ["$joined_on", last]}]}, "$joined_on", first]}
    log_event = {
        "d": "$$l.date",
        "join": 0,
        "present": presence_day_weight("$$l."),
        "late": {"$cond": [{"$gt": [late_value("$$l."), 0]}, 1, 0]},
    }
    join_event = {"d": join_day, "join": {"$cond": [{"$lte": ["$joined_on", last]}, 1, 0]}, "present": 0, "late": 0}
    rate = {
        "$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_expr({"$divide": [{"$toDecimal": "$present"}, {"$toDecimal": "$headcount"}]}, 4),
            None,
        ]
    }
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        # Every employee contributes one join event (+0 if they join after the range), so the stream is never empty.
        {"$project": {"ev": {"$concatArrays": [[join_event], {"$map": {"input": "$logs", "as": "l", "in": log_event}}]}}},
        {"$unwind": "$ev"},
        {"$replaceRoot": {"newRoot": "$ev"}},
        {
            "$group": {
                "_id": "$d",
                "join": {"$sum": "$join"},
                "present": {"$sum": "$present"},
                "late": {"$sum": "$late"},
            }
        },
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [day_start, day_after_end]}}},
        {
            "$set": {
                "join": {"$ifNull": ["$join", 0]},
                "present": {"$ifNull": ["$present", 0]},
                "late": {"$ifNull": ["$late", 0]},
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {"headcount": {"$sum": "$join", "window": {"documents": ["unbounded", "current"]}}},
            }
        },
        {"$set": {"is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]}}},
        {"$set": {"rate": rate}},
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, "current"]}}},
            }
        },
        {"$sort": {"d": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "is_working_day": 1,
                "headcount": 1,
                "present_count": "$present",
                "late_count": "$late",
                "attendance_rate": {"$toDouble": "$rate"},
                "moving_avg_7d": {"$toDouble": {"$cond": [{"$eq": ["$ma", None]}, None, half_up_expr("$ma", 4)]}},
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    frm: str = Query(alias="from", pattern=DATE_RE),
    to: str = Query(pattern=DATE_RE),
):
    start, end = trend_range(frm, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": list(db.employees.aggregate(trend_pipeline(department, start, end)))}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def need(name: str, value: Any) -> Any:
    if value is None:
        invalid(["query", name], "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[StatusName] = None,
    frm: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    to: Optional[str] = Query(None, pattern=DATE_RE),
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    """Explains the main query of the named endpoint, built by the same functions that endpoint uses."""
    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        command: dict = {"find": "attendance_logs", "filter": q, "sort": dict(ATTENDANCE_SORT), "limit": page_size}
        if page > 1:
            command["skip"] = (page - 1) * page_size
        collection = "attendance_logs"
    else:
        if endpoint == "department_trend":
            start, end = trend_range(need("from", frm), need("to", to))
            pipeline, collection = trend_pipeline(need("department", department), start, end), "employees"
        else:
            first_day, last_day = parse_month(need("month", month), ["query", "month"])
            if endpoint == "employee_monthly":
                pipeline = monthly_pipeline(need("emp_code", emp_code), first_day, last_day)
                collection = "attendance_logs"
            elif endpoint == "department_summary":
                pipeline, collection = summary_pipeline(first_day, last_day, department), "employees"
            else:
                pipeline = leaderboard_pipeline(first_day, last_day, limit, department)
                collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }
