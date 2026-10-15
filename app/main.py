"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real environment
      variables always win because load_dotenv() does not override them)

Layout of this file (everything lives here, as the assignment requires):
  1. configuration and database handle
  2. pure helpers: time zone, business rules R1-R5, rounding (no database access)
  3. request models (pydantic)
  4. startup: indexes
  5. validation + serialisation helpers
  6. endpoints: system, employees, attendance, regularization
  7. analytics: aggregation pipeline builders + endpoints
  8. admin: explain endpoint
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta, timezone
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Annotated, Any, Literal, NoReturn, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration and database handle
# --------------------------------------------------------------------------- #
load_dotenv()  # never overrides variables that are already set in the environment
log = logging.getLogger("attendance")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones,
# so we never mix naive and aware values. The short server-selection timeout keeps
# /health fast when MongoDB is down.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# 2. Pure helpers (no database access, easy to unit-test)
# --------------------------------------------------------------------------- #
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6: these count as present
ABSENCE = ("ABSENT", "LEAVE")  # no punch times
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW_HOURS = 4.50  # R5
MIN_EPOCH_MS = 100_000_000_000  # below this the number looks like seconds
MAX_EPOCH_MS = 4_102_444_800_000

_ROUND_CTX = Context(prec=60, rounding=ROUND_HALF_UP)


def round_half_up(value: Any, places: int = 2) -> float:
    """R8 rounding: half-up, not banker's rounding and not binary-float rounding.

    The value is first cleaned to 9 decimals so float noise such as 8.124999999999998
    (which really is 8.125) rounds the way a human expects.
    """
    d = value if isinstance(value, Decimal) else Decimal(repr(float(value)))
    d = _ROUND_CTX.quantize(d, Decimal("1e-9"))
    return float(_ROUND_CTX.quantize(d, Decimal(1).scaleb(-places)))


def as_utc(dt: datetime) -> datetime:
    """Normalise any datetime (naive values are assumed to be UTC) to aware UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    """Datetime -> integer epoch milliseconds (exact integer arithmetic, no floats)."""
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    """Current time truncated to whole seconds (R1)."""
    return datetime.now(UTC).replace(microsecond=0)


def _hm(hhmm: str) -> tuple[int, int]:
    hour, minute = hhmm.split(":")
    return int(hour), int(minute)


def _hm_seconds(hhmm: str) -> int:
    hour, minute = _hm(hhmm)
    return hour * 3600 + minute * 60


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return _hm_seconds(shift_end) <= _hm_seconds(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date a punch-in belongs to.

    For an overnight shift, a punch-in earlier than shift_end (IST) belongs to the shift
    that started the previous day.
    """
    ist = punch_in.astimezone(IST)
    day = ist.date()
    seconds_into_day = ist.hour * 3600 + ist.minute * 60 + ist.second
    if is_overnight(shift_start, shift_end) and seconds_into_day < _hm_seconds(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_window(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end of the shift that belongs to attendance date `date_str` (aware, IST).

    The end of an overnight shift falls on the next calendar day.
    """
    day = date_cls.fromisoformat(date_str)
    start = datetime.combine(day, time_cls(*_hm(shift_start)), tzinfo=IST)
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    end = datetime.combine(end_day, time_cls(*_hm(shift_end)), tzinfo=IST)
    return start, end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift_start; then whole minutes since shift_start."""
    start, _ = shift_window(date_str, shift_start, shift_end)
    seconds = (punch_in - start) // timedelta(seconds=1)
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (out - in) seconds / 3600, rounded to 2 decimals half-up."""
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift_end, counted only when that is at least 30."""
    _, end = shift_window(date_str, shift_start, shift_end)
    seconds = (punch_out - end) // timedelta(seconds=1)
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    date_str: str,
    shift_start: str,
    shift_end: str,
) -> dict:
    """The four derived fields (late_minutes, work_hours, overtime_minutes, half_day).

    Single source of truth, used by punch-in, punch-out and the regularization endpoint.
    """
    derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in PRESENCE and punch_in is not None:
        derived["late_minutes"] = compute_late_minutes(punch_in, date_str, shift_start, shift_end)
        if punch_out is not None:
            work_hours = compute_work_hours(punch_in, punch_out)
            derived["work_hours"] = work_hours
            derived["overtime_minutes"] = compute_overtime(punch_out, date_str, shift_start, shift_end)
            derived["half_day"] = work_hours < HALF_DAY_BELOW_HOURS  # R5, on the rounded value
    return derived


def count_weekdays(start: date_cls, end: date_cls) -> int:
    """Number of Monday-Friday days in [start, end] inclusive (R7, no holiday calendar)."""
    if end < start:
        return 0
    total = (end - start).days + 1
    full_weeks, remainder = divmod(total, 7)
    count = full_weeks * 5
    for offset in range(remainder):
        if (start + timedelta(days=offset)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# 3. Request models
# --------------------------------------------------------------------------- #
# strict=True: floats ("1.5e12"), strings ("1783312500000") and booleans are rejected with 422.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]

HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


def _check_iso_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ValueError("must be a date formatted YYYY-MM-DD")
    try:
        date_cls.fromisoformat(value)
    except ValueError:
        raise ValueError("not a real calendar date") from None
    return value


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("name", "department")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, value: str) -> str:
        return _check_iso_date(value)

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Derived fields (work_hours, late_minutes, ...) are not declared, so pydantic ignores them.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


PageQ = Annotated[int, Query(ge=1, le=1_000_000_000)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]
MonthQ = Annotated[str, Query(pattern=MONTH_PATTERN)]
LimitQ = Annotated[int, Query(ge=1, le=50)]


# --------------------------------------------------------------------------- #
# 4. Startup: indexes (idempotent) and app object
# --------------------------------------------------------------------------- #
# (collection, key pattern, options, why)
INDEXES = [
    ("employees", [("emp_code", ASCENDING)], {"unique": True},
     "emp_code is the business key: uniqueness is enforced by the database, not by check-then-insert"),
    ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], {},
     "employee list by department sorted by emp_code; department -> employee codes for the trend"),
    ("employees", [("joined_on", ASCENDING)], {},
     "headcount (R9): employees who joined on or before a date"),
    ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True},
     "natural key (emp_code, date): race-safe punch-in, PATCH, punch-out, employee monthly, $lookup joins"),
    ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {},
     "GET /attendance default sort (date desc, emp_code asc) and date-range filters"),
    ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {},
     "GET /attendance with a status filter, already in sort order"),
    ("attendance_logs", [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], {},
     "late leaderboard: covered index (month range + late_minutes > 0 + emp_code)"),
]


