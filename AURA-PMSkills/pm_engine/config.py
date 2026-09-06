"""Configuration files for the engine (API keys, endpoints).

Keys are looked up in this order (real environment variables always win):

1. the process environment
2. ``$PM_ENGINE_ENV_FILE`` if set
3. ``./.env`` in the current working directory (and ``AURA-PMSkills/.env`` next to this package)
4. ``~/.aura/pm-engine.env`` (``$PM_ENGINE_HOME/pm-engine.env`` when ``PM_ENGINE_HOME`` is set)

Set ``PM_ENGINE_ENV_FILE=none`` to disable file loading altogether.

Files use plain ``KEY=value`` lines (``export KEY=value`` and quotes are
accepted; ``#`` starts a comment). Values are loaded into ``os.environ`` once
per process and never overwrite variables that are already set, so a shell
export beats a file, and a project ``.env`` beats the user-level file.

Only these names are ever read from files (so a stray ``.env`` cannot inject
arbitrary environment): everything starting with ``ARENA_``, ``ANTHROPIC_``,
``OPENAI_``, ``OLLAMA_`` or ``PM_ENGINE_``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ALLOWED_PREFIXES = ("ARENA_", "ANTHROPIC_", "OPENAI_", "OLLAMA_", "PM_ENGINE_")
USER_ENV_FILENAME = "pm-engine.env"
_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$""")

_loaded: set[Path] = set()


def user_env_file() -> Path:
    home = os.environ.get("PM_ENGINE_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".aura"
    return base / USER_ENV_FILENAME


def candidate_env_files() -> list[Path]:
    out: list[Path] = []
    explicit = os.environ.get("PM_ENGINE_ENV_FILE")
    if explicit in ("", None):
        pass
    elif explicit.lower() in ("0", "none", "off", os.devnull):
        return []  # file loading disabled (used by the test-suite)
    else:
        out.append(Path(explicit).expanduser())
    out.append(Path.cwd() / ".env")
    out.append(Path(__file__).resolve().parent.parent / ".env")
    out.append(user_env_file())
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        rp = p.resolve() if p.exists() else p
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines; returns only allowed keys."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].rstrip()
        if key.startswith(ALLOWED_PREFIXES):
            out[key] = val
    return out


def load_env_files(*, force: bool = False) -> list[Path]:
    """Load candidate files into ``os.environ`` (without overriding). Returns the files that were read."""
    read: list[Path] = []
    for p in candidate_env_files():
        try:
            rp = p.resolve()
            if not p.is_file() or (rp in _loaded and not force):
                continue
            values = parse_env_text(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        _loaded.add(rp)
        read.append(p)
        for k, v in values.items():
            os.environ.setdefault(k, v)
    return read


def loaded_env_files() -> list[Path]:
    return sorted(_loaded)


def save_env_values(values: dict[str, str], path: Path | None = None) -> Path:
    """Write/merge ``values`` into an env file (default: the user-level file) with 0600 permissions."""
    path = Path(path).expanduser() if path else user_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        existing = parse_env_text("\n".join(lines))
    out: list[str] = []
    done: set[str] = set()
    for raw in lines:
        m = _LINE.match(raw.strip()) if raw.strip() and not raw.strip().startswith("#") else None
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={_quote(values[m.group(1)])}")
            done.add(m.group(1))
        else:
            out.append(raw)
    for k, v in values.items():
        if k not in done:
            out.append(f"{k}={_quote(v)}")
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platform specific
        pass
    for k, v in values.items():
        if existing.get(k) != v:
            os.environ[k] = v
    return path


def _quote(v: str) -> str:
    return v if re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]*", v) else '"' + v.replace('"', '\\"') + '"'


def redact(value: str | None) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"
