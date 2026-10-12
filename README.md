# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # required in practice; a local .env also works
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
`MONGO_URI` and `MONGO_DB` come from the environment (a `.env` is only a fallback). Indexes are created at startup; nothing else to do. Needs MongoDB 6.0+ (`$densify`, `$setWindowFields`). Optional sample data: `python sample_seed.py`.

## Status
All endpoints in `openapi.yaml` are implemented. I tested against MongoDB 7.0.14, including a 100,000-record dataset (explain plans show `IXSCAN` and no `COLLSCAN`). My test scripts are not part of this repository.

## Things to know
- `working_days` (monthly summary) is computed from the calendar in Python; everything that reads attendance data runs in aggregation pipelines.
- Half-up rounding is done exactly (`Decimal` in Python, Decimal128 inside the trend pipeline), not with `$round`.
- Punch-out picks the latest record with `punch_in <= punched_at`. A `punched_at` earlier than every punch-in therefore gives 404 ("no punch-in found"); an equal or later-than-24h value gives 422.
- Concurrent corrections use an optimistic guard: the loser gets 409 and nothing is written.
- Trend range limit: at most 92 days inclusive.
- If an index cannot be built (for example odd data breaking a unique index) the app logs it and keeps running.
