from datetime import datetime, timedelta

import pytest

from tests.conftest import MONDAY_8AM


def add_task(client, subject_id, **overrides):
    payload = {
        "subject_id": subject_id,
        "title": "Revise binary trees",
        "estimated_minutes": 120,
        "priority": 2,
        "difficulty": 3,
        "deadline": "2026-10-16T23:00",
        **overrides,
    }
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def sessions(client, task_id=None, status=None):
    items = client.get("/api/plan").json()["sessions"]
    return [s for s in items if (task_id is None or s["task_id"] == task_id)
            and (status is None or s["status"] == status)]


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


# --- profile & availability --------------------------------------------------------------


def test_profile_defaults_and_update(client):
    assert client.get("/api/profile").json()["max_session_minutes"] == 50
    updated = client.put("/api/profile", json={"name": "Asha", "goals": "Pass DSA", "max_session_minutes": 45,
                                               "break_minutes": 5, "max_daily_minutes": 180}).json()
    assert updated["name"] == "Asha" and updated["max_session_minutes"] == 45
    assert client.get("/api/profile").json()["goals"] == "Pass DSA"


@pytest.mark.parametrize("changes", [
    {"max_session_minutes": 10}, {"max_session_minutes": 181}, {"break_minutes": -1},
    {"max_daily_minutes": 20}, {"max_session_minutes": 120, "max_daily_minutes": 60}, {"name": " "},
])
def test_invalid_profile(client, changes):
    assert client.put("/api/profile", json=changes).status_code == 422


def test_availability_can_differ_per_day(client):
    windows = [
        {"weekday": 0, "start_time": "18:00", "end_time": "20:00"},
        {"weekday": 0, "start_time": "07:00", "end_time": "08:30"},
        {"weekday": 5, "start_time": "10:00", "end_time": "14:00"},
    ]
    saved = client.put("/api/availability", json={"windows": windows}).json()
    assert [(w["weekday"], w["start_time"]) for w in saved] == [(0, "07:00:00"), (0, "18:00:00"), (5, "10:00:00")]
    # Replacing the set is how updates work
    client.put("/api/availability", json={"windows": windows[:1]})
    assert len(client.get("/api/availability").json()) == 1


@pytest.mark.parametrize("window", [
    {"weekday": 7, "start_time": "18:00", "end_time": "19:00"},
    {"weekday": 0, "start_time": "19:00", "end_time": "18:00"},
    {"weekday": 0, "start_time": "18:00", "end_time": "18:10"},
    {"weekday": 0, "start_time": "25:00", "end_time": "26:00"},
])
def test_invalid_windows(client, window):
    assert client.put("/api/availability", json={"windows": [window]}).status_code == 422


def test_overlapping_windows_rejected(client):
    response = client.put("/api/availability", json={"windows": [
        {"weekday": 2, "start_time": "18:00", "end_time": "20:00"},
        {"weekday": 2, "start_time": "19:30", "end_time": "21:00"},
    ]})
    assert response.status_code == 422
    assert "overlap" in response.text


# --- subjects ----------------------------------------------------------------------------


def test_subjects(client, setup):
    assert client.post("/api/subjects", json={"name": "Data Structures"}).status_code == 409
    other = client.post("/api/subjects", json={"name": "Maths"}).json()
    assert [s["name"] for s in client.get("/api/subjects").json()] == ["Data Structures", "Maths"]
    add_task(client, setup)
    assert client.delete(f"/api/subjects/{setup}").status_code == 409  # has tasks
    assert client.delete(f"/api/subjects/{other['id']}").status_code == 204
    assert client.delete("/api/subjects/999").status_code == 404


# --- tasks --------------------------------------------------------------------------------


def test_create_task_schedules_it_inside_availability(client, setup):
    task = add_task(client, setup)
    assert task["scheduled_minutes"] == 120
    assert task["remaining_minutes"] == 120 and task["studied_minutes"] == 0
    for s in sessions(client, task["id"]):
        assert ts(s["start_at"]).hour >= 18 and ts(s["end_at"]) <= ts(s["end_at"]).replace(hour=21, minute=0)
        assert s["planned_minutes"] <= 50


@pytest.mark.parametrize("overrides", [
    {"estimated_minutes": 10}, {"estimated_minutes": 6001}, {"priority": 4}, {"difficulty": 0},
    {"title": "  "}, {"deadline": "not a date"}, {"deadline": "2026-10-12T07:00"},  # in the past
    {"subject_id": 999},
])
def test_invalid_task_rejected(client, setup, overrides):
    payload = {"subject_id": setup, "title": "x", "estimated_minutes": 60, "deadline": "2026-10-16T23:00", **overrides}
    assert client.post("/api/tasks", json=payload).status_code == 422
    assert client.get("/api/tasks").json() == []


