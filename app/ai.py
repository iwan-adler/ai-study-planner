"""Optional AI assistance behind a provider interface, with rule-based fallbacks.

Rules this module enforces:
- The LLM only ever *proposes*. Nothing here writes to the database; turning a
  breakdown into real tasks is a separate, explicit user action (POST /split).
- Every AI answer is parsed as JSON and validated against the real tasks,
  deadlines and available time. Invalid parts are dropped with a warning, or
  the whole answer is replaced by the rule-based result.
- Responses say whether they came from "ai" or "rule-based", so the app never
  presents deterministic logic as AI.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime
from typing import Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.schemas import SubtaskProposal

logger = logging.getLogger("study_planner.ai")

MAX_TIPS = 5


class AIError(RuntimeError):
    """The provider failed or returned something unusable."""


class AIProvider(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str: ...


class OpenAICompatibleProvider:
    """Chat Completions over HTTP: OpenAI, Groq, Ollama (http://127.0.0.1:11434/v1), ..."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.name = settings.ai_model
        self._model = settings.ai_model
        self._url = f"{settings.ai_base_url or 'https://api.openai.com/v1'}/chat/completions"
        self._headers = {"Authorization": f"Bearer {settings.ai_api_key}"} if settings.ai_api_key else {}
        self._client = client or httpx.Client(timeout=settings.ai_timeout_seconds)

    def complete(self, system: str, user: str) -> str:
        started = time.perf_counter()
        try:
            response = self._client.post(
                self._url,
                headers=self._headers,
                json={
                    "model": self._model,
                    "temperature": 0.2,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                },
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            # Never include headers in logs or messages: they carry the API key.
            logger.warning("ai_request_failed model=%s error=%s", self._model, type(exc).__name__)
            raise AIError(f"AI provider request failed ({type(exc).__name__})") from exc
        logger.info("ai_request model=%s duration_ms=%.0f", self._model, (time.perf_counter() - started) * 1000)
        return text


def provider_from_settings(settings: Settings) -> AIProvider | None:
    return OpenAICompatibleProvider(settings) if settings.ai_enabled else None


def parse_json_object(text: str) -> dict:
    """Extract the first JSON object from a reply (models often wrap it in prose or ``` fences)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise AIError("AI reply contained no JSON object")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise AIError(f"AI reply was not valid JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise AIError("AI reply JSON must be an object")
    return data


# --- explain the plan -------------------------------------------------------------------

EXPLAIN_SYSTEM = """You are a study coach. You receive a student's real tasks and the schedule an
algorithm produced. Explain the plan and suggest a realistic revision approach.
Only refer to tasks by the task_id values given. Do not invent tasks, dates or study time.
Reply with JSON only:
{"summary": "<=600 chars", "task_notes": [{"task_id": <int>, "note": "<=300 chars"}],
 "revision_tips": ["<=200 chars", "... at most 5"]}"""


class _AIExplanation(BaseModel):
    summary: str = Field(min_length=1)
    task_notes: list[dict] = Field(default_factory=list)
    revision_tips: list[str] = Field(default_factory=list)


def rule_based_explanation(facts: dict) -> dict:
    tasks = facts["tasks"]
    unscheduled = {u["task_id"]: u for u in facts["unscheduled"]}
    if not tasks:
        summary = "No open tasks. Add tasks with deadlines and estimates to get a plan."
    else:
        first = tasks[0]
        summary = (
            f"{len(tasks)} open task(s), {sum(t['remaining_minutes'] for t in tasks)} min of work left, "
            f"{facts['scheduled_minutes']} min scheduled over the next {facts['horizon_days']} days. "
            f"Tasks are ordered by deadline (earliest first), then priority, then difficulty, "
            f"so '{first['title']}' comes first."
        )
        if unscheduled:
            summary += f" {len(unscheduled)} task(s) cannot be fully scheduled before their deadline."
    notes = [
        {"task_id": t["id"], "task_title": t["title"],
         "note": unscheduled[t["id"]]["reason"] if t["id"] in unscheduled else t["reason"]}
        for t in tasks
    ]
    tips = []
    if unscheduled:
        tips.append("Add availability, lower an estimate, or move a deadline for the tasks that don't fit.")
    if any(t["difficulty"] >= 4 for t in tasks):
        tips.append("Split difficult tasks into smaller topics so each session has one clear goal.")
    if tasks:
        tips.append("Mark sessions completed, partial or missed so the plan adapts to what really happened.")
    return {"source": "rule-based", "summary": summary, "task_notes": notes,
            "revision_tips": tips, "warnings": []}


def explain(provider: AIProvider | None, facts: dict) -> dict:
    fallback = rule_based_explanation(facts)
    if provider is None:
        return fallback
    titles = {t["id"]: t["title"] for t in facts["tasks"]}
    try:
        data = _AIExplanation.model_validate(
            parse_json_object(provider.complete(EXPLAIN_SYSTEM, json.dumps(facts, default=str)))
        )
    except (AIError, ValidationError) as exc:
        fallback["warnings"].append(f"AI answer rejected, showing rule-based explanation instead: {_short(exc)}")
        return fallback

    warnings, notes = [], []
    for item in data.task_notes:
        task_id = item.get("task_id")
        if not isinstance(task_id, int) or task_id not in titles:
            warnings.append(f"Dropped a note about unknown task {task_id!r}.")
            continue
        notes.append({"task_id": task_id, "task_title": titles[task_id], "note": str(item.get("note", ""))[:300]})
    return {
        "source": "ai",
        "summary": data.summary[:600],
        "task_notes": notes,
        "revision_tips": [str(t)[:200] for t in data.revision_tips[:MAX_TIPS]],
        "warnings": warnings,
    }


# --- break a task into subtasks --------------------------------------------------------------

BREAKDOWN_SYSTEM = """You help a student break one study task into smaller subtasks.
Reply with JSON only: {"subtasks": [{"title": "<=120 chars", "estimated_minutes": <int 15-240>}]}
Use 2 to 8 subtasks. The total minutes should be close to the task's remaining minutes."""

TOTAL_TOLERANCE = 0.25  # AI totals may differ from the remaining effort by up to 25%


def rule_based_breakdown(title: str, remaining: int) -> list[dict]:
    parts = min(8, max(2, math.ceil(remaining / 90)))
    base, extra = divmod(remaining, parts)
    return [
        {"title": f"{title} (part {i + 1} of {parts})"[:120], "estimated_minutes": max(15, base + (1 if i < extra else 0))}
        for i in range(parts)
    ]


def breakdown(provider: AIProvider | None, task: dict, free_minutes: int) -> dict:
    remaining = task["remaining_minutes"]
    warnings: list[str] = []
    subtasks, source = None, "rule-based"

    if provider is not None:
        prompt = json.dumps(
            {k: task[k] for k in ("title", "notes", "subject_name", "difficulty", "remaining_minutes", "deadline")},
            default=str,
        )
        try:
            raw = parse_json_object(provider.complete(BREAKDOWN_SYSTEM, prompt)).get("subtasks")
            if not isinstance(raw, list) or not 2 <= len(raw) <= 8:
                raise AIError("expected 2-8 subtasks")
            proposals = [SubtaskProposal.model_validate(item) for item in raw]
            total = sum(p.estimated_minutes for p in proposals)
            if abs(total - remaining) > remaining * TOTAL_TOLERANCE:
                raise AIError(f"subtasks total {total} min but the task has {remaining} min left")
            subtasks, source = [p.model_dump() for p in proposals], "ai"
        except (AIError, ValidationError) as exc:
            warnings.append(f"AI answer rejected, showing a rule-based split instead: {_short(exc)}")

    if subtasks is None:
        subtasks = rule_based_breakdown(task["title"], remaining)

    total = sum(s["estimated_minutes"] for s in subtasks)
    fits = total <= free_minutes
    if not fits:
        warnings.append(
            f"These subtasks need {total} min but only {free_minutes} min of availability "
            f"remain before the deadline (shared with your other tasks)."
        )
    return {
        "source": source, "task_id": task["id"], "task_title": task["title"], "subtasks": subtasks,
        "total_minutes": total, "free_minutes_before_deadline": free_minutes,
        "fits_before_deadline": fits, "warnings": warnings,
    }


def _short(exc: Exception) -> str:
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return text[:200]


def explain_facts(tasks: list[dict], plan: dict, horizon_days: int, now: datetime) -> dict:
    reasons = {r["task_id"]: r["reason"] for r in plan["reasons"]}
    open_tasks = sorted((t for t in tasks if not t["completed"]), key=lambda t: t["deadline"])
    return {
        "now": now,
        "horizon_days": horizon_days,
        "tasks": [
            {"id": t["id"], "title": t["title"], "subject": t["subject_name"], "deadline": t["deadline"],
             "remaining_minutes": t["remaining_minutes"], "scheduled_minutes": t["scheduled_minutes"],
             "priority": t["priority"], "difficulty": t["difficulty"], "reason": reasons.get(t["id"], "")}
            for t in open_tasks
        ],
        "scheduled_minutes": sum(s["planned_minutes"] for s in plan["sessions"] if s["status"] == "planned"),
        "unscheduled": plan["unscheduled"],
    }
