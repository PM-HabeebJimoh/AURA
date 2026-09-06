"""Validation: upstream plugin-spec checks + engine-level checks.

Layer 1 re-uses the marketplace's own ``validate_plugins.py`` (vendored with the
content, so it stays in lock-step with upstream's rules).

Layer 2 adds what the *engine* needs to run the content safely:

* every skill referenced by a command resolves inside the same plugin
* every command parses into ≥1 workflow step (or declares modes that do)
* no duplicate skill or command names across plugins (they'd be ambiguous in
  bare ``/name`` form)
* every ``$ARGUMENTS`` placeholder sits in a command or a skill body (never in
  front matter)
* the README / marketplace counts match what the registry loaded
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .registry import Registry
from .workflow import parse_workflow, unresolved_references


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "info": self.info}


def _load_upstream_validator(root: Path):
    path = root / "validate_plugins.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("pm_skills_validate_plugins", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def validate_spec(registry: Registry) -> Report:
    """Layer 1 — upstream plugin-spec validation, run in-process."""
    rep = Report()
    vp = _load_upstream_validator(registry.root)
    if vp is None:
        rep.warnings.append(f"{registry.root}/validate_plugins.py not found; skipping spec validation")
        return rep
    totals = {"skills": 0, "commands": 0, "plugins": 0}
    for plugin in registry.plugins.values():
        res = vp.validate_plugin(str(plugin.path))
        totals["plugins"] += 1
        totals["skills"] += res["skill_count"]
        totals["commands"] += res["command_count"]
        for section, value in res["sections"].items():
            items = value.values() if isinstance(value, dict) else [value]
            for vr in items:
                rep.errors.extend(f"{plugin.name}/{section}: {e}" for e in vr.errors)
                rep.warnings.extend(f"{plugin.name}/{section}: {w}" for w in vr.warnings)
    rep.info["spec_totals"] = totals
    return rep


def validate_engine(registry: Registry) -> Report:
    """Layer 2 — engine-level checks."""
    rep = Report()

    for cmd, name in unresolved_references(registry):
        rep.errors.append(f"{cmd.qualified_name}: references skill '{name}' not found in plugin {cmd.plugin}")

    seen_skills: dict[str, str] = {}
    seen_cmds: dict[str, str] = {}
    steps_total = 0
    for plugin in registry.plugins.values():
        for s in plugin.skills.values():
            if s.name in seen_skills:
                rep.warnings.append(f"skill name '{s.name}' exists in both {seen_skills[s.name]} and {plugin.name} (bare /{s.name} is ambiguous)")
            seen_skills[s.name] = plugin.name
            if "$ARGUMENTS" in s.description:
                rep.errors.append(f"{s.qualified_name}: $ARGUMENTS must not appear in front matter")
        for c in plugin.commands.values():
            if c.name in seen_cmds:
                rep.warnings.append(f"command '/{c.name}' exists in both {seen_cmds[c.name]} and {plugin.name}")
            seen_cmds[c.name] = plugin.name
            wf = parse_workflow(c)
            n = len(wf.steps) + sum(len(v) for v in wf.modes.values())
            steps_total += n
            if n == 0:
                rep.errors.append(f"{c.qualified_name}: no workflow steps could be parsed")
            if not c.argument_hint:
                rep.warnings.append(f"{c.qualified_name}: missing argument-hint")
            if "$ARGUMENTS" in c.description:
                rep.errors.append(f"{c.qualified_name}: $ARGUMENTS must not appear in front matter")
            if wf.modes:
                for mode in c.modes:
                    if wf.match_mode(mode) is None:
                        rep.warnings.append(f"{c.qualified_name}: argument-hint mode '{mode}' has no matching '### … Mode' section")

    st = registry.stats()
    rep.info["registry"] = {"plugins": st["plugins"], "skills": st["skills"], "commands": st["commands"], "workflow_steps": steps_total}

    readme = registry.root / "README.md"
    if readme.is_file():
        m = re.search(r"(\d+) PM skills and (\d+) chained workflows across (\d+) plugins", readme.read_text(encoding="utf-8"))
        if m:
            want = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            have = (st["skills"], st["commands"], st["plugins"])
            if want != have:
                rep.errors.append(f"README headline counts {want} != registry {have}")
            rep.info["readme_counts"] = {"skills": want[0], "commands": want[1], "plugins": want[2]}

    mp = registry.root / ".claude-plugin" / "marketplace.json"
    if mp.is_file():
        data = json.loads(mp.read_text(encoding="utf-8"))
        listed = {p["name"] for p in data.get("plugins", [])}
        on_disk = set(registry.plugins)
        if listed != on_disk:
            rep.errors.append(f"marketplace.json plugins {sorted(listed)} != disk {sorted(on_disk)}")
        versions = {p.version for p in registry.plugins.values()} | {str(data.get("version", ""))}
        if len(versions) > 1:
            rep.warnings.append(f"version drift across manifests: {sorted(versions)}")
        rep.info["version"] = data.get("version")
    return rep


def validate_all(registry: Registry) -> Report:
    spec = validate_spec(registry)
    eng = validate_engine(registry)
    return Report(errors=spec.errors + eng.errors, warnings=spec.warnings + eng.warnings, info={**spec.info, **eng.info})
