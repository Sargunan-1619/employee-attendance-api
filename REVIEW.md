# REVIEW.md

Defects found in the starter `app/main.py` (line numbers refer to the starter as supplied).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes`, L36-37 | The grace test runs on already-floored minutes (`minutes > 10`), so "strictly more than 10:00" is lost. | Shift 09:30, punch-in 09:40:01: floor gives 10, `10 > 10` is false, returns 0 (spec: 10). | Work in seconds: late only if `seconds > 600`, then `seconds // 60`. |
| 2 | `compute_late_minutes`, L34-35 | Shift start is built with `replace()` on whatever zone the datetime has (server local), not IST; no overnight handling; sub-seconds not truncated. | Overnight 22:00 shift, punch-in 00:30 IST: start lands on the same day, result is negative, so 0 (spec: 150). | New `attendance_date` (R1) picks the shift's day; `at_ist` builds the start in IST; instants are truncated to whole seconds on input. |
| 3 | `compute_work_hours`, L41 | `round()` is float/banker's rounding, not half-up. | 9630 s = 2.675 h: `round` gives 2.67, spec says 2.68. | `Decimal` arithmetic with `ROUND_HALF_UP` (`half_up`). |
| 4 | `compute_overtime`, L47 | No 30-minute minimum; any positive gap is returned. | Punch-out 18:40 on an 18:30 shift returns 10, spec says 0. | Return minutes only when `>= 30`. |
| 5 | `compute_overtime`, L46 | Shift end is always built on the record date, so an overnight shift's end (next day) is wrong; builds an aware datetime that cannot be subtracted from a naive one (`TypeError`). | 22:00-06:00 shift, out 06:40 next day: end computed on the same day, huge overtime or a crash. | `compute_overtime_minutes` adds a day for overnight shifts; every datetime is tz-aware UTC. |
| 6 | module setup, L22 | `MongoClient` is not `tz_aware`, so PyMongo returns naive datetimes; they get mixed with aware ones. | Any subtraction between a stored and a computed time. | `tz_aware=True`; conversion to and from epoch ms happens only at the API boundary. |
| 7 | whole file | No indexes and no unique constraints. | `explain` shows COLLSCAN; duplicates possible. | `ensure_indexes()` at startup (idempotent, logged on failure). |
| 8 | `health`, L72-74 | Always returns ok, never pings MongoDB. | Stop MongoDB: still 200. | Pings the database; 503 when it does not answer. |
| 9 | `EmployeeIn`, L53-60 | No validation: `emp_code` pattern, lengths, e-mail, shift format, real `joined_on`, `shift_start != shift_end`. | `emp_code="x"`, `shift_start="99:99"` are accepted with 201. | Pydantic constraints plus validators; invalid input is 422. |
| 10 | `create_employee`, L79-83 | Check-then-insert race, no unique index. | Parallel creates of one code all return 201. | Unique index on `emp_code`; `DuplicateKeyError` becomes 409. |
| 11 | `create_employee`, L82-85 | `datetime.now()` is naive local time; the response returns a datetime, not epoch ms. | `created_at` comes back as an ISO string. | UTC-aware time, returned as epoch milliseconds. |
| 12 | `list_employees`, L93 | `skip = page * page_size`. | Page 1 skips the first page. | `(page - 1) * page_size`. |
| 13 | `list_employees`, L94 | `total` counts the whole collection, ignoring the filter. | `?department=Sales` reports the total of all employees. | `count_documents(q)` with the same filter. |
| 14 | `list_employees`, L95 | No sort; unbounded and unvalidated `page`/`page_size`. | Unstable pages; `page=-1` gives a negative skip (500). | Sort by `emp_code`; `page >= 1`, `1 <= page_size <= 100` (422). |
| 15 | `PunchInIn`, L63-66 | `punched_at` is lax (accepts strings, whole floats, seconds-looking values); `status` is any string. | `punched_at=1783311000` (seconds) is accepted; `status="ABSENT"` is accepted. | Strict int in `[1e11, 4102444800000]`; `status` limited to PRESENT/WFH/ON_DUTY. |
| 16 | `punch_in`, L101, L113 | Unknown employee gives `emp = None`, then `emp["shift_start"]` raises. | `POST` with an unknown `emp_code` returns 500. | `get_employee` raises 404 first. |
| 17 | `punch_in`, L102-103 | `fromtimestamp` is server-local and naive; `date` is the local date, not the IST date; `if body.punched_at` treats a falsy value as absent. | UTC server, punch-in at 02:00 IST gets the previous date. | `attendance_date` (R1); truncation to the second; explicit `is not None`. |
| 18 | `punch_in`, L104-105 | Check-then-insert race. | Parallel identical punch-ins produce several 201s and duplicate records. | Unique `(emp_code, date)` index; `DuplicateKeyError` becomes 409. |
| 19 | `punch_in`, L119-121 | Returns an `id` (the ObjectId) and raw datetimes. | Response has `id` and ISO times. | `record_out` returns the contract shape only. |
| 20 | `list_attendance`, L144-150 | Loads the whole collection, sorts and slices in Python. | Slow and memory-hungry at 100k records. | `find().sort().skip().limit()` backed by an index, plus `count_documents`. |
| 21 | `list_attendance`, L145 | Sorted by `date` only, no `emp_code` tiebreak. | Unstable pages. | Sort `date` desc, `emp_code` asc. |
| 22 | `list_attendance`, L136-147 | No validation of dates, `status`, `page`, `page_size`; no `date_from > date_to` check. | `date_from=2026-13-01` is accepted; `page=0` slices with negative bounds. | Pattern and calendar checks, enum for `status`, bounds on paging, 422 on a reversed range. |
| 23 | `list_attendance`, L148-150 | Exposes `id`, raw datetimes; legacy records without `half_day`/`history` stay incomplete. | Sample document 9 comes back without those fields. | `record_out` applies the documented defaults. |
| 24 | L154-162 | Seven endpoints missing. | 404 on punch-out, PATCH, analytics, explain. | Implemented. |

**Examined and left alone:** `load_dotenv()` defaults to `override=False`, so real environment variables win as required. `model_dump()` matches the pydantic v2 pin. `status_code=201` on the two POSTs is right. Comparing `YYYY-MM-DD` strings with `$gte/$lte` is valid. The localhost/`attendance_db` fallbacks are defaults only; the environment overrides them.

**Not fixed:** `README.md` (starter) refers to `.env.example`, which the kit does not contain; it is not needed.