def ensure_indexes() -> None:
    """Create every index; create_index is a no-op when the same index already exists."""
    for collection, keys, options, _why in INDEXES:
        try:
            db[collection].create_index(keys, **options)
        except PyMongoError:
            # Never crash the app here: /health reports 503 while MongoDB is unreachable.
            log.exception("could not create index %s on %s", keys, collection)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_request: Request, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 5. Validation and serialisation helpers
# --------------------------------------------------------------------------- #
def unprocessable(loc: list, msg: str) -> NoReturn:
    """Raise a 422 with the same body shape FastAPI uses for validation errors."""
    raise HTTPException(status_code=422, detail=[{"loc": loc, "msg": msg, "type": "value_error"}])


def missing_param(name: str) -> NoReturn:
    raise HTTPException(
        status_code=422,
        detail=[{"loc": ["query", name], "msg": "Field required", "type": "missing"}],
    )


def parse_date(value: str, loc: list) -> date_cls:
    try:
        _check_iso_date(value)
        return date_cls.fromisoformat(value)
    except ValueError as exc:
        unprocessable(loc, str(exc))


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        unprocessable(["query", "month"], "not a real calendar month")
    last_day = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last_day:02d}"


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]) if doc.get("created_at") else None,
    }


def _history_value(field: str, value: Any) -> Any:
    # from/to of punch_in and punch_out are BSON dates in MongoDB and epoch ms in the API
    if field in ("punch_in", "punch_out") and isinstance(value, datetime):
        return dt_to_ms(value)
    return value


def record_out(doc: dict) -> dict:
    """Stored attendance document -> API representation (no _id, instants as epoch ms)."""
    history = []
    for entry in doc.get("history") or []:
        changes = {
            field: {"from": _history_value(field, c.get("from")), "to": _history_value(field, c.get("to"))}
            for field, c in (entry.get("changes") or {}).items()
        }
        history.append({
            "at": dt_to_ms(entry["at"]),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    punch_in, punch_out = doc.get("punch_in"), doc.get("punch_out")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(punch_in) if punch_in else None,
        "punch_out": dt_to_ms(punch_out) if punch_out else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,  # missing means 0
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),  # missing means false
        "history": history,
    }


