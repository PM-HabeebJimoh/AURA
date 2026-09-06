"""Scaffold new marketplaces, plugins, skills and commands in the upstream format.

Everything generated here passes the vendored ``validate_plugins.py`` and the
engine validator out of the box, so authored content can be mixed with the
upstream marketplace (``Registry(extra_roots=[...])``) or installed straight
into Claude Code / Codex.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .frontmatter import dump_frontmatter

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ScaffoldError(ValueError):
    pass


def _check_name(name: str, what: str) -> str:
    if not _NAME_RE.match(name):
        raise ScaffoldError(f"{what} name must be kebab-case (a-z, 0-9, hyphens): {name!r}")
    return name


@dataclass
class Created:
    kind: str
    path: Path
    files: list[Path]

    def to_dict(self) -> dict:
        return {"kind": self.kind, "path": str(self.path), "files": [str(f) for f in self.files]}


# ─── marketplace ─────────────────────────────────────────────────────────────


def new_marketplace(root: Path, name: str, *, owner: str = "AURA", email: str = "", description: str = "") -> Created:
    root = Path(root)
    _check_name(name, "marketplace")
    mp_dir = root / ".claude-plugin"
    mp_dir.mkdir(parents=True, exist_ok=True)
    mp = mp_dir / "marketplace.json"
    if mp.exists():
        raise ScaffoldError(f"{mp} already exists")
    data = {
        "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
        "name": name,
        "version": "0.1.0",
        "description": description or f"{name} — skills and workflows authored for the AURA PM Engine.",
        "owner": {"name": owner, **({"email": email} if email else {})},
        "plugins": [],
    }
    mp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    readme = root / "README.md"
    files = [mp]
    if not readme.exists():
        readme.write_text(f"# {name}\n\n{data['description']}\n\nCreate plugins with `pm-engine new plugin <name> --root {root}`.\n", encoding="utf-8")
        files.append(readme)
    return Created(kind="marketplace", path=root, files=files)


def _register_plugin(root: Path, plugin_name: str, description: str) -> None:
    mp = root / ".claude-plugin" / "marketplace.json"
    if not mp.is_file():
        return
    data = json.loads(mp.read_text(encoding="utf-8"))
    plugins = data.setdefault("plugins", [])
    if any(p.get("name") == plugin_name for p in plugins):
        return
    plugins.append({"name": plugin_name, "description": description, "source": f"./{plugin_name}", "category": "product-management"})
    mp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ─── plugin ──────────────────────────────────────────────────────────────────


def new_plugin(root: Path, name: str, *, description: str = "", author: str = "AURA", email: str = "aura@users.noreply.github.com", homepage: str = "https://github.com/PM-HabeebJimoh/AURA", keywords: list[str] | None = None, version: str = "0.1.0") -> Created:
    root = Path(root)
    _check_name(name, "plugin")
    pdir = root / name
    if pdir.exists():
        raise ScaffoldError(f"{pdir} already exists")
    desc = description or f"{name.replace('-', ' ').title()} skills and workflows for the AURA PM Engine."
    (pdir / ".claude-plugin").mkdir(parents=True)
    (pdir / "skills").mkdir()
    (pdir / "commands").mkdir()
    manifest = {
        "name": name,
        "version": version,
        "description": desc,
        "author": {"name": author, **({"email": email} if email else {}), **({"url": homepage} if homepage else {})},
        "keywords": keywords or ["product-management", *name.split("-")],
        "homepage": homepage,
        "license": "MIT",
    }
    mpath = pdir / ".claude-plugin" / "plugin.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    readme = pdir / "README.md"
    readme.write_text(
        f"# {name}\n\n{desc}\n\n## Overview\n\nSkills are nouns (domain knowledge Claude auto-loads); commands are verbs (user-triggered workflows that chain skills).\n\n"
        f"## Installation\n\n```bash\nclaude plugin marketplace add {root}\nclaude plugin install {name}@{root.name}\n```\n\n## Skills (0)\n\n_None yet — `pm-engine new skill <name> --plugin {name}`._\n\n## Commands (0)\n\n_None yet — `pm-engine new command <name> --plugin {name}`._\n\n## License\n\nMIT\n",
        encoding="utf-8",
    )
    _register_plugin(root, name, desc)
    return Created(kind="plugin", path=pdir, files=[mpath, readme])


# ─── skill ───────────────────────────────────────────────────────────────────

SKILL_TEMPLATE = """# {title}

## Purpose

You are an experienced product manager applying the **{title}** framework to $ARGUMENTS.

## Context

{context}

## Instructions

1. **Gather Information**: Read any files the user provides and ask for missing context before you start.

2. **Think Step by Step**: Before writing, analyze:
   - What decision does this work inform?
   - Who is the audience for the output?
   - What constraints apply?

3. **Apply the Framework**:
   - [Describe the first analytical step]
   - [Describe the second analytical step]
   - [Describe how to synthesize the result]

4. **Structure Output**: Present the result as well-formatted markdown using the template below.

## Output Template

```
## {title}: [Topic]

**Date**: [today]
**Context**: [1-2 sentence summary]

### Analysis
[Findings]

### Recommendations
| # | Recommendation | Rationale | Effort |
|---|----------------|-----------|--------|

### Open Questions
- [What still needs validating]
```

## Notes

