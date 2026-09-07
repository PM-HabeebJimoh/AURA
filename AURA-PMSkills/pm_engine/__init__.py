"""AURA PM Engine — a runtime for the PM Skills marketplace.

The upstream project (https://github.com/phuryn/pm-skills) ships *content*:
plugins made of ``SKILL.md`` skills and slash-command workflows that Claude Code
loads at runtime. This package is the runtime — it makes that content
executable anywhere:

* :mod:`pm_engine.registry`   — discover marketplaces, plugins, skills, commands
* :mod:`pm_engine.frontmatter` — YAML-frontmatter parsing (no hard dependency on PyYAML)
* :mod:`pm_engine.search`     — BM25 search + automatic skill loading from free text
* :mod:`pm_engine.workflow`   — parse command markdown into steps / checkpoints / skill graph
* :mod:`pm_engine.prompt`     — assemble system + user prompts (``$ARGUMENTS`` substitution)
* :mod:`pm_engine.providers`  — LLM backends (Anthropic, OpenAI-compatible, Ollama, offline)
* :mod:`pm_engine.runner`     — the :class:`Engine` that ties everything together
* :mod:`pm_engine.session`    — persisted multi-turn sessions
* :mod:`pm_engine.export`     — export skills to Cursor / Gemini CLI / OpenCode / Kiro / Claude
* :mod:`pm_engine.validator`  — plugin-spec + engine-level validation
* :mod:`pm_engine.updater`    — re-sync the vendored marketplace from upstream git
* :mod:`pm_engine.cli`        — ``pm-engine`` command-line interface
* :mod:`pm_engine.server`     — Flask JSON API + web UI
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = [
    "__version__",
    "Engine",
    "Registry",
    "PromptBuilder",
    "SkillIndex",
    "parse_workflow",
]


def __getattr__(name: str):  # lazy imports keep `import pm_engine` cheap
    if name == "Engine":
        from .runner import Engine

        return Engine
    if name == "Registry":
        from .registry import Registry

        return Registry
    if name == "PromptBuilder":
        from .prompt import PromptBuilder

        return PromptBuilder
    if name == "SkillIndex":
        from .search import SkillIndex

        return SkillIndex
    if name == "parse_workflow":
        from .workflow import parse_workflow

        return parse_workflow
    raise AttributeError(name)
