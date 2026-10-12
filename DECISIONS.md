# DECISIONS.md

1. **Indexes.** Two unique indexes are guarantees: `employees.emp_code`, and `attendance_logs(emp_code, date)` (one record per day; also serves PATCH and the monthly summary). `(date desc, emp_code)` serves list order and month scans; `(emp_code, punch_in desc)` finds the record to punch out; `(status, date, emp_code)` serves the status filter. On `employees`: `(department, emp_code)`, `(department, joined_on)` and `(joined_on)` for listing and headcount. I rejected an index on `late_minutes`: the month range already narrows the scan.

2. **Punch-in race.** Both requests validate, compute the same IST date and insert. The unique `(emp_code, date)` index lets exactly one insert win (201); the other raises `DuplicateKeyError`, which I turn into 409. There is no read before the write.

3. **Ties.** I use `$rank` (1, 2, 2, 4) and apply `limit` after ranking, so everyone tied at the cutoff is returned, even if that exceeds `limit`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and `$lookup`s that month's logs; an employee without logs keeps an empty result and still adds one to the department count.

5. **100x data.** Replace skip/limit paging with keyset paging, and keep a pre-aggregated monthly rollup updated on each write, instead of summing raw logs on every request.
