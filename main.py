
"""Employee Attendance & Analytics API, contract v2.0."""
import os
import re
import math
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import ObjectId

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# -------------------------------------------------------------------
# Helper functions
# -------------------------------------------------------------------

def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def trunc_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def epoch_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def parse_ms(value):
    if value is None:
        return now_utc()

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 100_000_000_000 <= value <= 4_102_444_800_000
    ):
        raise HTTPException(422, "timestamp must be epoch milliseconds")

    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def round_half_up(value, places=2):
    if value is None:
        return None

    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP
        )
    )


def as_ist(dt):
    return dt.astimezone(IST)


def date_valid(value):
    try:
        return date.fromisoformat(value)
    except Exception:
        raise HTTPException(422, "date must be YYYY-MM-DD")


def month_bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")

    year, month_number = map(int, month.split("-"))
    start = date(year, month_number, 1)

    if month_number == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month_number + 1, 1) - timedelta(days=1)

    return start.isoformat(), end.isoformat(), start, end


def shift_dt(day: str, hhmm: str):
    hour, minute = map(int, hhmm.split(":"))
    day_value = date_valid(day)
    return datetime.combine(
        day_value,
        time(hour, minute),
        IST
    ).astimezone(UTC)


def attendance_date(punch: datetime, emp):
    local = as_ist(punch)
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        local -= timedelta(days=1)

    return local.date().isoformat()


def late_minutes(punch, emp, record_date):
    start = shift_dt(record_date, emp["shift_start"])
    difference = math.floor((punch - start).total_seconds() / 60)

    return difference if (punch - start).total_seconds() > 600 else 0


def overtime_minutes(punch_out, emp, record_date):
    end_day = date_valid(record_date)

    if emp["shift_end"] <= emp["shift_start"]:
        end_day += timedelta(days=1)

    shift_end = shift_dt(end_day.isoformat(), emp["shift_end"])
    minutes = math.floor(
        (punch_out - shift_end).total_seconds() / 60
    )

    return minutes if minutes >= 30 else 0


def work_hours(punch_in, punch_out):
    elapsed = (punch_out - punch_in).total_seconds() / 3600
    return round_half_up(elapsed, 2)


def derived(punch_in, punch_out, status, emp, record_date):
    if status not in PRESENCE:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False
        }

    late = (
        late_minutes(punch_in, emp, record_date)
        if punch_in else 0
    )

    if punch_in and punch_out:
        hours = work_hours(punch_in, punch_out)

        return {
            "late_minutes": late,
            "work_hours": hours,
            "overtime_minutes": overtime_minutes(
                punch_out, emp, record_date
            ),
            "half_day": hours < 4.5
        }

    return {
        "late_minutes": late,
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False
    }


def serialize(doc):
    if not doc:
        return doc

    output = dict(doc)
    output.pop("_id", None)

    if "id" in output and isinstance(output["id"], ObjectId):
        output["id"] = str(output["id"])

    for key in ("created_at", "punch_in", "punch_out"):
        if key in output:
            output[key] = epoch_ms(output[key])

    history = []

    for entry in output.get("history", []) or []:
        entry = dict(entry)
        entry["at"] = epoch_ms(entry.get("at"))

        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = epoch_ms(change[side])

        history.append(entry)

    if "history" in output:
        output["history"] = history

    return output


def working_days(start: date, end: date):
    if end < start:
        return 0

    days = (end - start).days + 1
    weeks, remainder = divmod(days, 7)
    count = weeks * 5

    for index in range(remainder):
        if (start + timedelta(days=weeks * 7 + index)).weekday() < 5:
            count += 1

    return count


# -------------------------------------------------------------------
# Request models
# -------------------------------------------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join(cls, value):
        date_valid(value)
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError(
                "shift_start and shift_end must differ"
            )
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, value):
        if value is not None:
            parse_ms(value)
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, value):
        if value is not None:
            parse_ms(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ts(cls, value):
        if value is not None:
            parse_ms(value)
        return value


# -------------------------------------------------------------------
# Database indexes and health
# -------------------------------------------------------------------

@app.on_event("startup")
def ensure_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="department_joined"
    )
    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="employee_joined_on"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="department_employee"
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_day"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING),
            ("status", ASCENDING)
        ],
        name="attendance_employee_date_status"
    )
    db.attendance_logs.create_index(
        [
            ("date", ASCENDING),
            ("late_minutes", DESCENDING),
            ("emp_code", ASCENDING)
        ],
        name="attendance_late_date"
    )


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


