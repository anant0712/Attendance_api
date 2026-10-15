# Review of the starter app

I went through the starter `app/main.py` and found a number of issues in the helper logic and API endpoints. The current version of the file already includes fixes for these problems, and the notes below describe the original defects and how they were resolved.

| # | Area | What was wrong | Why it mattered | Fix |
|---|---|---|---|---|
| 1 | `GET /employees?page=1` skips the first 20 rows | `GET /employees?page=1&page_size=20` | Skips the first 20 records and shows the next batch of 6 records instead of the first 20. |
| 2 |`POST /attendance/punch-in` unknown emp_code | `{"emp_code": "string", "punched_at": 100000000000, "status": "PRESENT"}` | Expected a 500 error, but actually received a proper 404 Not Found. |
| 3 | `GET /health` does not check DB status | `GET /health` | Returns `200 OK` with `{"status": "ok"}` even if MongoDB is stopped, because it is a dummy endpoint. |
| 4 | Send `punched_at` in seconds instead of milliseconds | `{"emp_code": "E001", "punched_at": 1729051801, "status": "PRESENT"}` | Expected it to crash or log a 1970 date, but it actually returned a 422 Unprocessable Content, successfully enforcing a minimum value of 1000000000000. |
| 5 |`round_half_up` | Binary floating-point noise caused exact ties to round down (e.g., `8.124999999999998` rounded to `8.12` instead of `8.13`). | Running `pytest` resulted in an `AssertionError` for `test_round_half_up`. | Used Python's `decimal.Decimal` module with the `ROUND_HALF_UP` flag, and pre-rounded to 10 places to eliminate float noise. |
| 6 | `dt_to_ms` | Naive datetime objects were being treated inconsistently (often as local time instead of strictly UTC). | `test_dt_to_ms_treats_naive_as_utc` failed because the timestamps did not match. | Updated the function to explicitly force all naive datetimes to UTC before converting to epoch milliseconds. |
| 7 | `attendance_date` | Overnight shifts (e.g., 22:00 to 06:00) assigned early morning punches to the new calendar day instead of the previous day's shift. | `test_attendance_date` failed when testing a 6:00 AM punch-out for an overnight shift. | Added an `is_overnight` check. If true and the punch is before `shift_end`, subtracted `timedelta(days=1)` from the attendance date. |
| 8 | `derive_fields` | The function skipped computing `work_hours` and `overtime_minutes` for employees working from home, as it only checked for the "PRESENT" status. | `test_derive_fields_for_each_situation` failed because the returned dictionary for the WFH scenario had missing/empty values. | Changed the condition from `if status == "PRESENT":` to `if status in ("PRESENT", "WFH"):`. |
| 9 |`punch_in` endpoint / `attendance_date` helper | The API contract requires `punched_at` to be an integer (epoch ms), but the starter helper functions expect a datetime object, causing a crash. | Hitting the `POST /attendance/punch-in` endpoint with a valid integer payload resulted in a 500 Internal Server Error (`AttributeError: 'int' object has no attribute 'tzinfo'`). | Converted the integer timestamp to a timezone-aware UTC datetime object using `datetime.fromtimestamp(payload.punched_at / 1000.0, tz=timezone.utc)` before passing it to the helper. |




