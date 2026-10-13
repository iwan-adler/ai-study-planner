"""Business logic between the database and the pure scheduler."""

from __future__ import annotations

import json
import logging
import time as clock
from collections import defaultdict
from datetime import datetime, time, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import models, scheduler

logger = logging.getLogger("study_planner")

STUDIED = ("completed", "partial")


# --- small helpers ----------------------------------------------------------------------


def get_profile(db: Session) -> models.Profile:
    profile = db.get(models.Profile, 1)
    if profile is None:
        profile = models.Profile(id=1)
        db.add(profile)
        db.flush()
    return profile


def studied_minutes(task: models.Task) -> int:
    return sum(s.actual_minutes or 0 for s in task.sessions if s.status in STUDIED)


def scheduled_minutes(task: models.Task, now: datetime) -> int:
    return sum(s.planned_minutes for s in task.sessions if s.status == "planned" and s.end_at > now)


def remaining_minutes(task: models.Task) -> int:
    return max(0, task.estimated_minutes - studied_minutes(task))


def task_view(task: models.Task, now: datetime) -> dict:
    return {
        "id": task.id,
        "subject_id": task.subject_id,
        "subject_name": task.subject.name,
        "title": task.title,
        "notes": task.notes,
        "estimated_minutes": task.estimated_minutes,
        "priority": task.priority,
        "difficulty": task.difficulty,
        "deadline": task.deadline,
        "completed": task.completed,
        "created_at": task.created_at,
        "studied_minutes": studied_minutes(task),
        "scheduled_minutes": scheduled_minutes(task, now),
        "remaining_minutes": 0 if task.completed else remaining_minutes(task),
        "overdue": not task.completed and task.deadline < now,
    }


def session_view(session: models.StudySession) -> dict:
    return {
        "id": session.id,
        "task_id": session.task_id,
        "task_title": session.task.title,
        "subject_name": session.task.subject.name,
        "start_at": session.start_at,
        "end_at": session.end_at,
        "planned_minutes": session.planned_minutes,
        "status": session.status,
        "actual_minutes": session.actual_minutes,
    }


def windows(db: Session) -> list[scheduler.Window]:
    rows = db.scalars(select(models.AvailabilityWindow)).all()
    return [scheduler.Window(w.weekday, w.start_time, w.end_time) for w in rows]