# -------------------------------------------------------------------
# Employee endpoints
# -------------------------------------------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    query = {"department": department} if department is not None else {}

    total = db.employees.count_documents(query)
    docs = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size
    }


# -------------------------------------------------------------------
# Attendance endpoints
# -------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:
        raise HTTPException(404, "employee not found")

    timestamp = parse_ms(body.punched_at)
    record_date = attendance_date(timestamp, employee)

    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": timestamp,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(
            timestamp, employee, record_date
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            "already punched in for this date"
        )

    return serialize(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:
        raise HTTPException(404, "employee not found")

    timestamp = parse_ms(body.punched_at)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": timestamp}
        },
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(404, "no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    if (
        timestamp <= record["punch_in"]
        or timestamp - record["punch_in"] > timedelta(hours=24)
    ):
        raise HTTPException(
            422,
            "punch-out must be after punch-in and within 24 hours"
        )

    values = derived(
        record["punch_in"],
        timestamp,
        record["status"],
        employee,
        record["date"]
    )

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
            "punch_in": {"$lt": timestamp}
        },
        {"$set": {"punch_out": timestamp, **values}},
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(409, "already punched out")

    return serialize(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from:
        date_valid(date_from)
    if date_to:
        date_valid(date_to)

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            422,
            "date_from must be <= date_to"
        )

    query = {}

    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(query)
    docs = list(
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for doc in docs:
        doc["id"] = doc.pop("_id")

    return {
        "items": [serialize(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    date_valid(date)
    employee = db.employees.find_one({"emp_code": emp_code})

    if not employee:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )

    if not record:
        raise HTTPException(404, "attendance record not found")

    status = (
        body.status
        if body.status is not None
        else record["status"]
    )

    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                "ABSENT/LEAVE cannot have punch times"
            )
        punch_in_value = None
        punch_out_value = None
    else:
        punch_in_value = (
            parse_ms(body.punch_in)
            if body.punch_in is not None
            else record.get("punch_in")
        )
        punch_out_value = (
            parse_ms(body.punch_out)
            if body.punch_out is not None
            else record.get("punch_out")
        )

        if punch_in_value is None:
            raise HTTPException(
                422,
                "presence status requires punch_in"
            )

        if attendance_date(punch_in_value, employee) != date:
            raise HTTPException(
                422,
                "punch_in must belong to attendance date"
            )

        if (
            punch_out_value is not None
            and (
                punch_out_value <= punch_in_value
                or punch_out_value - punch_in_value > timedelta(hours=24)
            )
        ):
            raise HTTPException(
                422,
                "punch_out must be after punch_in and within 24 hours"
            )

    values = derived(
        punch_in_value,
        punch_out_value,
        status,
        employee,
        date
    )

    new_values = {
        "status": status,
        "punch_in": punch_in_value,
        "punch_out": punch_out_value,
        **values
    }

    tracked_fields = [
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day"
    ]

    changes = {}

    for key in tracked_fields:
        old_value = record.get(key)

        if key in {"late_minutes", "overtime_minutes"}:
            old_value = old_value or 0
        if key == "half_day":
            old_value = bool(old_value)

        if old_value != new_values[key]:
            changes[key] = {
                "from": old_value,
                "to": new_values[key]
            }

    if not changes:
        raise HTTPException(
            422,
            "request does not change the record"
        )

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    if not record.get("history"):
        history_filter = {
            "$or": [
                {"history": record.get("history", [])},
                {"history": {"$exists": False}}
            ]
        }
    else:
        history_filter = {"history": record["history"]}

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], **history_filter},
        {
            "$set": new_values,
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(
            409,
            "record changed concurrently; retry"
        )

    return serialize(updated)


# -------------------------------------------------------------------
# Shared analytics aggregation builders
# -------------------------------------------------------------------

def attendance_match(month):
    start, end, _, _ = month_bounds(month)
    return {"date": {"$gte": start, "$lte": end}}


def monthly_pipeline(emp_code, month):
    query = attendance_match(month)
    query["emp_code"] = emp_code

    return [
        {"$match": query},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE)]},
                                    {
                                        "$not": [
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$date"
                                                            }
                                                        }
                                                    },
                                                    [1, 7]
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1]},
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$late_minutes", 0]},
                                    0
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]


