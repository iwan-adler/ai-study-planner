"""Settings from environment variables (optionally loaded from a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'study_planner.db'}"
    horizon_days: int = 7
    ai_api_key: str = ""
    ai_base_url: str = ""
    ai_model: str = ""
    ai_timeout_seconds: float = 60.0

    @property
    def ai_enabled(self) -> bool:
        # A model is required; a local server (Ollama) needs a base URL but no key.
        return bool(self.ai_model and (self.ai_api_key or self.ai_base_url))


def _int(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    return Settings(
        database_url=os.getenv("DATABASE_URL", "").strip() or Settings.database_url,
        horizon_days=_int("PLAN_HORIZON_DAYS", 7, 1, 28),
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_base_url=os.getenv("AI_BASE_URL", "").strip().rstrip("/"),
        ai_model=os.getenv("AI_MODEL", "").strip(),
        ai_timeout_seconds=float(_int("AI_TIMEOUT_SECONDS", 60, 1, 600)),
    )
