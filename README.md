# Employee Attendance API

This is a backend API for an Employee Attendance Management System, built using Python, FastAPI, and MongoDB.

## Project Status (Partial Submission)
This project is a partial submission. 

**What is completed:**
* **Database Setup:** Connected to MongoDB with timezone-aware configurations.
* **Business Rules (Step 4):** Pure, testable functions for calculating late minutes, overtime, attendance dates, and half-days.
* **Data Validation (Step 5):** Strict Pydantic models for `EmployeeIn`, `PunchInIn`, `PunchOutIn`, and `RegularizeIn`.
* **Employee Endpoints (Step 6):** `POST /employees` and `GET /employees` endpoints, including unique database indexing on startup using FastAPI's `lifespan`.

**What is missing:**
* `POST /attendance/punch-in` and `POST /attendance/punch-out`
* `POST /attendance/regularize`
* Analytics pipelines (Leaderboard and Trends)

## How to Run the Project
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