def get_employee_or_404(emp_code: str, projection: Optional[dict] = None) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, projection)
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 6. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = datetime.now(UTC)
    doc["created_at"] = now.replace(microsecond=now.microsecond // 1000 * 1000)  # BSON keeps milliseconds
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code is the uniqueness guarantee
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    query = {} if department is None else {"department": department}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(ts, emp["shift_start"], emp["shift_end"])  # R1
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, date_str, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    try:
        # One atomic insert. The unique index on (emp_code, date) decides the winner when two
        # identical requests arrive together: exactly one insert succeeds, the others raise.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent record whose punch_in is at or before punched_at. Records without punch times
    # (ABSENT / LEAVE) never match because a null is not <= a date. The (emp_code, date) index
    # serves this lookup, scanned backwards, so it works across midnight too.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found for this employee")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punched_in = as_utc(rec["punch_in"])
    if ts <= punched_in:
        unprocessable(["body", "punched_at"], "punched_at must be after punch_in")
    if ts - punched_in > timedelta(hours=24):
        unprocessable(["body", "punched_at"], "punched_at must be within 24 hours of punch_in")

    derived = derive_fields(rec["status"], punched_in, ts, rec["date"], emp["shift_start"], emp["shift_end"])
    # The `punch_out: None` condition makes the update atomic: of two simultaneous punch-outs,
    # only one still finds the record open; the other gets None back and answers 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    """Filter for GET /attendance. Shared with the explain endpoint so both build it the same way."""
    start = parse_date(date_from, ["query", "date_from"]) if date_from is not None else None
    end = parse_date(date_to, ["query", "date_to"]) if date_to is not None else None
    if start and end and start > end:
        unprocessable(["query", "date_from"], "date_from must not be after date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if start or end:
        query["date"] = {}
        if start:
            query["date"]["$gte"] = date_from
        if end:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    query = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 6b. Regularization (manual correction with an audit trail)
# --------------------------------------------------------------------------- #
CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
MAX_PATCH_ATTEMPTS = 8


def plan_regularization(rec: dict, emp: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Work out the record's new values and the history `changes` for one correction.

    Pure function (no database access). Raises 422 for every rule in the contract.
    Returns (new_values, changes) where changes only has the fields that actually changed.
    """
    old_in = as_utc(rec["punch_in"]) if rec.get("punch_in") else None
    old_out = as_utc(rec["punch_out"]) if rec.get("punch_out") else None
    new_status = body.status or rec["status"]

    if new_status in ABSENCE:
        # ABSENT / LEAVE clear both punch times, so supplying one is contradictory.
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable(["body", "punch_in"], f"punch times cannot be supplied with status {new_status}")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            unprocessable(["body", "punch_in"], f"status {new_status} requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if new_out is not None and (body.punch_in is not None or body.punch_out is not None):
            if new_out <= new_in:
                unprocessable(["body", "punch_out"], "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                unprocessable(["body", "punch_out"], "punch_out must be within 24 hours of punch_in")

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derive_fields(new_status, new_in, new_out, rec["date"], emp["shift_start"], emp["shift_end"]),
    }
    old_values = {
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {
        field: {"from": old_values[field], "to": new_values[field]}
        for field in CHANGE_FIELDS
        if old_values[field] != new_values[field]
    }
    if not any(field in changes for field in ("status", "punch_in", "punch_out")):
        unprocessable(["body"], "the request changes nothing")
    return new_values, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, ["path", "date"])
    emp = get_employee_or_404(emp_code)

    for _attempt in range(MAX_PATCH_ATTEMPTS):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for this date")
        new_values, changes = plan_regularization(rec, emp, body)

        entry = {
            "at": now_utc(),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }
        # Optimistic concurrency control: the update only applies if the record is still exactly
        # as we read it (same history length, status and punch times). If another correction or
        # punch-out slipped in, nothing matches, we re-read and recompute, so no history entry is
        # ever overwritten or lost. After too many lost races we give up with 409.
        history_len = len(rec.get("history") or [])
        condition: dict = {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
        }
        if history_len == 0:  # legacy documents may have no `history` field at all
            condition["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]
        else:
            condition["history"] = {"$size": history_len}

        updated = db.attendance_logs.find_one_and_update(
            condition,
            {"$set": new_values, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# 7. Analytics: aggregation pipelines
# --------------------------------------------------------------------------- #
# Reported numbers are rounded in Python (half-up, R8) because MongoDB's $round is
# round-half-to-even. The database does all the grouping, joining, ranking and gap filling.
_IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
_IS_WEEKDAY = {  # $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Monday-Friday = 2..6
    "$in": [
        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None}}},
        [2, 3, 4, 5, 6],
    ]
}
_LATE = {"$ifNull": ["$late_minutes", 0]}  # missing means 0
_DAY_CREDIT = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}  # half day = 0.5 (R5)
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def _sum_if(condition: dict, value: Any = 1) -> dict:
    return {"$sum": {"$cond": [condition, value, 0]}}


def log_accumulators() -> dict:
    """$group accumulators over stored attendance documents (stored derived values are used as-is)."""
    return {
        "present_days": _sum_if({"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _DAY_CREDIT),  # R6 + R7 + R5
        "hours_sum": _sum_if(_HAS_HOURS, "$work_hours"),
        "hours_n": _sum_if(_HAS_HOURS),
        "late_count": _sum_if({"$gt": [_LATE, 0]}),
        "total_late_minutes": {"$sum": _LATE},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": _sum_if({"$eq": ["$status", "LEAVE"]}),
        "on_duty_count": _sum_if({"$eq": ["$status", "ON_DUTY"]}),
    }


def employee_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": None, **log_accumulators()}},
    ]


def department_summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    """Start from employees so people with zero logs still count (R9), then join their logs."""
    employee_filter: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        employee_filter["department"] = department
    return [
        {"$match": employee_filter},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            # one summary document per employee (or none if the employee has no logs this month)
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {"_id": None, **log_accumulators()}},
            ],
            "as": "month",
        }},
        {"$set": {"month": {"$arrayElemAt": ["$month", 0]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            # sums and counts (not per-employee averages) so the average is taken over records
            "present_days": {"$sum": "$month.present_days"},
            "hours_sum": {"$sum": "$month.hours_sum"},
            "hours_n": {"$sum": "$month.hours_n"},
            "late_count": {"$sum": "$month.late_count"},
            "total_late_minutes": {"$sum": "$month.total_late_minutes"},
            "leave_count": {"$sum": "$month.leave_count"},
            "on_duty_count": {"$sum": "$month.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1,2,2,4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


MAX_TREND_DAYS = 92
MS_PER_DAY = 86_400_000


def department_trend_pipeline(department: str, emp_codes: list, start: date_cls, n_days: int) -> list:
    """One row per calendar day with gap filling and a 7-day moving average, all in the database.

    1. collapse the department's employees into one document holding their joined_on dates
    2. $lookup the department's logs for the range, already grouped per day (index-backed)
    3. $range + $unwind create exactly one row per day, whether or not any log exists that day
    4. compute headcount, rate and the moving average with $setWindowFields
    """
    first = start.isoformat()
    last = (start + timedelta(days=n_days - 1)).isoformat()
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "joined": {"$push": "$joined_on"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "pipeline": [
                {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": first, "$lte": last}}},
                {"$group": {
                    "_id": "$date",
                    "present_count": _sum_if(_IS_PRESENT, _DAY_CREDIT),  # any weekday or weekend
                    "late_count": _sum_if({"$gt": [_LATE, 0]}),
                }},
            ],
            "as": "per_day",
        }},
        {"$set": {"offset": {"$range": [0, n_days]}}},
        {"$unwind": "$offset"},
        {"$set": {"day_start": {"$add": [start_dt, {"$multiply": ["$offset", MS_PER_DAY]}]}}},
        {"$set": {"date": {"$dateToString": {"date": "$day_start", "format": "%Y-%m-%d"}}}},
        {"$set": {"found": {"$arrayElemAt": [
            {"$filter": {"input": "$per_day", "as": "p", "cond": {"$eq": ["$$p._id", "$date"]}}}, 0]}}},
        {"$set": {
            "is_working_day": {"$in": [{"$dayOfWeek": "$day_start"}, [2, 3, 4, 5, 6]]},
            "headcount": {"$size": {"$filter": {  # R9: joined on or before that day
                "input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
            "present_count": {"$ifNull": ["$found.present_count", 0]},
            "late_count": {"$ifNull": ["$found.late_count", 0]},
        }},
        {"$set": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        # current row + up to 6 preceding rows, only rows inside the requested range exist, so the
        # first rows automatically use a shorter window. $avg ignores null rates.
        {"$setWindowFields": {
            "sortBy": {"day_start": 1},
            "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"day_start": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "rate": 1, "moving": 1,
        }},
    ]


def prepare_trend(department: str, date_from: str, date_to: str) -> tuple[list, date_cls, int]:
    """Validate the trend request; return (department emp_codes, first day, number of days)."""
    start = parse_date(date_from, ["query", "from"])
    end = parse_date(date_to, ["query", "to"])
    if end < start:
        unprocessable(["query", "to"], "to must not be before from")
    n_days = (end - start).days + 1
    if n_days > MAX_TREND_DAYS:
        unprocessable(["query", "to"], f"the range must not exceed {MAX_TREND_DAYS} days")
    emp_codes = db.employees.distinct("emp_code", {"department": department})
    if not emp_codes:
        raise HTTPException(404, "department not found")
    return emp_codes, start, n_days


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code, {"joined_on": 1})
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    agg = rows[0] if rows else {}

    # R7: working days are Mon-Fri, counted from joined_on for a mid-month joiner
    window_start = max(date_cls.fromisoformat(first), date_cls.fromisoformat(emp["joined_on"]))
    working_days = count_weekdays(window_start, date_cls.fromisoformat(last))
    present_days = agg.get("present_days", 0)
    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up(Decimal(repr(float(present_days))) * 100 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(agg.get("leave_count", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        hours_n = int(row["hours_n"])
        avg_hours = None
        if hours_n > 0:
            avg_hours = round_half_up(Decimal(repr(float(row["hours_sum"]))) / hours_n, 2)
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": round_half_up(row["present_days"], 2),
            "avg_work_hours": avg_hours,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQ, limit: LimitQ = 10, department: Optional[str] = None):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


def _trend_items(rows: list) -> list:
    return [
        {
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": None if r.get("rate") is None else round_half_up(r["rate"], 4),
            "moving_avg_7d": None if r.get("moving") is None else round_half_up(r["moving"], 4),
        }
        for r in rows
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: str,
):
    emp_codes, start, n_days = prepare_trend(department, from_, to)
    rows = list(db.employees.aggregate(department_trend_pipeline(department, emp_codes, start, n_days)))
    return {"department": department, "items": _trend_items(rows)}


# --------------------------------------------------------------------------- #
# 8. Admin: explain
# --------------------------------------------------------------------------- #
ExplainName = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def _json_safe(document: Any) -> Any:
    """Explain output contains BSON types (ObjectId, Int64, dates); turn it into plain JSON."""
    return json.loads(json_util.dumps(document, json_options=json_util.RELAXED_JSON_OPTIONS))


def _explain(command: dict) -> dict:
    return db.command({"explain": command, "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: LimitQ = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    """Run explain (executionStats) on the main query of an endpoint, built by the same functions
    the endpoint itself uses, so the plan shown is the plan that really runs."""
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        explained = _explain({
            "find": collection,
            "filter": attendance_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        })
    elif endpoint == "employee_monthly":
        if emp_code is None:
            missing_param("emp_code")
        if month is None:
            missing_param("month")
        first, last = month_bounds(month)
        collection = "attendance_logs"
        explained = _explain({
            "aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, first, last), "cursor": {}})
    elif endpoint == "department_summary":
        if month is None:
            missing_param("month")
        first, last = month_bounds(month)
        collection = "employees"
        explained = _explain({
            "aggregate": collection, "pipeline": department_summary_pipeline(first, last, department), "cursor": {}})
    elif endpoint == "late_leaderboard":
        if month is None:
            missing_param("month")
        first, last = month_bounds(month)
        collection = "attendance_logs"
        explained = _explain({
            "aggregate": collection, "pipeline": late_leaderboard_pipeline(first, last, limit, department),
            "cursor": {}})
    else:  # department_trend
        if department is None:
            missing_param("department")
        if from_ is None:
            missing_param("from")
        if to is None:
            missing_param("to")
        emp_codes, start, n_days = prepare_trend(department, from_, to)
        collection = "employees"
        explained = _explain({
            "aggregate": collection, "pipeline": department_trend_pipeline(department, emp_codes, start, n_days),
            "cursor": {}})
    return {"endpoint": endpoint, "collection": collection, "explain": _json_safe(explained)}
