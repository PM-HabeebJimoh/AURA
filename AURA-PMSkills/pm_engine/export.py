"""Export skills (and commands) to other AI assistants.

Upstream documents copying ``skills/*/SKILL.md`` folders into each tool's
skills directory. This module automates that and adds a few extra formats:

============  =====================================  ==========================
target        layout                                 notes
============  =====================================  ==========================
``claude``    ``.claude/skills/<skill>/SKILL.md``    + ``.claude/commands/<cmd>.md``
``cursor``    ``.cursor/skills/<skill>/SKILL.md``
``gemini``    ``.gemini/skills/<skill>/SKILL.md``
``opencode``  ``.opencode/skills/<skill>/SKILL.md``
``kiro``      ``.kiro/skills/<skill>/SKILL.md``
``codex``     ``.codex/skills/<skill>/SKILL.md``     commands converted to skills
``bundle``    ``<out>/pm-skills-bundle.json``        single JSON for any runtime
``prompts``   ``<out>/<plugin>/<name>.prompt.md``    fully-rendered system prompts
============  =====================================  ==========================
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .frontmatter import dump_frontmatter
from .prompt import PromptBuilder
from .registry import Registry
from .workflow import parse_workflow

TARGET_DIRS = {
    "claude": ".claude",
    "cursor": ".cursor",
    "gemini": ".gemini",
    "opencode": ".opencode",
    "kiro": ".kiro",
    "codex": ".codex",
}


@dataclass
class ExportReport:
    target: str
    destination: Path
    skills: int = 0
    commands: int = 0
    files: list[Path] | None = None

    def to_dict(self) -> dict:
        return {"target": self.target, "destination": str(self.destination), "skills": self.skills, "commands": self.commands, "files": [str(f) for f in (self.files or [])]}


def _plugins(registry: Registry, only: Iterable[str] | None) -> list:
    if not only:
        return list(registry.plugins.values())
    return [registry.get_plugin(p) for p in only]


def export(registry: Registry, target: str, destination: Path | str, *, plugins: Iterable[str] | None = None, overwrite: bool = True) -> ExportReport:
    target = target.lower()
    dest = Path(destination).expanduser().resolve()
    if target == "bundle":
        return export_bundle(registry, dest, plugins=plugins)
    if target == "prompts":
        return export_prompts(registry, dest, plugins=plugins)
    if target not in TARGET_DIRS:
        raise ValueError(f"unknown export target '{target}'. Choose from: {', '.join([*TARGET_DIRS, 'bundle', 'prompts'])}")

    root = dest / TARGET_DIRS[target]
    skills_root = root / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    report = ExportReport(target=target, destination=root, files=[])

    for plugin in _plugins(registry, plugins):
        for skill in plugin.skills.values():
            out_dir = skills_root / skill.name
            if out_dir.exists() and overwrite:
                shutil.rmtree(out_dir)
            shutil.copytree(skill.path.parent, out_dir, dirs_exist_ok=True)
            report.skills += 1
            report.files.append(out_dir / "SKILL.md")

        if target == "claude":
            cmd_root = root / "commands"
            cmd_root.mkdir(parents=True, exist_ok=True)
            for cmd in plugin.commands.values():
                out = cmd_root / f"{cmd.name}.md"
                shutil.copyfile(cmd.path, out)
                report.commands += 1
                report.files.append(out)
        elif target == "codex":
            # Codex has no slash commands: convert each command into a workflow skill
            for cmd in plugin.commands.values():
                wf = parse_workflow(cmd)
                out_dir = skills_root / f"{cmd.name}-workflow"
                out_dir.mkdir(parents=True, exist_ok=True)
                meta = {
                    "name": f"{cmd.name}-workflow",
                    "description": f"{cmd.description}. Use when the user asks to {cmd.name.replace('-', ' ')} or describes the steps of that workflow. Argument hint: {cmd.argument_hint}",
                }
                body = (
                    f"# {wf.title}\n\n{wf.intro}\n\n"
                    "This skill was converted from a slash command. Run the workflow below in plain language, "
                    "pausing at each checkpoint.\n\n" + cmd.body
                )
                (out_dir / "SKILL.md").write_text(dump_frontmatter(meta, body), encoding="utf-8")
                report.commands += 1
                report.files.append(out_dir / "SKILL.md")
    return report


def export_bundle(registry: Registry, dest: Path, *, plugins: Iterable[str] | None = None) -> ExportReport:
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "pm-skills-bundle.json"
    data = {
        "marketplace": registry.marketplace.to_dict() if registry.marketplace else None,
        "plugins": [],
    }
    for plugin in _plugins(registry, plugins):
        pd = plugin.to_dict()
        pd["skills"] = [s.to_dict(include_body=True) for s in plugin.skills.values()]
        pd["commands"] = []
        for c in plugin.commands.values():
            cd = c.to_dict(include_body=True)
            cd["workflow"] = parse_workflow(c).to_dict()
            pd["commands"].append(cd)
        data["plugins"].append(pd)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    n_s = sum(len(p["skills"]) for p in data["plugins"])
    n_c = sum(len(p["commands"]) for p in data["plugins"])
    return ExportReport(target="bundle", destination=out, skills=n_s, commands=n_c, files=[out])


def export_prompts(registry: Registry, dest: Path, *, plugins: Iterable[str] | None = None) -> ExportReport:
    """Write the fully-assembled system prompt for every skill and command."""
    pb = PromptBuilder(registry)
    report = ExportReport(target="prompts", destination=dest, files=[])
    for plugin in _plugins(registry, plugins):
        pdir = dest / plugin.name
        pdir.mkdir(parents=True, exist_ok=True)
        for s in plugin.skills.values():
            p = pb.for_skills([s], "$ARGUMENTS")
            f = pdir / f"skill-{s.name}.prompt.md"
            f.write_text(p.system + "\n\n---\n\n" + p.user, encoding="utf-8")
            report.skills += 1
            report.files.append(f)
        for c in plugin.commands.values():
            p = pb.for_command(c, "$ARGUMENTS")
            f = pdir / f"command-{c.name}.prompt.md"
            f.write_text(p.system + "\n\n---\n\n" + p.user, encoding="utf-8")
            report.commands += 1
            report.files.append(f)
    return report
