"""Deterministic study scheduler.

Pure functions only: no database, no clock, no AI. Everything the algorithm
needs is passed in, so every rule can be tested with exact inputs.

Algorithm (greedy Earliest-Deadline-First):
1. Turn the weekly availability into concrete free intervals for the planning
   horizon, starting at `now` and removing time that is already busy.
2. Walk through the free intervals in time order. At each point, pick the task
   whose deadline is soonest (ties: higher priority, then higher difficulty)
   that still needs time and can finish a session before its deadline.
3. Place one session no longer than the max session length, the free interval,
   the daily limit or the deadline allow; then insert a break. Sessions are
   adjusted so no task is left with a remainder under 15 minutes (a session may
   run up to 14 minutes over the cap to finish a task).
4. Whatever does not fit before a deadline is reported as unscheduled instead
   of being squeezed into an impossible timetable.

Why EDF: for one person working on one thing at a time, processing work in
deadline order minimises the worst lateness, so if any order can meet every
deadline, deadline order can. Priority only breaks ties, which is the main
trade-off: an important task with a later deadline can wait behind a less
important task that is due sooner.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

MIN_SESSION_MINUTES = 15  # shorter fragments are not worth a context switch
SLOT_GRANULARITY_MINUTES = 5

PRIORITY_LABELS = {1: "low", 2: "medium", 3: "high"}


@dataclass(frozen=True)
class Window:
    """A weekly availability window, e.g. Monday 18:00-21:00 (weekday 0 = Monday)."""

    weekday: int
    start: time
    end: time


@dataclass(frozen=True)
class Preferences:
    max_session_minutes: int = 50
    break_minutes: int = 10
    max_daily_minutes: int = 240


@dataclass(frozen=True)
class TaskInput:
    id: int
    title: str
    remaining_minutes: int
    deadline: datetime
    priority: int = 2  # 1 low, 2 medium, 3 high
    difficulty: int = 3  # 1-5


@dataclass(frozen=True)
class Busy:
    """Time that must not be scheduled over (kept sessions).

    `study_minutes` counts towards that day's study limit (0 for a missed slot).
    """

    start: datetime
    end: datetime
    study_minutes: int = 0


@dataclass(frozen=True)
class PlannedSession:
    task_id: int
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class Unscheduled:
    task_id: int
    minutes: int
    reason: str


@dataclass
class PlanResult:
    sessions: list[PlannedSession] = field(default_factory=list)
    unscheduled: list[Unscheduled] = field(default_factory=list)  # deadline at risk
    deferred: dict[int, int] = field(default_factory=dict)  # minutes left for later plans
    reasons: dict[int, str] = field(default_factory=dict)  # why each task got its rank


def round_up(moment: datetime, minutes: int = SLOT_GRANULARITY_MINUTES) -> datetime:
    moment = moment.replace(second=0, microsecond=0)
    overflow = moment.minute % minutes
    return moment + timedelta(minutes=minutes - overflow) if overflow else moment


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def free_intervals(
    windows: list[Window], start: datetime, horizon_days: int, busy: list[Busy]
) -> list[tuple[datetime, datetime]]:
    """Concrete free time from `start` for `horizon_days` days, minus busy time."""
    intervals: list[tuple[datetime, datetime]] = []
    for offset in range(horizon_days):
        day = start.date() + timedelta(days=offset)
        for window in windows:
            if window.weekday != day.weekday():
                continue
            begin = max(datetime.combine(day, window.start), start)
            end = datetime.combine(day, window.end)
            if begin < end:
                intervals.append((begin, end))

    for block in sorted(busy, key=lambda b: b.start):
        carved: list[tuple[datetime, datetime]] = []
        for begin, end in intervals:
            if block.end <= begin or block.start >= end:
                carved.append((begin, end))
                continue
            if begin < block.start:
                carved.append((begin, block.start))
            if block.end < end:
                carved.append((block.end, end))
        intervals = carved
    return sorted(intervals)


def _rank_key(task: TaskInput) -> tuple:
    return (task.deadline, -task.priority, -task.difficulty, task.id)


def _reason(rank: int, task: TaskInput) -> str:
    return (
        f"Ranked #{rank}: deadline {task.deadline:%a %d %b %H:%M}, "
        f"{PRIORITY_LABELS.get(task.priority, task.priority)} priority, "
        f"difficulty {task.difficulty}/5, {task.remaining_minutes} min left. "
        "Earlier deadlines are scheduled first; priority and difficulty break ties."
    )


def plan(
    tasks: list[TaskInput],
    windows: list[Window],
    preferences: Preferences,
    now: datetime,
    busy: list[Busy] | None = None,
    horizon_days: int = 7,
) -> PlanResult:
    busy = busy or []
    start = round_up(now)
    horizon_end = datetime.combine(start.date() + timedelta(days=horizon_days), time.min)
    result = PlanResult()

    daily_used: dict[date, int] = defaultdict(int)
    for block in busy:
        daily_used[block.start.date()] += block.study_minutes

    remaining: dict[int, int] = {}
    ordered = sorted((t for t in tasks if t.remaining_minutes > 0), key=_rank_key)
    for rank, task in enumerate(ordered, start=1):
        result.reasons[task.id] = _reason(rank, task)
        if task.deadline <= start:
            result.unscheduled.append(
                Unscheduled(task.id, task.remaining_minutes, "Deadline has passed (overdue).")
            )
        else:
            remaining[task.id] = task.remaining_minutes

    candidates = [t for t in ordered if t.id in remaining]
    gap = timedelta(minutes=preferences.break_minutes)

    for begin, end in free_intervals(windows, start, horizon_days, busy):
        cursor = begin
        while cursor < end:
            day = cursor.date()
            cap_left = preferences.max_daily_minutes - daily_used[day]
            if cap_left < MIN_SESSION_MINUTES:
                break  # daily limit reached; later intervals today will also stop here

            chosen: tuple[TaskInput, int] | None = None
            for task in candidates:
                need = remaining[task.id]
                if need <= 0 or task.deadline <= cursor:
                    continue
                room = min(_minutes(end - cursor), cap_left, _minutes(task.deadline - cursor))
                length = min(preferences.max_session_minutes, need, room)
                leftover = need - length
                if 0 < leftover < MIN_SESSION_MINUTES:
                    # Don't strand a pointless 5-minute remainder for a later day:
                    if need <= min(room, preferences.max_session_minutes + MIN_SESSION_MINUTES - 1):
                        length = need  # finish the task now with a slightly longer session
                    elif length - (MIN_SESSION_MINUTES - leftover) >= MIN_SESSION_MINUTES:
                        length -= MIN_SESSION_MINUTES - leftover  # leave a proper session for later
                    else:
                        continue  # this fragment doesn't suit this task; try the next one
                # Skip fragments too short to be useful, unless they are the task's last bit.
                if length >= MIN_SESSION_MINUTES or (length == need and length > 0):
                    chosen = (task, length)
                    break
            if chosen is None:
                break

            task, length = chosen
            session_end = cursor + timedelta(minutes=length)
            result.sessions.append(PlannedSession(task.id, cursor, session_end))
            remaining[task.id] -= length
            daily_used[day] += length
            cursor = session_end + gap

    for task in candidates:
        left = remaining[task.id]
        if left <= 0:
            continue
        if task.deadline <= horizon_end:
            result.unscheduled.append(
                Unscheduled(
                    task.id,
                    left,
                    f"Not enough available study time before the deadline: "
                    f"{left} of {task.remaining_minutes} min could not be placed.",
                )
            )
        else:
            result.deferred[task.id] = left
    return result
