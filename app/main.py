"""FastAPI application: JSON API under /api, web UI at /."""


import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import ai, models, schemas, services
from app.config import Settings, load_settings
from app.database import Base, make_engine, make_session_factory

STATIC_DIR = Path(__file__).parent.parent / "static"
MIN_SPLIT_MINUTES = 30  # two subtasks of at least 15 minutes


def real_now() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def create_app(settings: Settings | None = None, provider: ai.AIProvider | None = None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    app = FastAPI(
        title="AI Study Planner",
        description="Plan study sessions around deadlines and availability, then adapt as you go.",
        version="0.1.0",
    )
    app.state.settings = settings
    app.state.ai_provider = provider if provider is not None else ai.provider_from_settings(settings)

    def get_db() -> Iterator[Session]:
        with session_factory() as db:
            yield db

    def get_now() -> datetime:
        return real_now()

    app.state.get_now = get_now  # tests override this dependency with a fixed clock

    DB = Annotated[Session, Depends(get_db)]
    Now = Annotated[datetime, Depends(get_now)]
    horizon = settings.horizon_days

    def replan(db: Session, now: datetime) -> dict:
        return services.replan(db, now, horizon)

    def task_or_404(db: Session, task_id: int) -> models.Task:
        task = db.get(models.Task, task_id)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Task {task_id} not found")
        return task

    def require_subject(db: Session, subject_id: int) -> None:
        if db.get(models.Subject, subject_id) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Subject {subject_id} does not exist")

    def future_deadline(deadline: datetime, now: datetime) -> None:
        if deadline <= now:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "deadline must be in the future")

    # --- service --------------------------------------------------------------------

    @app.get("/api/health", tags=["service"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- profile & availability ----------------------------------------------------------

    @app.get("/api/profile", response_model=schemas.ProfileOut, tags=["setup"])
    def get_profile(db: DB) -> Any:
        profile = services.get_profile(db)
        db.commit()
        return profile

    @app.put("/api/profile", response_model=schemas.ProfileOut, tags=["setup"])
    def update_profile(payload: schemas.ProfileIn, db: DB, now: Now) -> Any:
        profile = services.get_profile(db)
        for field, value in payload.model_dump().items():
            setattr(profile, field, value)
        db.commit()
        replan(db, now)
        return profile

    @app.get("/api/availability", response_model=list[schemas.WindowOut], tags=["setup"])
    def get_availability(db: DB) -> Any:
        return db.scalars(
            select(models.AvailabilityWindow).order_by(
                models.AvailabilityWindow.weekday, models.AvailabilityWindow.start_time
            )
        ).all()

    @app.put("/api/availability", response_model=list[schemas.WindowOut], tags=["setup"])
    def set_availability(payload: schemas.AvailabilityIn, db: DB, now: Now) -> Any:
        """Replace the whole weekly availability (windows may differ per day)."""
        db.query(models.AvailabilityWindow).delete()
        for window in payload.windows:
            db.add(models.AvailabilityWindow(**window.model_dump()))
        db.commit()
        replan(db, now)
        return get_availability(db)

    # --- subjects -------------------------------------------------------------------------

    @app.get("/api/subjects", response_model=list[schemas.SubjectOut], tags=["setup"])
    def list_subjects(db: DB) -> Any:
        return db.scalars(select(models.Subject).order_by(models.Subject.name)).all()

    @app.post("/api/subjects", response_model=schemas.SubjectOut, status_code=201, tags=["setup"])
    def add_subject(payload: schemas.SubjectIn, db: DB) -> Any:
        subject = models.Subject(name=payload.name)
        db.add(subject)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, f"Subject '{payload.name}' already exists")
        return subject

    @app.delete("/api/subjects/{subject_id}", status_code=204, tags=["setup"])
    def delete_subject(subject_id: int, db: DB) -> None:
        subject = db.get(models.Subject, subject_id)
        if subject is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Subject {subject_id} not found")
        if db.scalars(select(models.Task.id).where(models.Task.subject_id == subject_id)).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "Subject has tasks; delete or move them first")
        db.delete(subject)
        db.commit()

    # --- tasks ------------------------------------------------------------------------------

    @app.get("/api/tasks", response_model=list[schemas.TaskOut], tags=["tasks"])
    def list_tasks(db: DB, now: Now, include_completed: bool = True) -> Any:
        query = select(models.Task).order_by(models.Task.completed, models.Task.deadline)
        if not include_completed:
            query = query.where(models.Task.completed.is_(False))
        return [services.task_view(t, now) for t in db.scalars(query).unique()]

    @app.post("/api/tasks", response_model=schemas.TaskOut, status_code=201, tags=["tasks"])
    def create_task(payload: schemas.TaskCreate, db: DB, now: Now) -> Any:
        require_subject(db, payload.subject_id)
        future_deadline(payload.deadline, now)
        task = models.Task(**payload.model_dump(), created_at=now)
        db.add(task)
        db.commit()
        replan(db, now)
        return services.task_view(task_or_404(db, task.id), now)

    @app.get("/api/tasks/{task_id}", response_model=schemas.TaskOut, tags=["tasks"])
    def get_task(task_id: int, db: DB, now: Now) -> Any:
        return services.task_view(task_or_404(db, task_id), now)

    @app.patch("/api/tasks/{task_id}", response_model=schemas.TaskOut, tags=["tasks"])
    def update_task(task_id: int, payload: schemas.TaskUpdate, db: DB, now: Now) -> Any:
        task = task_or_404(db, task_id)
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("subject_id") is not None:
            require_subject(db, changes["subject_id"])
        if changes.get("deadline") is not None and changes["deadline"] != task.deadline:
            future_deadline(changes["deadline"], now)
        for field, value in changes.items():
            if value is not None:
                setattr(task, field, value)
        db.commit()
        replan(db, now)
        return services.task_view(task_or_404(db, task_id), now)

    @app.delete("/api/tasks/{task_id}", status_code=204, tags=["tasks"])
    def delete_task(task_id: int, db: DB, now: Now) -> None:
        db.delete(task_or_404(db, task_id))
        db.commit()
        replan(db, now)

    # --- schedule ----------------------------------------------------------------------------

    @app.get("/api/plan", response_model=schemas.PlanOut, tags=["schedule"])
    def get_plan(db: DB, now: Now) -> Any:
        """Sessions from the start of today across the planning horizon, plus the last report."""
        return services.plan_view(db, now, horizon)

    @app.post("/api/plan", response_model=schemas.PlanOut, tags=["schedule"])
    def regenerate_plan(db: DB, now: Now) -> Any:
        """Rebuild future planned sessions. Completed, partial and missed sessions are kept."""
        return replan(db, now)

    @app.patch("/api/sessions/{session_id}", response_model=schemas.PlanOut, tags=["schedule"])
    def check_in(session_id: int, payload: schemas.SessionUpdate, db: DB, now: Now) -> Any:
        """Mark a session completed, partial or missed; the rest of the plan adapts."""
        session = db.get(models.StudySession, session_id)
        if session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Session {session_id} not found")
        try:
            services.record_session(db, session, payload.status, payload.actual_minutes)
        except services.CheckInError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
        db.commit()
        return replan(db, now)

    @app.get("/api/dashboard", response_model=schemas.DashboardOut, tags=["schedule"])
    def get_dashboard(db: DB, now: Now) -> Any:
        return services.dashboard(db, now, horizon)

    # --- AI (optional) ----------------------------------------------------------------------

    @app.get("/api/ai/status", response_model=schemas.AIStatus, tags=["ai"])
    def ai_status(request: Request) -> Any:
        provider = request.app.state.ai_provider
        if provider is None:
            return {"enabled": False, "model": None,
                    "note": "No AI provider configured; explanations and breakdowns are rule-based."}
        return {"enabled": True, "model": provider.name,
                "note": "AI suggestions are validated against your tasks and never saved automatically."}

    @app.post("/api/ai/explain", response_model=schemas.ExplanationOut, tags=["ai"])
    def explain_plan(request: Request, db: DB, now: Now) -> Any:
        tasks = [services.task_view(t, now) for t in db.scalars(select(models.Task)).unique()]
        facts = ai.explain_facts(tasks, services.plan_view(db, now, horizon), horizon, now)
        return ai.explain(request.app.state.ai_provider, facts)

    @app.post("/api/ai/breakdown/{task_id}", response_model=schemas.BreakdownOut, tags=["ai"])
    def breakdown_task(task_id: int, request: Request, db: DB, now: Now) -> Any:
        """Propose subtasks. Nothing is saved; use POST /api/tasks/{id}/split to apply."""
        task = services.task_view(task_or_404(db, task_id), now)
        if task["completed"] or task["remaining_minutes"] < MIN_SPLIT_MINUTES:
            raise HTTPException(status.HTTP_409_CONFLICT, "Task is completed or too small to break down")
        free = services.free_minutes_before(db, task["deadline"], now)
        return ai.breakdown(request.app.state.ai_provider, task, free)

    @app.post("/api/tasks/{task_id}/split", response_model=list[schemas.TaskOut], status_code=201, tags=["tasks"])
    def split_task(task_id: int, payload: schemas.SplitIn, db: DB, now: Now) -> Any:
        """Replace a not-yet-started task with the given subtasks (same subject, deadline, priority)."""
        task = task_or_404(db, task_id)
        if services.studied_minutes(task) > 0:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "Task already has studied time; create subtasks manually instead")
        if task.completed or task.deadline <= now:
            raise HTTPException(status.HTTP_409_CONFLICT, "Only open tasks with a future deadline can be split")
        created = [
            models.Task(subject_id=task.subject_id, title=sub.title, notes=f"Part of: {task.title}",
                        estimated_minutes=sub.estimated_minutes, priority=task.priority,
                        difficulty=task.difficulty, deadline=task.deadline, created_at=now)
            for sub in payload.subtasks
        ]
        db.add_all(created)
        db.delete(task)
        db.commit()  # one transaction: either all subtasks exist and the original is gone, or nothing changed
        replan(db, now)
        return [services.task_view(task_or_404(db, t.id), now) for t in created]

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


# No module-level app: importing this file has no side effects (no database file is
# created). Run with:  uvicorn app.main:create_app --factory