def department_pipeline(month, department=None):
    start, end, _, _ = month_bounds(month)
    employee_match = {"joined_on": {"$lte": end}}

    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lte": ["$date", end]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": ["$logs.date", None]}
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$not": [
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$logs.date"
                                                            }
                                                        }
                                                    },
                                                    [1, 7]
                                                ]
                                            }
                                        ]
                                    },
                                    {"$cond": ["$logs.half_day", 0.5, 1]},
                                    0
                                ]
                            },
                            0
                        ]
                    }
                },
                "work_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            0
                        ]
                    }
                },
                "work_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$logs.late_minutes", 0]},
                                    0
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_count", 0]},
                        {"$divide": ["$work_sum", "$work_count"]},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}}
    ]


def leaderboard_pipeline(month, department=None, limit=10):
    query = attendance_match(month)

    pipeline = [
        {"$match": query},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$late_minutes", 0]},
                                    0
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"}
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}}
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])

    return pipeline


# -------------------------------------------------------------------
# Monthly and department analytics endpoints
# -------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1}
    )

    if not employee:
        raise HTTPException(404, "employee not found")

    _, _, month_start, month_end = month_bounds(month)

    working = working_days(
        max(month_start, date_valid(employee["joined_on"])),
        month_end
    )

    rows = list(
        db.attendance_logs.aggregate(
            monthly_pipeline(emp_code, month)
        )
    )
    result = rows[0] if rows else {}

    present = round_half_up(result.get("present_days", 0), 2)
    percentage = (
        round_half_up(present / working * 100, 4)
        if working else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": present,
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": percentage
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = None
):
    rows = list(
        db.employees.aggregate(
            department_pipeline(month, department)
        )
    )

    items = []

    for row in rows:
        row["present_days"] = round_half_up(row["present_days"], 2)
        row["avg_work_hours"] = (
            round_half_up(row["avg_work_hours"], 2)
            if row["avg_work_hours"] is not None
            else None
        )
        items.append(row)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    items = list(
        db.attendance_logs.aggregate(
            leaderboard_pipeline(month, department, limit)
        )
    )

    return {"month": month, "items": items}


