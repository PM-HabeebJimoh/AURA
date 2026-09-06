"""Marketplace / plugin / skill / command discovery.

A **marketplace** is a directory that contains ``.claude-plugin/marketplace.json``
plus one directory per plugin. A **plugin** directory contains
``.claude-plugin/plugin.json``, ``skills/<name>/SKILL.md`` and ``commands/<name>.md``.

The registry is filesystem-backed and immutable after :meth:`Registry.load`;
call :meth:`Registry.reload` to pick up changes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .frontmatter import Document, parse_frontmatter

DEFAULT_MARKETPLACE_ENV = "PM_SKILLS_PATH"


def default_marketplace_path() -> Path:
    """Locate the vendored marketplace.

    Resolution order: ``$PM_SKILLS_PATH`` → ``<repo>/AURA-PMSkills/pm-skills``
    (relative to this file) → ``./pm-skills`` → cwd.
    """
    env = os.environ.get(DEFAULT_MARKETPLACE_ENV)
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve().parent.parent / "pm-skills"
    if (here / ".claude-plugin" / "marketplace.json").is_file():
        return here
    local = Path.cwd() / "pm-skills"
    if (local / ".claude-plugin" / "marketplace.json").is_file():
        return local
    return Path.cwd()


# ─── Models ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Skill:
    name: str
    plugin: str
    path: Path
    description: str
    body: str
    meta: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}:{self.name}"

    @property
    def word_count(self) -> int:
        return len(self.body.split())

    @property
    def further_reading(self) -> list[tuple[str, str]]:
        """``(title, url)`` pairs from the ``### Further Reading`` section."""
        idx = self.body.find("### Further Reading")
        if idx == -1:
            return []
        return re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", self.body[idx:])

    @property
    def uses_arguments(self) -> bool:
        return "$ARGUMENTS" in self.body

    def to_dict(self, include_body: bool = False) -> dict:
        d = {
            "name": self.name,
            "plugin": self.plugin,
            "qualified_name": self.qualified_name,
            "description": self.description,
            "path": str(self.path),
            "word_count": self.word_count,
            "further_reading": [{"title": t, "url": u} for t, u in self.further_reading],
        }
        if include_body:
            d["body"] = self.body
        return d


@dataclass(frozen=True)
class Command:
    name: str
    plugin: str
    path: Path
    description: str
    argument_hint: str
    body: str
    allowed_tools: str = ""
    meta: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}:{self.name}"

    @property
    def slash(self) -> str:
        return f"/{self.name}"

    @property
    def referenced_skills(self) -> list[str]:
        """Skill names referenced as ``**skill-name** skill`` / ``skills`` in the body,
        plus any bold token that names a skill (``**brainstorm-ideas-new**``)."""
        strict = re.findall(r"\*\*([a-z0-9][a-z0-9-]+)\*\*\s+skills?", self.body)
        # "Apply the **a** and **b** skills" → both a and b
        pairs = re.findall(r"\*\*([a-z0-9][a-z0-9-]+)\*\*\s+(?:and|or|,)\s+\*\*([a-z0-9][a-z0-9-]+)\*\*\s+skills?", self.body)
        out: list[str] = []
        for s in strict + [p[0] for p in pairs] + [p[1] for p in pairs]:
            if s not in out:
                out.append(s)
        return out

    @property
    def modes(self) -> list[str]:
        """Modes parsed from an ``argument-hint`` like ``[plan|retro|release-notes] <ctx>``."""
        m = re.match(r"\s*\[([^\]]+)\]", self.argument_hint or "")
        if not m:
            return []
        return [x.strip() for x in m.group(1).split("|") if x.strip()]

    def to_dict(self, include_body: bool = False) -> dict:
        d = {
            "name": self.name,
            "plugin": self.plugin,
            "qualified_name": self.qualified_name,
            "slash": self.slash,
            "description": self.description,
            "argument_hint": self.argument_hint,
            "allowed_tools": self.allowed_tools,
            "modes": self.modes,
            "path": str(self.path),
        }
        if include_body:
            d["body"] = self.body
        return d