- Be specific and data-driven where possible
- Flag assumptions clearly so the team can validate them
- Keep the output concise but complete
"""


def new_skill(plugin_dir: Path, name: str, *, description: str = "", triggers: str = "", context: str = "") -> Created:
    plugin_dir = Path(plugin_dir)
    _check_name(name, "skill")
    if not (plugin_dir / ".claude-plugin" / "plugin.json").is_file():
        raise ScaffoldError(f"{plugin_dir} is not a plugin directory")
    sdir = plugin_dir / "skills" / name
    if sdir.exists():
        raise ScaffoldError(f"{sdir} already exists")
    title = name.replace("-", " ").title()
    base = description or f"Apply the {title} framework to a product, feature, or decision."
    trig = triggers or f"Use when the user asks about {title.lower()}, mentions {name.replace('-', ' ')}, or needs this kind of analysis."
    desc = base.rstrip(".") + ". " + trig
    sdir.mkdir(parents=True)
    body = SKILL_TEMPLATE.format(title=title, context=context or f"This skill encodes the {title} approach so the output is structured and comparable across runs.")
    path = sdir / "SKILL.md"
    path.write_text(dump_frontmatter({"name": name, "description": desc}, body), encoding="utf-8")
    _bump_readme_count(plugin_dir, "Skills", name, base)
    return Created(kind="skill", path=sdir, files=[path])


# ─── command ─────────────────────────────────────────────────────────────────

COMMAND_TEMPLATE = """# /{name} -- {title}

{intro}

## Invocation

```
/{name} <describe what you want>
/{name}                    # asks what you need
```

## Workflow

### Step 1: Gather Context

Ask conversationally — most important questions first:

1. **Goal**: What decision will this inform?
2. **Inputs**: What do you already know? (research, data, documents)
3. **Constraints**: Timeline, team, technical or regulatory constraints?

Accept context from uploaded files, pasted text, or conversation.

### Step 2: Apply the Framework

{apply_line}

- [Describe the core analysis this step produces]
- [Describe how to handle missing information]

**Checkpoint**: "Here is the first pass. Anything you'd like to adjust before I finalize?"

### Step 3: Generate the Output

```
## {title}: [Topic]

**Date**: [today]

### Summary
[2-3 sentences]

### Details
[Findings, tables, recommendations]

### Next Steps
- [Action, owner, timing]
```

Save the output as a markdown file to the user's workspace.

### Step 4: Offer Next Steps

- "Want me to **turn this into a PRD**?"
- "Should I **run a pre-mortem** on the plan?"
- "Want me to **prioritize** the recommendations against your backlog?"

## Notes

- Be opinionated — a tight recommendation beats an exhaustive list
- State assumptions explicitly when context is missing
- Suggest follow-ups in natural language; never hard-reference commands from other plugins
"""


def new_command(plugin_dir: Path, name: str, *, description: str = "", argument_hint: str = "<product, feature, or question>", skills: list[str] | None = None, intro: str = "") -> Created:
    plugin_dir = Path(plugin_dir)
    _check_name(name, "command")
    if not (plugin_dir / ".claude-plugin" / "plugin.json").is_file():
        raise ScaffoldError(f"{plugin_dir} is not a plugin directory")
    path = plugin_dir / "commands" / f"{name}.md"
    if path.exists():
        raise ScaffoldError(f"{path} already exists")
    title = name.replace("-", " ").title()
    skills = skills or []
    for sk in skills:
        if not (plugin_dir / "skills" / sk / "SKILL.md").is_file():
            raise ScaffoldError(f"skill '{sk}' does not exist in {plugin_dir.name} — commands may only chain skills from their own plugin")
    if skills:
        bolded = " and ".join(f"**{s}**" for s in skills)
        apply_line = f"Apply the {bolded} skill{'s' if len(skills) > 1 else ''}:"
    else:
        apply_line = "Apply the relevant skill(s) from this plugin:"
    desc = description or f"{title} — a guided workflow that gathers context, applies the framework, and produces a structured document"
    body = COMMAND_TEMPLATE.format(name=name, title=title, intro=intro or f"Run a structured {title.lower()} workflow with checkpoints, ending in a saved markdown document.", apply_line=apply_line)
    path.parent.mkdir(exist_ok=True)
    path.write_text(dump_frontmatter({"description": desc, "argument-hint": argument_hint}, body), encoding="utf-8")
    _bump_readme_count(plugin_dir, "Commands", name, desc, slash=True)
    return Created(kind="command", path=path, files=[path])


# ─── README maintenance (keeps upstream's `## Skills (N)` convention) ────────


def _bump_readme_count(plugin_dir: Path, section: str, name: str, description: str, *, slash: bool = False) -> None:
    readme = plugin_dir / "README.md"
    if not readme.is_file():
        return
    text = readme.read_text(encoding="utf-8")
    m = re.search(rf"^## {section} \((\d+)\)\s*$", text, re.M)
    if not m:
        return
    n = int(m.group(1)) + 1
    label = f"`/{plugin_dir.name}:{name}`" if slash else f"**{name}**"
    entry = f"- {label} — {description.split('. ')[0].rstrip('.')}."
    head_end = m.end()
    # find the end of this section (next H2 or EOF)
    nxt = re.search(r"^## ", text[head_end:], re.M)
    sec_end = head_end + nxt.start() if nxt else len(text)
    section_body = text[head_end:sec_end]
    section_body = re.sub(r"\n_None yet[^\n]*_\n", "\n", section_body)
    section_body = section_body.rstrip("\n") + f"\n{entry}\n\n"
    text = text[: m.start()] + f"## {section} ({n})" + section_body + text[sec_end:]
    readme.write_text(text, encoding="utf-8")