# -------------------------------------------------------------------
# Department trend endpoint
# -------------------------------------------------------------------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...)
):
    start = date_valid(from_)
    end = date_valid(to)

    if end < start:
        raise HTTPException(422, "to must be >= from")

    number_of_days = (end - start).days + 1

    if number_of_days > 92:
        raise HTTPException(
            422,
            "date range must not exceed 92 days"
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    # Generate every calendar date in the requested range.
    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "days": {
                    "$map": {
                        "input": {"$range": [0, number_of_days]},
                        "as": "i",
                        "in": {
                            "$dateToString": {
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {
                                                "dateString": from_
                                            }
                                        },
                                        "unit": "day",
                                        "amount": "$$i"
                                    }
                                },
                                "format": "%Y-%m-%d",
                                "timezone": "UTC"
                            }
                        }
                    }
                }
            }
        },
        {"$unwind": "$days"},
        {"$project": {"_id": 0, "date": "$days"}},

        # Find department employees who have joined by each date.
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {
                                        "$lte": [
                                            "$joined_on",
                                            "$$current_date"
                                        ]
                                    }
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}}
                ],
                "as": "eligible_employees"
            }
        },

        # Join all attendance records for the generated date.
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "date",
                "foreignField": "date",
                "as": "all_logs"
            }
        },

        # Keep only records belonging to eligible employees.
        {
            "$set": {
                "logs": {
                    "$filter": {
                        "input": "$all_logs",
                        "as": "log",
                        "cond": {
                            "$in": [
                                "$$log.emp_code",
                                {
                                    "$map": {
                                        "input": "$eligible_employees",
                                        "as": "employee",
                                        "in": "$$employee.emp_code"
                                    }
                                }
                            ]
                        }
                    }
                },
                "headcount": {"$size": "$eligible_employees"}
            }
        },

        # Compute present days and late arrivals in MongoDB.
        {
            "$set": {
                "stats": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": {"present": 0, "late": 0},
                        "in": {
                            "present": {
                                "$add": [
                                    "$$value.present",
                                    {
                                        "$cond": [
                                            {
                                                "$in": [
                                                    "$$this.status",
                                                    list(PRESENCE)
                                                ]
                                            },
                                            {
                                                "$cond": [
                                                    {
                                                        "$ifNull": [
                                                            "$$this.half_day",
                                                            False
                                                        ]
                                                    },
                                                    0.5,
                                                    1
                                                ]
                                            },
                                            0
                                        ]
                                    }
                                ]
                            },
                            "late": {
                                "$add": [
                                    "$$value.late",
                                    {
                                        "$cond": [
                                            {
                                                "$gt": [
                                                    {
                                                        "$ifNull": [
                                                            "$$this.late_minutes",
                                                            0
                                                        ]
                                                    },
                                                    0
                                                ]
                                            },
                                            1,
                                            0
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                },
                "is_working_day": {
                    "$not": [
                        {
                            "$in": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$date"
                                        }
                                    }
                                },
                                [1, 7]
                            ]
                        }
                    ]
                }
            }
        },

        # Attendance rate is the present-day fraction of headcount.
        {
            "$set": {
                "present_count": "$stats.present",
                "late_count": "$stats.late",
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$divide": [
                                "$stats.present",
                                "$headcount"
                            ]
                        },
                        None
                    ]
                }
            }
        },

        # Calculate a trailing seven-calendar-day moving average.
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        },
        {"$sort": {"date": 1}}
    ]

    rows = list(db.employees.aggregate(pipeline))
    items = []

    for row in rows:
        row["is_working_day"] = (
            date_valid(row["date"]).weekday() < 5
        )

        row["attendance_rate"] = (
            round_half_up(row["attendance_rate"], 4)
            if (
                row["attendance_rate"] is not None
                and row["is_working_day"]
                and row["headcount"] > 0
            )
            else None
        )

        row["moving_avg_7d"] = (
            round_half_up(row["moving_avg_7d"], 4)
            if row.get("moving_avg_7d") is not None
            else None
        )

        items.append(row)

    return {"department": department, "items": items}


# -------------------------------------------------------------------
# Query plan explain endpoint
# -------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    supported = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    }

    if endpoint not in supported:
        raise HTTPException(422, "unsupported endpoint")

    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status

        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size
            },
            verbosity="executionStats"
        )
        collection = "attendance_logs"

    else:
        if not month:
            raise HTTPException(422, "month is required")

        month_bounds(month)

        if endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(422, "emp_code is required")

            explain = db.command(
                "explain",
                {
                    "aggregate": "attendance_logs",
                    "pipeline": monthly_pipeline(emp_code, month),
                    "cursor": {}
                },
                verbosity="executionStats"
            )
            collection = "attendance_logs"

        elif endpoint == "department_summary":
            explain = db.command(
                "explain",
                {
                    "aggregate": "employees",
                    "pipeline": department_pipeline(month, department),
                    "cursor": {}
                },
                verbosity="executionStats"
            )
            collection = "employees"

        elif endpoint == "late_leaderboard":
            explain = db.command(
                "explain",
                {
                    "aggregate": "attendance_logs",
                    "pipeline": leaderboard_pipeline(
                        month, department, limit
                    ),
                    "cursor": {}
                },
                verbosity="executionStats"
            )
            collection = "attendance_logs"

        else:
            if not department or not from_ or not to:
                raise HTTPException(
                    422,
                    "department, from and to are required"
                )

            date_valid(from_)
            date_valid(to)

            explain = db.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": {
                        "date": {
                            "$gte": from_,
                            "$lte": to
                        }
                    },
                    "sort": {"date": 1},
                    "limit": 100000
                },
                verbosity="executionStats"
            )
            collection = "attendance_logs"

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain
    }