@dataclass(frozen=True)
class Plugin:
    name: str
    path: Path
    version: str
    description: str
    keywords: tuple[str, ...]
    author: dict
    skills: dict[str, Skill]
    commands: dict[str, Command]
    manifest: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def readme(self) -> str:
        p = self.path / "README.md"
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "keywords": list(self.keywords),
            "author": self.author,
            "path": str(self.path),
            "skills": sorted(self.skills),
            "commands": sorted(self.commands),
            "skill_count": len(self.skills),
            "command_count": len(self.commands),
        }


@dataclass(frozen=True)
class Marketplace:
    name: str
    version: str
    description: str
    owner: dict
    path: Path
    plugin_entries: tuple[dict, ...]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "owner": self.owner,
            "path": str(self.path),
            "plugins": [p["name"] for p in self.plugin_entries],
        }


# ─── Loading ─────────────────────────────────────────────────────────────────


class RegistryError(RuntimeError):
    pass


def _load_skill(plugin_name: str, skill_dir: Path) -> Skill | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    doc: Document = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    name = str(doc.get("name") or skill_dir.name)
    return Skill(
        name=name,
        plugin=plugin_name,
        path=skill_md,
        description=str(doc.get("description", "")),
        body=doc.body,
        meta=dict(doc.meta),
    )


def _load_command(plugin_name: str, cmd_file: Path) -> Command:
    doc = parse_frontmatter(cmd_file.read_text(encoding="utf-8"))
    return Command(
        name=cmd_file.stem,
        plugin=plugin_name,
        path=cmd_file,
        description=str(doc.get("description", "")),
        argument_hint=str(doc.get("argument-hint", "") or ""),
        allowed_tools=str(doc.get("allowed-tools", "") or ""),
        body=doc.body,
        meta=dict(doc.meta),
    )


