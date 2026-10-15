# Employee Attendance & Analytics API

A REST API built using Python, FastAPI, and MongoDB to manage employee attendance, punch-in/punch-out records, attendance corrections, and analytics.

## Tech Stack
- Python
- FastAPI
- MongoDB
- PyMongo
- Uvicorn

## Features
- Employee attendance punch-in and punch-out
- Duplicate punch-in prevention
- Attendance regularization with audit history
- Monthly employee attendance analytics
- Department-wise attendance summaries and trends
- Late-arrival leaderboard
- MongoDB query plan analysis

## Project Structure
```text
candidate_kit/
├── app/
│   └── main.py
├── requirements.txt
├── README.md
├── REVIEW.md
├── DECISIONS.md
├── DATA_MODEL.md
├── openapi.yaml
└── sample_seed.py
```

## Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Start MongoDB and configure the MongoDB connection using the environment variables supported by the application.

4. Start the API:

```bash
python -m uvicorn app.main:app --port 8000
```

5. Open the interactive API documentation:

http://127.0.0.1:8000/docs

## Testing
Use Swagger UI to test the health check, attendance, employee monthly analytics, department summary, late-arrival leaderboard, department trend, and query-plan endpoints.

## Notes
Attendance calculations depend on the configured shift timings, attendance rules, and recorded punch timestamps. Manual corrections should include a reason and preserve the previous values in the audit history.
