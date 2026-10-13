"""Request/response models. Invalid input is rejected with 422 and a readable message."""

from __future__ import annotations

from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Stripped(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


def _naive_local(value: datetime) -> datetime:
    """Store local wall-clock time; convert timezone-aware input to local first."""
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.replace(second=0, microsecond=0)


# --- profile -----------------------------------------------------------------------


class ProfileIn(Stripped):
    name: str = Field(default="Student", min_length=1, max_length=100)
    goals: str = Field(default="", max_length=2000)
    max_session_minutes: int = Field(default=50, ge=15, le=180)
    break_minutes: int = Field(default=10, ge=0, le=60)
    max_daily_minutes: int = Field(default=240, ge=30, le=960)

    @model_validator(mode="after")
    def _session_fits_in_day(self) -> ProfileIn:
        if self.max_session_minutes > self.max_daily_minutes:
            raise ValueError("max_session_minutes cannot exceed max_daily_minutes")
        return self


class ProfileOut(ProfileIn):
    model_config = ConfigDict(from_attributes=True)


# --- subjects ----------------------------------------------------------------------


class SubjectIn(Stripped):
    name: str = Field(min_length=1, max_length=80)


class SubjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


# --- availability ------------------------------------------------------------------


class WindowIn(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0 = Monday ... 6 = Sunday")
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def _valid_range(self) -> WindowIn:
        start = self.start_time.hour * 60 + self.start_time.minute
        end = self.end_time.hour * 60 + self.end_time.minute
        if end <= start:
            raise ValueError("end_time must be after start_time (windows cannot cross midnight)")
        if end - start < 15:
            raise ValueError("a window must be at least 15 minutes long")
        return self


class AvailabilityIn(BaseModel):
    windows: list[WindowIn] = Field(max_length=50)

    @model_validator(mode="after")
    def _no_overlaps(self) -> AvailabilityIn:
        by_day: dict[int, list[WindowIn]] = {}
        for window in self.windows:
            by_day.setdefault(window.weekday, []).append(window)
        for day, windows in by_day.items():
            windows.sort(key=lambda w: w.start_time)
            for a, b in zip(windows, windows[1:]):
                if b.start_time < a.end_time:
                    raise ValueError(
                        f"windows overlap on weekday {day}: "
                        f"{a.start_time:%H:%M}-{a.end_time:%H:%M} and {b.start_time:%H:%M}-{b.end_time:%H:%M}"
                    )
        return self


class WindowOut(WindowIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


# --- tasks -------------------------------------------------------------------------


class TaskCreate(Stripped):
    subject_id: int
    title: str = Field(min_length=1, max_length=120, examples=["Revise binary trees"])
    notes: str = Field(default="", max_length=1000)
    estimated_minutes: int = Field(ge=15, le=6000, description="Total effort, 15 min to 100 h")
    priority: int = Field(default=2, ge=1, le=3, description="1 low, 2 medium, 3 high")
    difficulty: int = Field(default=3, ge=1, le=5)
    deadline: datetime

    @field_validator("deadline")
    @classmethod
    def _deadline(cls, value: datetime) -> datetime:
        return _naive_local(value)


class TaskUpdate(Stripped):
    subject_id: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=120)
    notes: str | None = Field(default=None, max_length=1000)
    estimated_minutes: int | None = Field(default=None, ge=15, le=6000)
    priority: int | None = Field(default=None, ge=1, le=3)
    difficulty: int | None = Field(default=None, ge=1, le=5)
    deadline: datetime | None = None
    completed: bool | None = None

    @field_validator("deadline")
    @classmethod
    def _deadline(cls, value: datetime | None) -> datetime | None:
        return _naive_local(value) if value else value


class TaskOut(BaseModel):
    id: int
    subject_id: int
    subject_name: str
    title: str
    notes: str
    estimated_minutes: int
    priority: int
    difficulty: int
    deadline: datetime
    completed: bool
    created_at: datetime
    studied_minutes: int
    scheduled_minutes: int  # future planned sessions
    remaining_minutes: int
    overdue: bool


# --- sessions and plans ----------------------------------------------------------------


class SessionOut(BaseModel):
    id: int
    task_id: int
    task_title: str
    subject_name: str
    start_at: datetime
    end_at: datetime
    planned_minutes: int
    status: str
    actual_minutes: int | None


class SessionUpdate(BaseModel):
    status: Literal["completed", "partial", "missed"]
    actual_minutes: int | None = Field(
        default=None, ge=1, le=600,
        description="Minutes actually studied. Optional for completed (defaults to planned), "
                    "required for partial, ignored for missed.",
    )


class TaskMinutes(BaseModel):
    task_id: int
    task_title: str
    minutes: int


class UnscheduledOut(TaskMinutes):
    reason: str


class TaskReason(BaseModel):
    task_id: int
    task_title: str
    reason: str


class PlanOut(BaseModel):
    generated_at: datetime | None
    sessions: list[SessionOut]
    unscheduled: list[UnscheduledOut]
    deferred: list[TaskMinutes]
    reasons: list[TaskReason]


class WeekProgress(BaseModel):
    week_start: datetime
    studied_minutes: int
    scheduled_minutes: int
    completed_sessions: int
    partial_sessions: int
    missed_sessions: int


class SubjectAttention(BaseModel):
    subject_id: int
    subject_name: str
    open_tasks: int
    remaining_minutes: int
    needs_attention: bool
    reasons: list[str]


class DashboardOut(BaseModel):
    now: datetime
    today: list[SessionOut]
    to_review: list[SessionOut]
    upcoming: list[TaskOut]
    overdue: list[TaskOut]
    tasks_total: int
    tasks_completed: int
    week: WeekProgress
    subjects: list[SubjectAttention]
    unscheduled: list[UnscheduledOut]


# --- AI ------------------------------------------------------------------------------


class AIStatus(BaseModel):
    enabled: bool
    model: str | None
    note: str


class TaskNote(BaseModel):
    task_id: int
    task_title: str
    note: str


class ExplanationOut(BaseModel):
    source: Literal["ai", "rule-based"]
    summary: str
    task_notes: list[TaskNote]
    revision_tips: list[str]
    warnings: list[str]


class SubtaskProposal(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    estimated_minutes: int = Field(ge=15, le=240)


class BreakdownOut(BaseModel):
    source: Literal["ai", "rule-based"]
    task_id: int
    task_title: str
    subtasks: list[SubtaskProposal]
    total_minutes: int
    free_minutes_before_deadline: int
    fits_before_deadline: bool
    warnings: list[str]


class SplitIn(BaseModel):
    subtasks: list[SubtaskProposal] = Field(min_length=2, max_length=8)
