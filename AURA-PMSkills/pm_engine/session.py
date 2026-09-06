"""Persisted multi-turn sessions.

A session records every turn (request, loaded skills / command, model output)
as JSON under ``~/.aura/pm-sessions`` (override with ``PM_ENGINE_HOME``) so
workflows can be resumed step by step — matching upstream's *checkpoint*
behaviour where a command pauses for the user between phases.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator


def sessions_dir() -> Path:
    home = os.environ.get("PM_ENGINE_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".aura"
    d = base / "pm-sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Turn:
    role: str  # "user" | "assistant" | "system"
    content: str
    ts: float = field(default_factory=time.time)
    command: str | None = None
    mode: str | None = None
    step: int | None = None
    skills: list[str] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Session:
    id: str
    title: str = ""
    created: float = field(default_factory=time.time)
    turns: list[Turn] = field(default_factory=list)
    active_command: str | None = None
    active_mode: str | None = None
    current_step: int = 0
    total_steps: int = 0
    arguments: str = ""
    artifacts: list[str] = field(default_factory=list)

    # -- persistence -----------------------------------------------------

    @classmethod
    def new(cls, title: str = "") -> "Session":
        return cls(id=time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6], title=title)

    @property
    def path(self) -> Path:
        return sessions_dir() / f"{self.id}.json"

    def save(self) -> Path:
        data = asdict(self)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return self.path

    @classmethod
    def load(cls, session_id: str) -> "Session":
        p = sessions_dir() / f"{session_id}.json"
        if not p.is_file():
            raise FileNotFoundError(f"no session '{session_id}' in {sessions_dir()}")
        data = json.loads(p.read_text(encoding="utf-8"))
        turns = [Turn(**t) for t in data.pop("turns", [])]
        return cls(turns=turns, **data)

    @classmethod
    def list(cls) -> Iterator["Session"]:
        for p in sorted(sessions_dir().glob("*.json"), reverse=True):
            try:
                yield cls.load(p.stem)
            except Exception:  # noqa: BLE001 — skip corrupt files
                continue

    def delete(self) -> None:
        if self.path.is_file():
            self.path.unlink()

    # -- turns -----------------------------------------------------------

    def add(self, turn: Turn) -> Turn:
        self.turns.append(turn)
        if not self.title and turn.role == "user":
            self.title = re.sub(r"\s+", " ", turn.content)[:80]
        return turn

    def messages(self, limit: int = 20) -> list[dict]:
        """Chat history in provider format (alternating user/assistant)."""
        msgs = [{"role": t.role, "content": t.content} for t in self.turns if t.role in ("user", "assistant")]
        msgs = msgs[-limit:]
        # providers require the first message to be from the user
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    @property
    def last_assistant(self) -> Turn | None:
        for t in reversed(self.turns):
            if t.role == "assistant":
                return t
        return None

    def summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created": self.created,
            "turns": len(self.turns),
            "active_command": self.active_command,
            "active_mode": self.active_mode,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "artifacts": self.artifacts,
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        d["summary"] = self.summary()
        return d
