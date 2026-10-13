import random
from collections import defaultdict
from datetime import datetime, time, timedelta

import pytest

from app.scheduler import (
    MIN_SESSION_MINUTES,
    Busy,
    Preferences,
    TaskInput,
    Window,
    free_intervals,
    plan,
    round_up,
)

MONDAY = datetime(2026, 10, 12, 8, 0)  # a Monday
EVENINGS = [Window(d, time(18, 0), time(21, 0)) for d in range(7)]  # 3 h every day
PREFS = Preferences(max_session_minutes=50, break_minutes=10, max_daily_minutes=240)


def test_fixture_date_is_monday():
    assert MONDAY.weekday() == 0


def task(id, minutes, deadline_days, priority=2, difficulty=3, hour=23):
    deadline = datetime.combine(MONDAY.date() + timedelta(days=deadline_days), time(hour, 0))
    return TaskInput(id=id, title=f"T{id}", remaining_minutes=minutes, deadline=deadline,
                     priority=priority, difficulty=difficulty)


def minutes_for(result, task_id):
    return sum(s.minutes for s in result.sessions if s.task_id == task_id)


# --- core constraints ------------------------------------------------------------


def test_sessions_stay_inside_availability():
    result = plan([task(1, 300, 6)], EVENINGS, PREFS, MONDAY)
    assert result.sessions
    for s in result.sessions:
        assert s.start.time() >= time(18, 0) and s.end.time() <= time(21, 0)
        assert s.start.date() == s.end.date()


def test_no_overlaps_and_breaks_between_sessions():
    result = plan([task(1, 200, 6), task(2, 200, 6)], EVENINGS, PREFS, MONDAY)
    sessions = sorted(result.sessions, key=lambda s: s.start)
    for a, b in zip(sessions, sessions[1:]):
        assert a.end <= b.start
        if a.start.date() == b.start.date():
            assert b.start - a.end >= timedelta(minutes=PREFS.break_minutes)


def test_long_task_is_split_into_capped_sessions():
    result = plan([task(1, 170, 6)], EVENINGS, PREFS, MONDAY)
    assert [s.minutes for s in result.sessions] == [50, 50, 50, 20]
    assert minutes_for(result, 1) == 170
    assert not result.unscheduled


def test_daily_limit_is_respected():
    prefs = Preferences(max_session_minutes=60, break_minutes=0, max_daily_minutes=90)
    result = plan([task(1, 300, 6)], EVENINGS, prefs, MONDAY)
    per_day = defaultdict(int)
    for s in result.sessions:
        per_day[s.start.date()] += s.minutes
    assert max(per_day.values()) == 90


def test_daily_limit_counts_already_studied_time():
    studied = Busy(datetime(2026, 10, 12, 9, 0), datetime(2026, 10, 12, 12, 0), study_minutes=180)
    prefs = Preferences(max_session_minutes=60, break_minutes=0, max_daily_minutes=200)
    result = plan([task(1, 60, 6)], EVENINGS, prefs, MONDAY, busy=[studied])
    today = [s for s in result.sessions if s.start.date() == MONDAY.date()]
    assert sum(s.minutes for s in today) == 20


def test_busy_time_is_not_reused():
    blocked = Busy(datetime(2026, 10, 12, 18, 0), datetime(2026, 10, 12, 19, 30))
    result = plan([task(1, 60, 6)], EVENINGS, PREFS, MONDAY, busy=[blocked])
    first = min(result.sessions, key=lambda s: s.start)
    assert first.start == datetime(2026, 10, 12, 19, 30)


def test_starts_from_now_rounded_up():
    now = datetime(2026, 10, 12, 18, 7, 30)
    result = plan([task(1, 30, 6)], EVENINGS, PREFS, now)
    assert result.sessions[0].start == datetime(2026, 10, 12, 18, 10)
    assert round_up(datetime(2026, 10, 12, 18, 10)) == datetime(2026, 10, 12, 18, 10)


# --- prioritisation ----------------------------------------------------------------


def test_earliest_deadline_first():
    later, sooner = task(1, 50, 5), task(2, 50, 2)
    result = plan([later, sooner], EVENINGS, PREFS, MONDAY)
    assert sorted(result.sessions, key=lambda s: s.start)[0].task_id == 2


