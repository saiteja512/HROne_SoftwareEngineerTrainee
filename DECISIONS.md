# Design Decisions

## 1. FastAPI
FastAPI was selected to implement REST endpoints with request validation and automatically generated OpenAPI documentation.

## 2. MongoDB
MongoDB stores employee and attendance documents. Its aggregation framework supports analytics without requiring all attendance records to be transferred into application memory.

## 3. Attendance Records
Attendance records include employee identification, date, attendance status, punch timestamps, work hours, late minutes, overtime minutes, and half-day information.

## 4. Duplicate Punch Prevention
The API rejects duplicate punch-in requests for the same employee and attendance date to reduce inconsistent records.

## 5. Attendance Regularization
Manual corrections require a reason and preserve the original and updated values in an audit history to support traceability.

## 6. Analytics
MongoDB aggregation pipelines calculate employee monthly metrics, department summaries, department trends, and late-arrival rankings.

## 7. Query Plan Inspection
An administrative endpoint exposes query-plan information to help assess index usage and query performance.

## 8. Future Improvements
Potential improvements include authentication and role-based authorization, automated integration tests, stronger concurrency controls, structured logging, and monitoring.
