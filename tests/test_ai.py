import json

import httpx
import pytest

from app.ai import AIError, OpenAICompatibleProvider, parse_json_object, rule_based_breakdown
from app.config import Settings, load_settings
from tests.test_api import add_task


class FakeProvider:
    """Returns a fixed reply (or raises) and records prompts."""

    name = "fake-model"

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.prompts = reply, error, []

    def complete(self, system, user):
        self.prompts.append(user)
        if self.error:
            raise self.error
        return self.reply


def ai_setup(make_client, provider):
    client = make_client(provider)
    client.put("/api/availability", json={"windows": [
        {"weekday": d, "start_time": "18:00", "end_time": "21:00"} for d in range(7)]})
    subject = client.post("/api/subjects", json={"name": "DSA"}).json()["id"]
    task = add_task(client, subject, estimated_minutes=180, difficulty=4)
    return client, task


# --- without a provider: rule-based, clearly labelled --------------------------------------


def test_status_and_explain_without_provider(client, setup):
    status = client.get("/api/ai/status").json()
    assert status["enabled"] is False and "rule-based" in status["note"]
    add_task(client, setup)
    explanation = client.post("/api/ai/explain").json()
    assert explanation["source"] == "rule-based"
    assert "ordered by deadline" in explanation["summary"]
    assert explanation["task_notes"][0]["note"].startswith("Ranked #1")


def test_breakdown_without_provider_is_rule_based_and_not_saved(client, setup):
    task = add_task(client, setup, estimated_minutes=200)
    result = client.post(f"/api/ai/breakdown/{task['id']}").json()
    assert result["source"] == "rule-based"
    assert [s["estimated_minutes"] for s in result["subtasks"]] == [67, 67, 66]
    assert result["total_minutes"] == 200
    assert len(client.get("/api/tasks").json()) == 1  # proposal only


def test_rule_based_breakdown_bounds():
    assert len(rule_based_breakdown("x", 30)) == 2
    assert len(rule_based_breakdown("x", 5000)) == 8
    assert all(p["estimated_minutes"] >= 15 for p in rule_based_breakdown("x", 31))


# --- with a provider: validated, never trusted blindly ------------------------------------------


def test_ai_explanation_is_validated_against_real_tasks(make_client):
    provider = FakeProvider()
    client, task = ai_setup(make_client, provider)
    provider.reply = "Sure! ```json\n" + json.dumps({
        "summary": "Focus on trees first.",
        "task_notes": [{"task_id": task["id"], "note": "Start early."},
                       {"task_id": 999, "note": "Invented task"}],
        "revision_tips": [f"tip {i}" for i in range(9)],
    }) + "\n```"
    result = client.post("/api/ai/explain").json()
    assert result["source"] == "ai"
    assert [n["task_id"] for n in result["task_notes"]] == [task["id"]]  # invented task dropped
    assert any("999" in w for w in result["warnings"])
    assert len(result["revision_tips"]) == 5
    sent = json.loads(provider.prompts[0])
    assert sent["tasks"][0]["id"] == task["id"]  # the model saw only real data


@pytest.mark.parametrize("reply", ["no json here", "{broken", "[1, 2]", json.dumps({"summary": ""})])
def test_malformed_ai_explanation_falls_back(make_client, reply):
    client, _ = ai_setup(make_client, FakeProvider(reply=reply))
    result = client.post("/api/ai/explain").json()
    assert result["source"] == "rule-based"
    assert result["warnings"][0].startswith("AI answer rejected")


def test_provider_failure_falls_back(make_client):
    client, _ = ai_setup(make_client, FakeProvider(error=AIError("timeout")))
    result = client.post("/api/ai/explain").json()
    assert result["source"] == "rule-based" and "timeout" in result["warnings"][0]


def test_ai_breakdown_valid(make_client):
    reply = json.dumps({"subtasks": [
        {"title": "Traversals", "estimated_minutes": 60},
        {"title": "BST operations", "estimated_minutes": 70},
        {"title": "Practice problems", "estimated_minutes": 50},
    ]})
    client, task = ai_setup(make_client, FakeProvider(reply=reply))
    result = client.post(f"/api/ai/breakdown/{task['id']}").json()
    assert result["source"] == "ai" and result["total_minutes"] == 180
    assert result["fits_before_deadline"] is True
    assert len(client.get("/api/tasks").json()) == 1  # still only a proposal


@pytest.mark.parametrize("subtasks", [
    [{"title": "only one", "estimated_minutes": 180}],                                   # too few
    [{"title": f"s{i}", "estimated_minutes": 20} for i in range(9)],                     # too many
    [{"title": "a", "estimated_minutes": 30}, {"title": "b", "estimated_minutes": 30}],  # total far off
    [{"title": "a", "estimated_minutes": 5}, {"title": "b", "estimated_minutes": 175}],  # below 15 min
    [{"title": "", "estimated_minutes": 90}, {"title": "b", "estimated_minutes": 90}],   # empty title
])
def test_invalid_ai_breakdown_falls_back(make_client, subtasks):
    client, task = ai_setup(make_client, FakeProvider(reply=json.dumps({"subtasks": subtasks})))
    result = client.post(f"/api/ai/breakdown/{task['id']}").json()
    assert result["source"] == "rule-based"
    assert result["total_minutes"] == 180
    assert result["warnings"][0].startswith("AI answer rejected")


def test_breakdown_warns_when_it_cannot_fit(make_client):
    client, _ = ai_setup(make_client, None)
    subject = client.get("/api/subjects").json()[0]["id"]
    tight = add_task(client, subject, estimated_minutes=600, deadline="2026-10-12T21:00")  # one evening
    result = client.post(f"/api/ai/breakdown/{tight['id']}").json()
    assert result["fits_before_deadline"] is False
    assert result["free_minutes_before_deadline"] == 180
    assert "only 180 min" in result["warnings"][-1]


def test_breakdown_of_tiny_or_missing_task(client, setup):
    task = add_task(client, setup, estimated_minutes=20)
    assert client.post(f"/api/ai/breakdown/{task['id']}").status_code == 409
    assert client.post("/api/ai/breakdown/999").status_code == 404


# --- provider and config -------------------------------------------------------------------------


def test_openai_compatible_provider_request_and_errors():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    settings = Settings(ai_api_key="sk-secret", ai_base_url="http://llm.test/v1", ai_model="m1")
    provider = OpenAICompatibleProvider(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    assert provider.complete("sys", "user") == "hello"
    assert seen["url"] == "http://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-secret"
    assert seen["body"]["model"] == "m1" and seen["body"]["messages"][1]["content"] == "user"

    failing = OpenAICompatibleProvider(
        settings, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))))
    with pytest.raises(AIError) as excinfo:
        failing.complete("s", "u")
    assert "sk-secret" not in str(excinfo.value)


def test_parse_json_object():
    assert parse_json_object('text {"a": 1} more') == {"a": 1}
    with pytest.raises(AIError):
        parse_json_object("nothing")


def test_ai_enabled_rules():
    assert not Settings().ai_enabled
    assert not Settings(ai_api_key="k").ai_enabled  # no model
    assert Settings(ai_api_key="k", ai_model="m").ai_enabled
    assert Settings(ai_base_url="http://127.0.0.1:11434/v1", ai_model="llama3.2").ai_enabled  # Ollama, no key


def test_invalid_horizon_setting(monkeypatch):
    monkeypatch.setenv("PLAN_HORIZON_DAYS", "99")
    with pytest.raises(ValueError, match="PLAN_HORIZON_DAYS"):
        load_settings()