def test_priority_then_difficulty_break_ties():
    low = task(1, 50, 3, priority=1)
    high = task(2, 50, 3, priority=3)
    result = plan([low, high], EVENINGS, PREFS, MONDAY)
    assert sorted(result.sessions, key=lambda s: s.start)[0].task_id == 2

    easy = task(3, 50, 3, priority=2, difficulty=1)
    hard = task(4, 50, 3, priority=2, difficulty=5)
    result = plan([easy, hard], EVENINGS, PREFS, MONDAY)
    assert sorted(result.sessions, key=lambda s: s.start)[0].task_id == 4


def test_reasons_explain_the_ranking():
    result = plan([task(1, 50, 5), task(2, 50, 2, priority=3)], EVENINGS, PREFS, MONDAY)
    assert result.reasons[2].startswith("Ranked #1")
    assert "high priority" in result.reasons[2]
    assert result.reasons[1].startswith("Ranked #2")


# --- deadlines and honest failure -------------------------------------------------


def test_no_session_ends_after_its_deadline():
    tight = task(1, 120, 0, hour=19)  # due today 19:00; only 18:00-19:00 is usable
    result = plan([tight], EVENINGS, PREFS, MONDAY)
    assert all(s.end <= tight.deadline for s in result.sessions)
    # 18:00-18:50, then the 10-minute break reaches the 19:00 deadline: nothing more fits.
    assert minutes_for(result, 1) == 50
    assert result.unscheduled[0].minutes == 70


def test_insufficient_time_is_reported_not_squeezed():
    big = task(1, 600, 1)  # 10 h due Tuesday 23:00, but only 2 evenings x 3 h exist
    result = plan([big], EVENINGS, PREFS, MONDAY)
    scheduled = minutes_for(result, 1)
    assert scheduled < 600
    [problem] = result.unscheduled
    assert problem.task_id == 1
    assert problem.minutes == 600 - scheduled
    assert "Not enough available study time" in problem.reason


def test_overdue_task_is_reported_and_not_scheduled():
    overdue = TaskInput(1, "late", 60, MONDAY - timedelta(hours=1))
    result = plan([overdue], EVENINGS, PREFS, MONDAY)
    assert result.sessions == []
    assert result.unscheduled[0].reason == "Deadline has passed (overdue)."


def test_deadline_beyond_horizon_is_deferred_not_an_error():
    far = task(1, 3000, 20)
    result = plan([far], EVENINGS, PREFS, MONDAY, horizon_days=7)
    assert result.unscheduled == []
    assert result.deferred[1] == 3000 - minutes_for(result, 1)


def test_no_availability_means_nothing_scheduled():
    result = plan([task(1, 60, 3)], [], PREFS, MONDAY)
    assert result.sessions == []
    assert result.unscheduled[0].minutes == 60


def test_short_gaps_are_not_used_for_tiny_sessions():
    window = [Window(0, time(18, 0), time(18, 10))]  # a 10-minute window
    result = plan([task(1, 60, 6)], window, PREFS, MONDAY)
    assert result.sessions == []
    # ...but a 10-minute remainder that finishes a task may use it.
    result = plan([task(2, 10, 6)], window, PREFS, MONDAY)
    assert [s.minutes for s in result.sessions] == [10]


def test_no_tiny_remainders_are_left_for_later():
    # Found via a screenshot: 75 min task with a 20-min fragment left a 5-min session on another day.
    windows = [Window(0, time(18, 0), time(19, 10)), Window(1, time(18, 0), time(21, 0))]
    result = plan([task(1, 75, 6)], windows, PREFS, MONDAY)
    assert all(s.minutes >= MIN_SESSION_MINUTES for s in result.sessions)
    assert minutes_for(result, 1) == 75


def test_session_extends_slightly_to_finish_a_task():
    result = plan([task(1, 55, 6)], EVENINGS, PREFS, MONDAY)
    assert [s.minutes for s in result.sessions] == [55]  # not 50 + 5


def test_completed_tasks_are_ignored():
    result = plan([task(1, 0, 3)], EVENINGS, PREFS, MONDAY)
    assert result.sessions == [] and result.unscheduled == [] and result.reasons == {}


