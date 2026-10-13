from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

MONDAY_8AM = datetime(2026, 10, 12, 8, 0)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(MONDAY_8AM)


@pytest.fixture
def make_client(tmp_path, clock):
    """Build an isolated app (temporary SQLite file, fixed clock, optional fake AI)."""
    clients = []

    def factory(provider=None, db_name="test.db") -> TestClient:
        app = create_app(Settings(database_url=f"sqlite:///{tmp_path / db_name}"), provider=provider)
        app.dependency_overrides[app.state.get_now] = clock
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture
def setup(client):
    """Evenings 18:00-21:00 every day and one subject. Returns the subject id."""
    client.put("/api/availability", json={"windows": [
        {"weekday": d, "start_time": "18:00", "end_time": "21:00"} for d in range(7)
    ]})
    return client.post("/api/subjects", json={"name": "Data Structures"}).json()["id"]
