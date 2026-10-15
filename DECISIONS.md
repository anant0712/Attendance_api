# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code` to guarantee no duplicate employees exist. I also created a compound index on `attendance` for `[emp_code, punched_at]` to optimize chronological lookups and enforce punch uniqueness.

2. **Punch-in race.** Both identical requests hit the FastAPI endpoint and pass validation simultaneously. However, MongoDB's unique compound index acts as the tie-breaker. The first thread successfully inserts the document; the second throws a `DuplicateKeyError`, which the API converts into an HTTP 400 Bad Request.

3. **Ties.** *Not implemented in this partial submission.* If implemented, the leaderboard pipeline would apply a secondary sort on `emp_code` ascending to handle ties deterministically, rather than arbitrarily fluctuating.

4. **Headcount.** *Not implemented in this partial submission.* If implemented, the department summary would use a `$lookup` (Left Outer Join) starting from the `employees` collection to ensure all employees are grouped and counted, even with zero logs.

5. **One thing you would change.** To serve 100x the data, I would add pagination (`limit` and `skip`) to the `GET` endpoints to prevent memory exhaustion, and calculate the leaderboard asynchronously via a cron job, caching it in Redis.