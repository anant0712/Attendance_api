# Employee Attendance & Analytics API

FastAPI + MongoDB backend for punch-in/out, manual corrections with an audit
trail, and attendance analytics. All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then set MONGO_URI and MONGO_DB
uvicorn app.main:app --port 8000
```

- `MONGO_URI` and `MONGO_DB` are read from environment variables. `.env` is
  only a local fallback; real environment variables take priority.
- Requires MongoDB 6.0 or newer.
- Indexes are created automatically at startup (idempotent).
- Interactive API docs: http://127.0.0.1:8000/docs

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/test_rules.py   # business rules, no database needed
pytest tests/test_api.py     # needs MongoDB; uses the `attendance_test` database
```

## Decisions worth knowing

- Instants are stored as BSON dates (UTC) and exposed as epoch milliseconds.
- Derived fields (work_hours, late_minutes, overtime_minutes, half_day) are
  stored; analytics read the stored values.
- Rounding is half-up (Decimal), because MongoDB's `$round` rounds half to even.
- `moving_avg_7d` averages the unrounded daily rates, then rounds to 4 decimals.
- Gap filling in the trend uses `$range` + `$unwind` rather than `$densify`.

## Status

All endpoints in the contract are implemented. <Say here what you tested and
what you did not, e.g. "not tested against MongoDB Atlas", "explain output
checked only on small data".>
