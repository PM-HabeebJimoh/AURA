"""YAML front-matter parsing for SKILL.md and command markdown files.

The upstream marketplace uses *flat* front matter (``name``, ``description``,
``argument-hint``, ``allowed-tools``), so a small dependency-free parser is
enough. If PyYAML is installed it is used for fidelity; the fallback handles
quoted strings, comments, and simple ``key: value`` pairs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_FM_DELIM = "---"
_KV_RE = re.compile(r"^([A-Za-z0-9_\-\.]+)\s*:\s*(.*)$")


@dataclass
class Document:
    """A markdown document split into front matter and body."""

    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    raw: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        return self.meta.get(key, default)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _fallback_parse(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        # continuation line for a multi-line scalar (indented, no key)
        if current_key and (line.startswith(" ") or line.startswith("\t")) and not _KV_RE.match(line.strip()):
            prev = result.get(current_key, "")
            result[current_key] = (prev + " " + line.strip()).strip()
            continue
        m = _KV_RE.match(line.strip())
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        current_key = key
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            result[key] = [_strip_quotes(v) for v in inner.split(",") if v.strip()] if inner else []
        elif value in ("|", ">", "|-", ">-"):
            result[key] = ""
        else:
            result[key] = _strip_quotes(value)
    return result


def parse_frontmatter(text: str) -> Document:
    """Split *text* into ``Document(meta, body)``.

    Files without a leading ``---`` block yield an empty ``meta``.
    """
    if not text.startswith(_FM_DELIM):
        return Document(meta={}, body=text, raw=text)

    # find the closing delimiter on its own line
    lines = text.split("\n")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FM_DELIM:
            end_idx = i
            break
    if end_idx is None:
        return Document(meta={}, body=text, raw=text)

    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")

    meta: dict[str, Any]
    try:  # prefer PyYAML when present
        import yaml  # type: ignore

        loaded = yaml.safe_load(fm_text) or {}
        meta = loaded if isinstance(loaded, dict) else {}
    except Exception:  # noqa: BLE001 — any yaml failure falls back to the simple parser
        meta = _fallback_parse(fm_text)

    # normalise scalar values to str where they came in as other types
    for k, v in list(meta.items()):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            meta[k] = str(v)
    return Document(meta=meta, body=body, raw=text)


def dump_frontmatter(meta: dict[str, Any], body: str) -> str:
    """Serialize ``meta`` + ``body`` back into a markdown document."""
    out = [_FM_DELIM]
    for k, v in meta.items():
        if isinstance(v, list):
            out.append(f"{k}: [{', '.join(str(x) for x in v)}]")
        else:
            sv = str(v)
            needs_quote = any(ch in sv for ch in (":", "#", '"', "'")) or sv != sv.strip()
            out.append(f'{k}: "{sv.replace(chr(34), chr(39))}"' if needs_quote else f"{k}: {sv}")
    out.append(_FM_DELIM)
    out.append("")
    out.append(body.rstrip("\n") + "\n")
    return "\n".join(out)