def free_minutes_before(db: Session, deadline: datetime, now: datetime) -> int:
    """Available study time between now and a deadline, ignoring other tasks."""
    days = max(1, (deadline.date() - now.date()).days + 1)
    total = 0
    for begin, end in scheduler.free_intervals(windows(db), scheduler.round_up(now), days, []):
        end = min(end, deadline)
        if end > begin:
            total += int((end - begin).total_seconds() // 60)
    return total


# --- planning ---------------------------------------------------------------------------


def replan(db: Session, now: datetime, horizon_days: int) -> dict:
    """Rebuild future planned sessions; never touches completed, partial or missed ones.

    Deleting every future *planned* session before planning again makes the
    operation idempotent: running it twice cannot create duplicate sessions.
    """
    started = clock.perf_counter()
    db.execute(
        delete(models.StudySession).where(
            models.StudySession.status == "planned", models.StudySession.start_at >= now
        )
    )
    db.expire_all()

    busy: list[scheduler.Busy] = []
    in_progress: dict[int, int] = defaultdict(int)
    today_start = datetime.combine(now.date(), time.min)
    for s in db.scalars(select(models.StudySession).where(models.StudySession.end_at > today_start)):
        if s.status == "planned":
            if s.end_at <= now:
                continue  # past and not yet reviewed: neither busy nor studied
            busy.append(scheduler.Busy(s.start_at, s.end_at, s.planned_minutes))  # in progress
            in_progress[s.task_id] += s.planned_minutes
        elif s.status in STUDIED:
            busy.append(scheduler.Busy(s.start_at, s.end_at, s.actual_minutes or 0))
        else:  # missed: the slot stays blocked, but no study time is counted
            busy.append(scheduler.Busy(s.start_at, s.end_at, 0))

    open_tasks = db.scalars(select(models.Task).where(models.Task.completed.is_(False))).unique().all()
    inputs = [
        scheduler.TaskInput(
            id=t.id,
            title=t.title,
            remaining_minutes=max(0, remaining_minutes(t) - in_progress[t.id]),
            deadline=t.deadline,
            priority=t.priority,
            difficulty=t.difficulty,
        )
        for t in open_tasks
    ]
    profile = get_profile(db)
    preferences = scheduler.Preferences(
        max_session_minutes=profile.max_session_minutes,
        break_minutes=profile.break_minutes,
        max_daily_minutes=profile.max_daily_minutes,
    )
    result = scheduler.plan(inputs, windows(db), preferences, now, busy, horizon_days)

    for s in result.sessions:
        db.add(models.StudySession(task_id=s.task_id, start_at=s.start, end_at=s.end))
    db.execute(delete(models.PlanReport))
    db.add(
        models.PlanReport(
            created_at=now,
            data=json.dumps(
                {
                    "unscheduled": [u.__dict__ for u in result.unscheduled],
                    "deferred": result.deferred,
                    "reasons": result.reasons,
                }
            ),
        )
    )
    db.commit()
    # New sessions were inserted by task_id, so already-loaded task.sessions lists are stale.
    db.expire_all()
    logger.info(
        "replan sessions=%d unscheduled=%d deferred=%d duration_ms=%.1f",
        len(result.sessions), len(result.unscheduled), len(result.deferred),
        (clock.perf_counter() - started) * 1000,
    )
    return plan_view(db, now, horizon_days)


def latest_report(db: Session) -> tuple[datetime | None, dict]:
    report = db.scalars(select(models.PlanReport).order_by(models.PlanReport.id.desc())).first()
    if report is None:
        return None, {"unscheduled": [], "deferred": {}, "reasons": {}}
    return report.created_at, json.loads(report.data)


def plan_view(db: Session, now: datetime, horizon_days: int) -> dict:
    generated_at, data = latest_report(db)
    titles = {t.id: t.title for t in db.scalars(select(models.Task)).unique()}
    start = datetime.combine(now.date(), time.min)
    end = start + timedelta(days=horizon_days)
    sessions = db.scalars(
        select(models.StudySession)
        .where(models.StudySession.start_at >= start, models.StudySession.start_at < end)
        .order_by(models.StudySession.start_at)
    ).unique().all()
    return {
        "generated_at": generated_at,
        "sessions": [session_view(s) for s in sessions],
        "unscheduled": [
            {**u, "task_title": titles[u["task_id"]]} for u in data["unscheduled"] if u["task_id"] in titles
        ],
        "deferred": [
            {"task_id": int(k), "task_title": titles[int(k)], "minutes": v}
            for k, v in data["deferred"].items() if int(k) in titles
        ],
        "reasons": [
            {"task_id": int(k), "task_title": titles[int(k)], "reason": v}
            for k, v in data["reasons"].items() if int(k) in titles
        ],
    }


# --- session check-ins ----------------------------------------------------------------


class CheckInError(ValueError):
    pass


def record_session(db: Session, session: models.StudySession, status: str, actual: int | None) -> None:
    if status == "partial":
        if actual is None:
            raise CheckInError("actual_minutes is required for a partial session")
        if actual >= session.planned_minutes:
            raise CheckInError(
                f"a partial session must be shorter than planned ({session.planned_minutes} min); "
                "mark it completed instead"
            )
    session.status = status
    session.actual_minutes = 0 if status == "missed" else (actual or session.planned_minutes)
    task = session.task
    if remaining_minutes(task) == 0:
        task.completed = True  # all estimated effort has been studied


# --- dashboard ----------------------------------------------------------------------------


def dashboard(db: Session, now: datetime, horizon_days: int) -> dict:
    today_start = datetime.combine(now.date(), time.min)
    tomorrow = today_start + timedelta(days=1)
    week_start = today_start - timedelta(days=now.weekday())
    week_end = week_start + timedelta(days=7)

    tasks = db.scalars(select(models.Task).order_by(models.Task.deadline)).unique().all()
    open_tasks = [t for t in tasks if not t.completed]
    sessions = db.scalars(
        select(models.StudySession).order_by(models.StudySession.start_at)
    ).unique().all()

    plan = plan_view(db, now, horizon_days)
    unscheduled_by_task = defaultdict(int)
    for u in plan["unscheduled"]:
        unscheduled_by_task[u["task_id"]] += u["minutes"]

    week_sessions = [s for s in sessions if week_start <= s.start_at < week_end]
    recent_missed = defaultdict(int)
    for s in sessions:
        if s.status == "missed" and now - timedelta(days=7) <= s.start_at <= now:
            recent_missed[s.task.subject_id] += 1

    subjects = []
    for subject in db.scalars(select(models.Subject).order_by(models.Subject.name)):
        mine = [t for t in open_tasks if t.subject_id == subject.id]
        reasons = []
        overdue = sum(1 for t in mine if t.deadline < now)
        if overdue:
            reasons.append(f"{overdue} overdue task(s)")
        short = sum(unscheduled_by_task[t.id] for t in mine if t.deadline >= now)
        if short:
            reasons.append(f"{short} min can't fit before deadlines")
        if recent_missed[subject.id] >= 2:
            reasons.append(f"{recent_missed[subject.id]} sessions missed in the last 7 days")
        subjects.append(
            {
                "subject_id": subject.id,
                "subject_name": subject.name,
                "open_tasks": len(mine),
                "remaining_minutes": sum(remaining_minutes(t) for t in mine),
                "needs_attention": bool(reasons),
                "reasons": reasons,
            }
        )
    subjects.sort(key=lambda s: (not s["needs_attention"], -s["remaining_minutes"]))

    return {
        "now": now,
        "today": [session_view(s) for s in sessions if today_start <= s.start_at < tomorrow],
        "to_review": [
            session_view(s) for s in sessions
            if s.status == "planned" and s.end_at <= now and not s.task.completed
        ][-10:],
        "upcoming": [task_view(t, now) for t in open_tasks if now <= t.deadline < now + timedelta(days=7)],
        "overdue": [task_view(t, now) for t in open_tasks if t.deadline < now],
        "tasks_total": len(tasks),
        "tasks_completed": len(tasks) - len(open_tasks),
        "week": {
            "week_start": week_start,
            "studied_minutes": sum(s.actual_minutes or 0 for s in week_sessions if s.status in STUDIED),
            "scheduled_minutes": sum(s.planned_minutes for s in week_sessions),
            "completed_sessions": sum(1 for s in week_sessions if s.status == "completed"),
            "partial_sessions": sum(1 for s in week_sessions if s.status == "partial"),
            "missed_sessions": sum(1 for s in week_sessions if s.status == "missed"),
        },
        "subjects": subjects,
        "unscheduled": plan["unscheduled"],
    }
