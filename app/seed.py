"""Fill an empty database with sample data (invented, no personal information).

Usage:  python -m app.seed
Deadlines are relative to today so the demo always looks current.
"""

import sys
from datetime import datetime, time, timedelta

from sqlalchemy import select

from app import models, services
from app.config import load_settings
from app.database import Base, make_engine, make_session_factory
from app.main import real_now

SUBJECTS = ["Data Structures", "Linear Algebra", "Machine Learning", "Technical Writing"]

# weekday: list of (start, end)
AVAILABILITY = {
    0: [("18:00", "21:00")], 1: [("18:00", "21:00")], 2: [("17:00", "19:00")],
    3: [("18:00", "21:00")], 4: [("16:00", "18:00")],
    5: [("10:00", "13:00"), ("15:00", "18:00")], 6: [("10:00", "12:30")],
}

# (subject, title, minutes, priority, difficulty, days from today, hour)
TASKS = [
    ("Data Structures", "Binary search trees: insert, delete, traversals", 180, 3, 4, 3, 23),
    ("Data Structures", "Solve 10 graph BFS/DFS problems", 240, 2, 4, 8, 23),
    ("Linear Algebra", "Eigenvalues problem set", 150, 3, 3, 2, 9),
    ("Linear Algebra", "Revise matrix decompositions (LU, QR)", 90, 1, 3, 10, 23),
    ("Machine Learning", "Implement logistic regression from scratch", 200, 2, 5, 6, 23),
    ("Machine Learning", "Read chapter on bias-variance trade-off", 60, 1, 2, 5, 23),
    ("Technical Writing", "Draft lab report introduction", 75, 2, 2, 4, 17),
]


def _t(value: str) -> time:
    return time.fromisoformat(value)


def seed(session_factory, now: datetime, horizon_days: int) -> bool:
    with session_factory() as db:
        if db.scalars(select(models.Task.id)).first() or db.scalars(select(models.Subject.id)).first():
            return False
        profile = services.get_profile(db)
        profile.name, profile.goals = "Demo Student", "Keep up with weekly coursework and prepare for mid-terms."
        subjects = {name: models.Subject(name=name) for name in SUBJECTS}
        db.add_all(subjects.values())
        for weekday, windows in AVAILABILITY.items():
            for start, end in windows:
                db.add(models.AvailabilityWindow(weekday=weekday, start_time=_t(start), end_time=_t(end)))
        db.flush()
        for subject, title, minutes, priority, difficulty, days, hour in TASKS:
            deadline = datetime.combine(now.date() + timedelta(days=days), time(hour, 0))
            db.add(models.Task(subject_id=subjects[subject].id, title=title, estimated_minutes=minutes,
                               priority=priority, difficulty=difficulty, deadline=deadline, created_at=now))
        db.commit()
        services.replan(db, now, horizon_days)
    return True


def main() -> int:
    settings = load_settings()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    if seed(make_session_factory(engine), real_now(), settings.horizon_days):
        print(f"Sample data added: {len(SUBJECTS)} subjects, {len(TASKS)} tasks, weekly availability.")
    else:
        print("Database already has data; nothing added.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
