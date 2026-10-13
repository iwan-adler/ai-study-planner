from app.config import Settings
from app.database import Base, make_engine, make_session_factory
from app.seed import TASKS, seed
from tests.conftest import MONDAY_8AM


def test_seed_creates_a_plannable_demo_once(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    assert seed(factory, MONDAY_8AM, 7) is True
    assert seed(factory, MONDAY_8AM, 7) is False  # never mixes into existing data


def test_seeded_app_has_a_schedule(make_client, tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    seed(make_session_factory(engine), MONDAY_8AM, Settings().horizon_days)

    client = make_client()
    assert len(client.get("/api/tasks").json()) == len(TASKS)
    plan = client.get("/api/plan").json()
    assert plan["sessions"] and plan["reasons"]