def test_update_task_replans(client, setup):
    task = add_task(client, setup)
    updated = client.patch(f"/api/tasks/{task['id']}", json={"estimated_minutes": 60}).json()
    assert updated["scheduled_minutes"] == 60
    assert sum(s["planned_minutes"] for s in sessions(client, task["id"])) == 60
    assert client.patch(f"/api/tasks/{task['id']}", json={"deadline": "2026-10-01T10:00"}).status_code == 422
    assert client.patch("/api/tasks/999", json={"title": "x"}).status_code == 404


def test_completing_a_task_removes_its_future_sessions(client, setup):
    task = add_task(client, setup)
    done = client.patch(f"/api/tasks/{task['id']}", json={"completed": True}).json()
    assert done["completed"] and done["remaining_minutes"] == 0
    assert sessions(client, task["id"]) == []


def test_delete_task(client, setup):
    task = add_task(client, setup)
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 204
    assert sessions(client) == []
    assert client.get(f"/api/tasks/{task['id']}").status_code == 404


# --- scheduling through the API -------------------------------------------------------------


def test_no_overlapping_sessions_across_tasks(client, setup):
    for i in range(4):
        add_task(client, setup, title=f"T{i}", estimated_minutes=150, deadline=f"2026-10-1{4 + i}T23:00")
    items = sorted(sessions(client), key=lambda s: s["start_at"])
    for a, b in zip(items, items[1:]):
        assert ts(a["end_at"]) <= ts(b["start_at"])
        if ts(a["start_at"]).date() == ts(b["start_at"]).date():
            assert ts(b["start_at"]) - ts(a["end_at"]) >= timedelta(minutes=10)


def test_earlier_deadline_is_scheduled_first(client, setup):
    later = add_task(client, setup, title="Later", deadline="2026-10-17T23:00")
    sooner = add_task(client, setup, title="Sooner", deadline="2026-10-13T23:00")
    first = min(sessions(client), key=lambda s: s["start_at"])
    assert first["task_id"] == sooner["id"]
    reasons = {r["task_id"]: r["reason"] for r in client.get("/api/plan").json()["reasons"]}
    assert reasons[sooner["id"]].startswith("Ranked #1") and reasons[later["id"]].startswith("Ranked #2")


def test_replanning_twice_creates_no_duplicates(client, setup):
    add_task(client, setup, estimated_minutes=300)
    first = [(s["task_id"], s["start_at"]) for s in client.post("/api/plan").json()["sessions"]]
    second = [(s["task_id"], s["start_at"]) for s in client.post("/api/plan").json()["sessions"]]
    assert first == second
    assert len(first) == len(set(first))


def test_not_enough_time_is_reported_honestly(client, setup):
    task = add_task(client, setup, estimated_minutes=900, deadline="2026-10-13T23:00")  # 15 h in 2 evenings
    plan = client.get("/api/plan").json()
    scheduled = sum(s["planned_minutes"] for s in plan["sessions"])
    [problem] = plan["unscheduled"]
    assert problem["task_id"] == task["id"] and problem["minutes"] == 900 - scheduled
    dashboard = client.get("/api/dashboard").json()
    subject = dashboard["subjects"][0]
    assert subject["needs_attention"] and "can't fit" in subject["reasons"][0]


# --- check-ins and adaptive replanning --------------------------------------------------------


def test_completed_session_counts_and_is_preserved(client, setup, clock):
    task = add_task(client, setup)
    first = sessions(client, task["id"])[0]
    clock.now = ts(first["end_at"]) + timedelta(minutes=5)

    plan = client.patch(f"/api/sessions/{first['id']}", json={"status": "completed"}).json()
    kept = [s for s in plan["sessions"] if s["id"] == first["id"]]
    assert kept and kept[0]["status"] == "completed" and kept[0]["actual_minutes"] == 50

    refreshed = client.get(f"/api/tasks/{task['id']}").json()
    assert refreshed["studied_minutes"] == 50 and refreshed["remaining_minutes"] == 70
    assert refreshed["scheduled_minutes"] == 70  # only the remaining work is planned again


def test_partial_session_requires_and_validates_minutes(client, setup, clock):
    task = add_task(client, setup)
    first = sessions(client, task["id"])[0]
    url = f"/api/sessions/{first['id']}"
    assert client.patch(url, json={"status": "partial"}).status_code == 422
    assert client.patch(url, json={"status": "partial", "actual_minutes": 50}).status_code == 422
    assert client.patch(url, json={"status": "bogus"}).status_code == 422
    assert client.patch(url, json={"status": "partial", "actual_minutes": 20}).status_code == 200
    assert client.get(f"/api/tasks/{task['id']}").json()["remaining_minutes"] == 100
    assert client.patch("/api/sessions/999", json={"status": "missed"}).status_code == 404


