"""Database tables (SQLAlchemy ORM). Times are naive local time: a single-user, local app."""

from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Profile(Base):
    """Exactly one row (id = 1): the student's goals and study preferences."""

    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="Student")
    goals: Mapped[str] = mapped_column(Text, default="")
    max_session_minutes: Mapped[int] = mapped_column(default=50)
    break_minutes: Mapped[int] = mapped_column(default=10)
    max_daily_minutes: Mapped[int] = mapped_column(default=240)


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)


class AvailabilityWindow(Base):
    __tablename__ = "availability"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6"),
        CheckConstraint("start_time < end_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    weekday: Mapped[int]  # 0 = Monday
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("estimated_minutes > 0"),
        CheckConstraint("priority BETWEEN 1 AND 3"),
        CheckConstraint("difficulty BETWEEN 1 AND 5"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String(120))
    notes: Mapped[str] = mapped_column(Text, default="")
    estimated_minutes: Mapped[int]
    priority: Mapped[int] = mapped_column(default=2)
    difficulty: Mapped[int] = mapped_column(default=3)
    deadline: Mapped[datetime] = mapped_column(DateTime)
    completed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    subject: Mapped[Subject] = relationship(lazy="joined")
    sessions: Mapped[list[StudySession]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )


class StudySession(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("start_at < end_at"),
        CheckConstraint("status IN ('planned', 'completed', 'partial', 'missed')"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(10), default="planned")
    actual_minutes: Mapped[int | None]  # set when completed / partial (0 when missed)

    task: Mapped[Task] = relationship(back_populates="sessions", lazy="joined")

    @property
    def planned_minutes(self) -> int:
        return int((self.end_at - self.start_at).total_seconds() // 60)


class PlanReport(Base):
    """The latest planning run's findings (unscheduled work, ranking reasons)."""

    __tablename__ = "plan_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    data: Mapped[str] = mapped_column(Text)  # JSON