def test_deterministic():
    tasks = [task(i, 40 + 13 * i, i % 5 + 1, priority=i % 3 + 1) for i in range(1, 8)]
    assert plan(tasks, EVENINGS, PREFS, MONDAY) == plan(tasks, EVENINGS, PREFS, MONDAY)


def test_free_intervals_subtract_busy_in_the_middle():
    busy = [Busy(datetime(2026, 10, 12, 19, 0), datetime(2026, 10, 12, 19, 30))]
    intervals = free_intervals([EVENINGS[0]], MONDAY, 1, busy)
    assert intervals == [
        (datetime(2026, 10, 12, 18, 0), datetime(2026, 10, 12, 19, 0)),
        (datetime(2026, 10, 12, 19, 30), datetime(2026, 10, 12, 21, 0)),
    ]


# --- randomised invariant check -----------------------------------------------------


@pytest.mark.parametrize("seed", range(300))
def test_random_plans_never_break_hard_constraints(seed):
    rng = random.Random(seed)
    windows = []
    for day in range(7):
        hour = rng.randint(6, 18)
        for _ in range(rng.randint(0, 2)):
            start = time(hour, rng.choice([0, 15, 30]))
            end_hour = min(hour + rng.randint(1, 3), 23)
            windows.append(Window(day, start, time(end_hour, 45)))
            hour = end_hour + 1
            if hour > 21:
                break
    prefs = Preferences(
        max_session_minutes=rng.choice([25, 45, 50, 90]),
        break_minutes=rng.choice([0, 5, 10, 15]),
        max_daily_minutes=rng.choice([60, 120, 240, 480]),
    )
    now = MONDAY + timedelta(minutes=rng.randint(0, 3 * 24 * 60))
    tasks = [
        TaskInput(i, f"T{i}", rng.randint(10, 600),
                  now + timedelta(hours=rng.randint(-5, 24 * 12)),
                  priority=rng.randint(1, 3), difficulty=rng.randint(1, 5))
        for i in range(rng.randint(0, 8))
    ]
    busy = [Busy(now + timedelta(hours=h), now + timedelta(hours=h, minutes=40), 40)
            for h in rng.sample(range(1, 100), 3)]

    result = plan(tasks, windows, prefs, now, busy=busy)
    by_id = {t.id: t for t in tasks}
    sessions = sorted(result.sessions, key=lambda s: s.start)

    for s in sessions:
        assert s.start >= now
        assert s.end <= by_id[s.task_id].deadline
        assert 0 < s.minutes <= prefs.max_session_minutes + MIN_SESSION_MINUTES - 1
        # Short sessions only when the task itself needed less than a minimum session.
        assert s.minutes >= MIN_SESSION_MINUTES or by_id[s.task_id].remaining_minutes < MIN_SESSION_MINUTES
        assert any(w.weekday == s.start.weekday() and w.start <= s.start.time()
                   and s.end.time() <= w.end and s.start.date() == s.end.date() for w in windows)
        assert all(s.end <= b.start or s.start >= b.end for b in busy)
    for a, b in zip(sessions, sessions[1:]):
        assert a.end <= b.start  # no overlaps
        if a.start.date() == b.start.date() and b.start - a.end < timedelta(minutes=prefs.break_minutes):
            pytest.fail("break too short")

    per_day = defaultdict(int)
    for b in busy:
        per_day[b.start.date()] += b.study_minutes
    for s in sessions:
        per_day[s.start.date()] += s.minutes
    for day, total in per_day.items():
        studied_busy = sum(b.study_minutes for b in busy if b.start.date() == day)
        assert total - studied_busy <= max(0, prefs.max_daily_minutes - studied_busy)

    # Every minute is accounted for: scheduled + unscheduled + deferred == needed.
    unscheduled = {u.task_id: u.minutes for u in result.unscheduled}
    for t in tasks:
        accounted = (sum(s.minutes for s in sessions if s.task_id == t.id)
                     + unscheduled.get(t.id, 0) + result.deferred.get(t.id, 0))
        assert accounted == t.remaining_minutes, t
