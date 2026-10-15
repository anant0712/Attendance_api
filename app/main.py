import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from fastapi import FastAPI, HTTPException
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError,ConnectionFailure
from dotenv import load_dotenv
from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator, model_validator, EmailStr
from typing import Annotated, Optional, Literal
from contextlib import asynccontextmanager


load_dotenv()
# --- Database Connection ---
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

# --- Startup Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs when the server starts: Creates indexes for faster database searching
    db.employees.create_index("emp_code", unique=True)
    db.attendance.create_index([("emp_code", 1), ("punched_at", 1)])
    yield

app = FastAPI(title="Attendance API", lifespan=lifespan)

# --- Time Helpers ---
IST = timezone(timedelta(hours=5, minutes=30))

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def ms_to_dt(ms: int) -> datetime:
    # Truncates milliseconds to seconds securely
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(microsecond=0)

def dt_to_ms(dt: datetime) -> int:
    return int(as_utc(dt).timestamp() * 1000)

# --- Math Helper ---
def round_half_up(value: float, decimals: int = 2) -> float:
    # Rounding to 10 places first eliminates float noise on exact ties
    clean_value = round(value, 10)
    factor = str(10 ** -decimals) if decimals > 0 else '1'
    return float(Decimal(str(clean_value)).quantize(Decimal(factor), rounding=ROUND_HALF_UP))

# --- Health Endpoint ---
@app.get("/health")
def health_check():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except ConnectionFailure:
        raise HTTPException(status_code=503, detail="Database connection failed")
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return datetime.strptime(shift_end, "%H:%M").time() < datetime.strptime(shift_start, "%H:%M").time()


def attendance_date(punched_in: datetime, shift_start: str, shift_end: str) -> str:
    local_punch = as_utc(punched_in).astimezone(IST)
    if is_overnight(shift_start, shift_end):
        end_time = datetime.strptime(shift_end, "%H:%M").time()
        # If punch is before shift end, it counts toward the previous day's shift
        if local_punch.time() < end_time:
            return (local_punch.date() - timedelta(days=1)).isoformat()
    return local_punch.date().isoformat()


def shift_window(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    # Returns (shift_start_dt, shift_end_dt) in UTC
    d = date.fromisoformat(date_str)
    t_start = datetime.strptime(shift_start, "%H:%M").time()
    t_end = datetime.strptime(shift_end, "%H:%M").time()

    start_dt = datetime.combine(d, t_start).replace(tzinfo=IST)
    end_dt = datetime.combine(d, t_end).replace(tzinfo=IST)

    if is_overnight(shift_start, shift_end):
        end_dt += timedelta(days=1)

    return as_utc(start_dt), as_utc(end_dt)


def compute_late_minutes(punched_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    start_dt, _ = shift_window(date_str, shift_start, shift_end)
    delta_seconds = (punched_in - start_dt).total_seconds()
    if delta_seconds > 600:
        return int(delta_seconds // 60)
    return 0


def compute_overtime(punched_out: datetime, date_str: str, shift_start: str, shift_end: str) -> float:
    _, end_dt = shift_window(date_str, shift_start, shift_end)
    delta_seconds = (punched_out - end_dt).total_seconds()
    if delta_seconds >= 1800:
        return round_half_up(delta_seconds / 60, 0)
    return 0.0


def compute_work_hours(punched_in: datetime, punched_out: datetime) -> float:
    delta_seconds = (punched_out - punched_in).total_seconds()
    return round_half_up(delta_seconds / 3600, 2)


def count_weekdays(start_date: datetime.date, end_date: datetime.date) -> int:
    days = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:  # 0-4 are Mon-Fri
            days += 1
        current += timedelta(days=1)
    return days


def derive_fields(status: str, punched_in: datetime, punched_out: Optional[datetime], date_str: str, shift_start: str,
                  shift_end: str) -> dict:
    res = {
        "late_minutes": 0,
        "overtime_minutes": 0,
        "work_hours": None,
        "half_day": False
    }

    if status == "PRESENT" and punched_in:
        res["late_minutes"] = compute_late_minutes(punched_in, date_str, shift_start, shift_end)

    if status == "PRESENT" and punched_in and punched_out:
        res["overtime_minutes"] = compute_overtime(punched_out, date_str, shift_start, shift_end)
        res["work_hours"] = compute_work_hours(punched_in, punched_out)
        res["half_day"] = res["work_hours"] < 4.5

    return res

EpochMs = Annotated[int, Field(strict=True, ge=1_000_000_000_000, le=4_102_444_800_000)]

class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4}$")
    name: str = Field(..., min_length=1)
    email: EmailStr
    department: str = Field(..., min_length=1)
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    shift_start: str = Field(..., pattern=r"^(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: str = Field(..., pattern=r"^(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]$")

    @field_validator("joined_on")
    @classmethod
    def validate_date(cls, v):
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid date")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end cannot be the same")
        return self

class PunchInIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4}$")
    punched_at: EpochMs = Field(default_factory=lambda: int(now_utc().timestamp() * 1000))
    status: Literal["PRESENT"] = "PRESENT"

class PunchOutIn(BaseModel):
    punched_at: EpochMs = Field(default_factory=lambda: int(now_utc().timestamp() * 1000))

class RegularizeIn(BaseModel):
    reason: str = Field(..., min_length=1)
    regularized_by: str = Field(..., min_length=1)
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE"]] = None



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs when the server starts: Creates indexes for faster database searching
    db.employees.create_index("emp_code", unique=True)
    db.attendance.create_index([("emp_code", 1), ("punched_at", 1)])
    yield


# Update your FastAPI app initialization at the top of your file to use the lifespan:
# app = FastAPI(title="Attendance API", lifespan=lifespan)

@app.post("/employees", status_code=201)
def create_employee(emp: EmployeeIn):
    if db.employees.find_one({"emp_code": emp.emp_code}):
        raise HTTPException(status_code=400, detail="Employee code already exists")

    emp_dict = emp.model_dump()
    db.employees.insert_one(emp_dict)

    # Remove the MongoDB specific _id before returning
    emp_dict.pop("_id", None)
    return emp_dict


@app.get("/employees")
def get_employees():
    return list(db.employees.find({}, {"_id": 0}))

@app.post("/attendance/punch-in", status_code=201)
def punch_in(payload: PunchInIn):
    # Find the employee to ensure they exist
    emp = db.employees.find_one({"emp_code": payload.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Convert the millisecond integer timestamp to a UTC datetime object
    punch_dt = datetime.fromtimestamp(payload.punched_at / 1000.0, tz=timezone.utc)
    
    # Calculating the official attendance date using the datetime object
    att_date = attendance_date(punch_dt, emp["shift_start"], emp["shift_end"])
    
    # Checking if they already punched in for this specific shift
    existing_record = db.attendance.find_one({
        "emp_code": payload.emp_code, 
        "attendance_date": att_date
    })
    
    if existing_record:
        raise HTTPException(status_code=400, detail="Already punched in for this shift")
    
    # Creating the new attendance record
    new_log = {
        "emp_code": payload.emp_code,
        "attendance_date": att_date,
        "punch_in": payload.punched_at, # Keep original integer for the database
        "punch_out": None,
    }
    
    try:
        db.attendance.insert_one(new_log)
    except DuplicateKeyError:
        raise HTTPException(status_code=400, detail="Punch-in race condition detected")
    
    new_log.pop("_id", None)
    return new_log