def test_missed_session_is_rescheduled_elsewhere(client, setup):
    task = add_task(client, setup)
    first = sessions(client, task["id"])[0]
    plan = client.patch(f"/api/sessions/{first['id']}", json={"status": "missed"}).json()
    planned = [s for s in plan["sessions"] if s["task_id"] == task["id"] and s["status"] == "planned"]
    assert sum(s["planned_minutes"] for s in planned) == 120  # all work still planned
    for s in planned:  # ...but not on top of the missed slot
        assert ts(s["end_at"]) <= ts(first["start_at"]) or ts(s["start_at"]) >= ts(first["end_at"])


def test_finishing_all_work_completes_the_task(client, setup, clock):
    task = add_task(client, setup, estimated_minutes=50)
    [only] = sessions(client, task["id"])
    clock.now = ts(only["end_at"])
    client.patch(f"/api/sessions/{only['id']}", json={"status": "completed"})
    assert client.get(f"/api/tasks/{task['id']}").json()["completed"] is True


def test_time_passing_keeps_history_and_flags_unreviewed_sessions(client, setup, clock):
    task = add_task(client, setup, estimated_minutes=200)
    first = sessions(client, task["id"])[0]
    clock.now = MONDAY_8AM + timedelta(days=1)  # Tuesday 08:00; Monday's sessions were never marked
    client.post("/api/plan")

    dashboard = client.get("/api/dashboard").json()
    assert first["id"] in [s["id"] for s in dashboard["to_review"]]
    # Unreviewed time is not counted as studied, so all 200 min are planned again from Tuesday on.
    future = [s for s in sessions(client, task["id"], "planned") if ts(s["start_at"]) >= clock.now]
    assert sum(s["planned_minutes"] for s in future) == 200


def test_in_progress_session_survives_replanning(client, setup, clock):
    task = add_task(client, setup)
    first = sessions(client, task["id"])[0]
    clock.now = ts(first["start_at"]) + timedelta(minutes=20)  # halfway through
    client.post("/api/plan")
    assert first["id"] in [s["id"] for s in sessions(client, task["id"])]
    planned = sum(s["planned_minutes"] for s in sessions(client, task["id"], "planned"))
    assert planned == 120  # the in-progress session plus the rest, nothing doubled


# --- dashboard ----------------------------------------------------------------------------------


def test_dashboard(client, setup, clock):
    soon = add_task(client, setup, title="Soon", deadline="2026-10-13T23:00", estimated_minutes=60)
    add_task(client, setup, title="Later", deadline="2026-10-30T23:00", estimated_minutes=60)
    first = sessions(client, soon["id"])[0]
    client.patch(f"/api/sessions/{first['id']}", json={"status": "partial", "actual_minutes": 30})

    clock.now = datetime(2026, 10, 14, 9, 0)  # "Soon" is overdue: only 30 of 60 min done
    data = client.get("/api/dashboard").json()
    assert [t["title"] for t in data["overdue"]] == ["Soon"]
    assert data["tasks_total"] == 2
    assert data["week"]["studied_minutes"] == 30 and data["week"]["partial_sessions"] == 1
    assert data["subjects"][0]["needs_attention"]


# --- splitting ------------------------------------------------------------------------------------


def test_split_replaces_task_with_subtasks(client, setup):
    task = add_task(client, setup, estimated_minutes=120, priority=3)
    created = client.post(f"/api/tasks/{task['id']}/split", json={"subtasks": [
        {"title": "Tree traversals", "estimated_minutes": 60},
        {"title": "BST insert/delete", "estimated_minutes": 60},
    ]})
    assert created.status_code == 201
    titles = [t["title"] for t in client.get("/api/tasks").json()]
    assert sorted(titles) == ["BST insert/delete", "Tree traversals"]
    assert all(t["priority"] == 3 and t["deadline"] == task["deadline"] for t in created.json())


def test_split_rejected_after_study_started(client, setup):
    task = add_task(client, setup)
    first = sessions(client, task["id"])[0]
    client.patch(f"/api/sessions/{first['id']}", json={"status": "partial", "actual_minutes": 10})
    response = client.post(f"/api/tasks/{task['id']}/split", json={"subtasks": [
        {"title": "a", "estimated_minutes": 30}, {"title": "b", "estimated_minutes": 30}]})
    assert response.status_code == 409


def test_data_persists_across_restarts(make_client, setup, client):
    add_task(client, setup)
    second = make_client()  # same database file, new app instance
    assert [t["title"] for t in second.get("/api/tasks").json()] == ["Revise binary trees"]
    assert len(second.get("/api/plan").json()["sessions"]) == 3