def load_plugin(plugin_dir: Path) -> Plugin:
    plugin_dir = Path(plugin_dir)
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest_path.is_file():
        raise RegistryError(f"{plugin_dir}: missing .claude-plugin/plugin.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RegistryError(f"{manifest_path}: invalid JSON ({e})") from e

    name = manifest.get("name") or plugin_dir.name
    skills: dict[str, Skill] = {}
    skills_dir = plugin_dir / "skills"
    if skills_dir.is_dir():
        for sd in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            s = _load_skill(name, sd)
            if s:
                skills[s.name] = s
    commands: dict[str, Command] = {}
    cmds_dir = plugin_dir / "commands"
    if cmds_dir.is_dir():
        for cf in sorted(cmds_dir.glob("*.md")):
            c = _load_command(name, cf)
            commands[c.name] = c

    kw = manifest.get("keywords") or []
    return Plugin(
        name=name,
        path=plugin_dir,
        version=str(manifest.get("version", "")),
        description=str(manifest.get("description", "")),
        keywords=tuple(str(k) for k in kw) if isinstance(kw, list) else (),
        author=manifest.get("author") or {},
        skills=skills,
        commands=commands,
        manifest=manifest,
    )


class Registry:
    """All plugins, skills and commands of one marketplace directory."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser().resolve() if root else default_marketplace_path()
        self.marketplace: Marketplace | None = None
        self.plugins: dict[str, Plugin] = {}
        self._skills_by_name: dict[str, list[Skill]] = {}
        self._commands_by_name: dict[str, list[Command]] = {}
        self.load()

    # -- loading ---------------------------------------------------------

    def load(self) -> "Registry":
        self.plugins = {}
        self._skills_by_name = {}
        self._commands_by_name = {}
        mp_path = self.root / ".claude-plugin" / "marketplace.json"
        entries: list[dict] = []
        if mp_path.is_file():
            data = json.loads(mp_path.read_text(encoding="utf-8"))
            entries = list(data.get("plugins", []))
            self.marketplace = Marketplace(
                name=data.get("name", self.root.name),
                version=str(data.get("version", "")),
                description=data.get("description", ""),
                owner=data.get("owner", {}),
                path=mp_path,
                plugin_entries=tuple(entries),
            )
        else:
            self.marketplace = None

        plugin_dirs: list[Path] = []
        for e in entries:
            src = e.get("source")
            if isinstance(src, str):
                cand = (self.root / src).resolve()
                if (cand / ".claude-plugin" / "plugin.json").is_file():
                    plugin_dirs.append(cand)
        # also pick up unlisted plugin directories (dev convenience)
        for p in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if p.is_dir() and (p / ".claude-plugin" / "plugin.json").is_file() and p not in plugin_dirs:
                plugin_dirs.append(p)
        # a bare plugin directory passed as root
        if not plugin_dirs and (self.root / ".claude-plugin" / "plugin.json").is_file():
            plugin_dirs.append(self.root)

        for pd in plugin_dirs:
            plugin = load_plugin(pd)
            self.plugins[plugin.name] = plugin
            for s in plugin.skills.values():
                self._skills_by_name.setdefault(s.name, []).append(s)
            for c in plugin.commands.values():
                self._commands_by_name.setdefault(c.name, []).append(c)
        return self

    reload = load

    # -- lookups ---------------------------------------------------------

    def iter_skills(self) -> Iterator[Skill]:
        for p in self.plugins.values():
            yield from p.skills.values()

    def iter_commands(self) -> Iterator[Command]:
        for p in self.plugins.values():
            yield from p.commands.values()

    @property
    def skills(self) -> list[Skill]:
        return sorted(self.iter_skills(), key=lambda s: (s.plugin, s.name))

    @property
    def commands(self) -> list[Command]:
        return sorted(self.iter_commands(), key=lambda c: (c.plugin, c.name))

    def get_plugin(self, name: str) -> Plugin:
        try:
            return self.plugins[name]
        except KeyError:
            raise RegistryError(f"unknown plugin '{name}'. Known: {', '.join(sorted(self.plugins))}") from None

    def get_skill(self, ref: str) -> Skill:
        """Resolve ``skill``, ``plugin:skill`` or ``/plugin:skill``."""
        ref = ref.strip().lstrip("/")
        if ":" in ref:
            plugin, name = ref.split(":", 1)
            plugin_obj = self.get_plugin(plugin)
            if name not in plugin_obj.skills:
                raise RegistryError(f"plugin '{plugin}' has no skill '{name}'")
            return plugin_obj.skills[name]
        matches = self._skills_by_name.get(ref, [])
        if not matches:
            raise RegistryError(f"unknown skill '{ref}'")
        if len(matches) > 1:
            names = ", ".join(m.qualified_name for m in matches)
            raise RegistryError(f"skill '{ref}' is ambiguous: {names}")
        return matches[0]

    def get_command(self, ref: str) -> Command:
        """Resolve ``/name``, ``name``, ``plugin:name`` or ``/plugin:name``."""
        ref = ref.strip().lstrip("/")
        if ":" in ref:
            plugin, name = ref.split(":", 1)
            plugin_obj = self.get_plugin(plugin)
            if name not in plugin_obj.commands:
                raise RegistryError(f"plugin '{plugin}' has no command '{name}'")
            return plugin_obj.commands[name]
        matches = self._commands_by_name.get(ref, [])
        if not matches:
            raise RegistryError(f"unknown command '/{ref}'")
        if len(matches) > 1:
            names = ", ".join(m.qualified_name for m in matches)
            raise RegistryError(f"command '/{ref}' is ambiguous: {names}")
        return matches[0]

    def has_command(self, name: str) -> bool:
        return name.strip().lstrip("/") in self._commands_by_name

    def has_skill(self, name: str) -> bool:
        return name.strip().lstrip("/") in self._skills_by_name

    def resolve_command_skills(self, command: Command) -> list[Skill]:
        """Skills a command chains, resolved *within the same plugin* (upstream rule)."""
        plugin = self.plugins[command.plugin]
        out: list[Skill] = []
        for name in command.referenced_skills:
            s = plugin.skills.get(name)
            if s and s not in out:
                out.append(s)
        return out

    # -- stats -----------------------------------------------------------

    def stats(self) -> dict:
        return {
            "root": str(self.root),
            "marketplace": self.marketplace.to_dict() if self.marketplace else None,
            "plugins": len(self.plugins),
            "skills": sum(len(p.skills) for p in self.plugins.values()),
            "commands": sum(len(p.commands) for p in self.plugins.values()),
            "per_plugin": {n: {"skills": len(p.skills), "commands": len(p.commands)} for n, p in self.plugins.items()},
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        st = self.stats()
        return f"<Registry {self.root} plugins={st['plugins']} skills={st['skills']} commands={st['commands']}>"


def discover_registries(paths: Iterable[Path | str]) -> list[Registry]:
    return [Registry(p) for p in paths]
