"""Parse a command's markdown into an executable workflow.

Upstream commands are prose written for Claude Code. They follow a strong
convention that we exploit:

* ``## Invocation`` — fenced examples of how to call the command
* ``## Workflow`` / ``## The workflow`` / ``## The audit`` — steps as
  ``### Step N: Title``, ``### N. Title`` or ``**Step N: Title**`` (inside modes)
* ``## Modes`` with ``### <Mode> Mode`` sub-sections when the argument hint is
  ``[a|b|c] <ctx>``
* skill references as ``**skill-name** skill`` (only within the same plugin)
* ``**Checkpoint**: "..."`` lines where the workflow pauses for the user
* a fenced output template starting with ``## <Title>: [...]``
* ``### Step N: Offer Next Steps`` with quoted ``- "Want me to **…**?"`` lines
* ``## Notes`` — guidance that applies to the whole run

The parser is deliberately tolerant: anything it cannot classify stays in the
step body so no instruction is lost when the prompt is assembled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .registry import Command, Registry, Skill

_H2 = re.compile(r"^## +(.+?)\s*$", re.M)
_H3 = re.compile(r"^### +(.+?)\s*$", re.M)
_STEP_H3 = re.compile(r"^### +(?:Steps? +([0-9]+(?: *\+ *[0-9]+)*)[:.]?|([0-9]+)\.)\s*(.*)$")
_STEP_BOLD = re.compile(r"^\*\*Step +([0-9]+)[:.]?\s*(.*?)\*\*\s*$")
_MODE_H3 = re.compile(r"^### +(.+?) Mode\s*$")
_CHECKPOINT = re.compile(r"^\*\*Checkpoint\*\*:?\s*(.*)$", re.M)
_SKILL_REF = re.compile(r"\*\*([a-z0-9][a-z0-9-]+)\*\*\s+skills?")
_SKILL_PAIR = re.compile(r"\*\*([a-z0-9][a-z0-9-]+)\*\*\s+(?:and|or|,)\s+\*\*([a-z0-9][a-z0-9-]+)\*\*\s+skills?")
_OFFER = re.compile(r'^\s*-\s+"([^"\n]*\?)"\s*(?:\(.*\))?\s*$', re.M)
_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.S)
_WORKFLOW_H2 = ("workflow", "the workflow", "the audit", "the shipping sequence", "workflow (all modes)", "modes")


@dataclass
class Step:
    number: str
    title: str
    body: str
    skills: list[str] = field(default_factory=list)
    checkpoints: list[str] = field(default_factory=list)
    parallel: bool = False
    is_next_steps: bool = False
    is_context_gathering: bool = False
    mode: str | None = None

    @property
    def offers(self) -> list[str]:
        return [_strip_md(o) for o in _OFFER.findall(self.body)]

    @property
    def questions(self) -> list[str]:
        """Bullet questions in a context-gathering step ("- **User problem**: What ...?")."""
        out = []
        for line in self.body.splitlines():
            s = line.strip()
            if s.startswith(("-", "*", "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")) and "?" in s:
                out.append(_strip_md(re.sub(r"^(?:[-*]|\d+\.)\s*", "", s)))
        return out

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "mode": self.mode,
            "skills": self.skills,
            "checkpoints": self.checkpoints,
            "parallel": self.parallel,
            "is_next_steps": self.is_next_steps,
            "is_context_gathering": self.is_context_gathering,
            "offers": self.offers,
            "questions": self.questions,
            "body": self.body,
        }


@dataclass
class Workflow:
    command: Command
    title: str
    intro: str
    invocation_examples: list[str]
    steps: list[Step]
    modes: dict[str, list[Step]]
    output_template: str
    notes: list[str]
    skills: list[str]
    sections: dict[str, str]
    _mode_sections: dict[str, str] = field(default_factory=dict, repr=False)
    _shared_skills: list[str] = field(default_factory=list, repr=False)

    @property
    def has_modes(self) -> bool:
        return bool(self.modes)

    def skills_for(self, mode: str | None) -> list[str]:
        """Skills relevant to a run: with a matched mode, only that mode's skills plus the shared ones.

        ``/sprint retro`` therefore loads ``retro`` — not ``sprint-plan`` and ``release-notes`` too.
        A template-only mode (``/business-model all``) keeps every skill.
        """
        key = self.match_mode(mode)
        if key is None:
            return list(self.skills)
        own = _skills_in(self._mode_sections.get(key, ""))
        if not own:
            return list(self.skills)
        wanted = set(own) | set(self._shared_skills)
        return [n for n in self.skills if n in wanted]

    def skills_up_to(self, mode: str | None, step: int | None) -> list[str]:
        """Skills a step-wise run needs at *step* (1-based).

        Mirrors Claude Code's lazy loading: a skill enters the context when the
        workflow first says "apply the **x** skill" and stays for later steps, so
        step 1 ("gather context") of most commands loads none. Skills referenced
        outside the numbered steps (a Scope section, a mode template) are always in play.
        """
        allowed = self.skills_for(mode)
        steps = self.steps_for(mode)
        if step is None or not steps:
            return allowed
        in_steps = {n for st in steps for n in st.skills}
        active = {n for st in steps[: max(step, 0)] for n in st.skills}
        # skills mentioned outside the numbered steps join at the first step that actually does work
        first_work = next((i for i, st in enumerate(steps, 1) if st.skills or not st.is_context_gathering), 1)
        outside_ok = step >= first_work
        return [n for n in allowed if n in active or (n not in in_steps and outside_ok)]

    def skills_at(self, mode: str | None, step: int | None) -> list[str]:
        """Lean variant of :meth:`skills_up_to`: only what *step* itself applies.

        Earlier steps' results already sit in the session history, so their skill
        instructions can be left out when context is tight: a "Synthesize" or
        "Generate report" step that names no skill gets none of the step-introduced
        ones (the command body carries the output template). Skills referenced
        outside the numbered steps stay in play for every working step;
        context-gathering and next-steps steps load nothing.
        """
        allowed = self.skills_for(mode)
        steps = self.steps_for(mode)
        if step is None or not steps or not (1 <= step <= len(steps)):
            return allowed
        st = steps[step - 1]
        in_steps = {n for x in steps for n in x.skills}
        working = bool(st.skills) or not (st.is_context_gathering or st.is_next_steps)
        return [n for n in allowed if n in st.skills or (n not in in_steps and working)]

    @property
    def checkpoints(self) -> list[str]:
        return [c for s in self.steps for c in s.checkpoints]

    @property
    def offers(self) -> list[str]:
        return [o for s in self.steps if s.is_next_steps for o in s.offers]

    def offers_for(self, mode: str | None) -> list[str]:
        """Follow-up offers for a run: mode-specific ones first, then shared ones."""
        out: list[str] = []
        key = self.match_mode(mode)
        if key is not None:
            out.extend(o for s in self.modes[key] for o in s.offers)
        for o in self.offers:
            if o not in out:
                out.append(o)
        return out

    def match_mode(self, token: str | None) -> str | None:
        """Map an argument token (``lean``) onto a parsed mode section (``lean-canvas``)."""
        if not token or not self.modes:
            return None
        tok = token.lower().split("/")[0]
        if tok in self.modes:
            return tok
        for key in self.modes:
            if key.startswith(tok) or tok.startswith(key):
                return key
        for key in self.modes:
            parts = set(key.split("-"))
            if set(tok.split("-")) & parts:
                return key
        return None

    def steps_for(self, mode: str | None) -> list[Step]:
        key = self.match_mode(mode)
        if key is not None:
            shared = [s for s in self.steps if s.mode is None]
            mode_steps = self.modes[key]
            if not mode_steps:  # mode section is a pure template → run the shared workflow
                return self.steps
            return mode_steps + [s for s in shared if s.is_next_steps]
        return self.steps

    def mode_template(self, mode: str | None) -> str:
        """The fenced output template inside a mode section, if any."""
        key = self.match_mode(mode)
        if key is None:
            return ""
        fences = _FENCE.findall(self._mode_sections.get(key, ""))
        return fences[0].strip() if fences else ""

    def to_dict(self) -> dict:
        return {
            "command": self.command.qualified_name,
            "title": self.title,
            "intro": self.intro,
            "invocation_examples": self.invocation_examples,
            "modes": {m: [s.to_dict() for s in steps] for m, steps in self.modes.items()},
            "steps": [s.to_dict() for s in self.steps],
            "skills": self.skills,
            "checkpoints": self.checkpoints,
            "offers": self.offers,
            "output_template": self.output_template,
            "notes": self.notes,
        }


def _strip_md(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"`(.+?)`", r"\1", s)
    return s.strip()


def _split_h2(body: str) -> tuple[str, dict[str, str]]:
    """Return ``(preamble, {heading: text})`` split on H2 headings (fences respected)."""
    sections: dict[str, str] = {}
    preamble_lines: list[str] = []
    current: str | None = None
    buf: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = _H2.match(line) if not in_fence else None
        if m:
            if current is None:
                preamble_lines = buf
            else:
                sections[current] = "\n".join(buf).strip("\n")
            current = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    if current is None:
        preamble_lines = buf
    else:
        sections[current] = "\n".join(buf).strip("\n")
    return "\n".join(preamble_lines).strip("\n"), sections


def _skills_in(text: str) -> list[str]:
    out: list[str] = []
    for a, b in _SKILL_PAIR.findall(text):
        for s in (a, b):
            if s not in out:
                out.append(s)
    for s in _SKILL_REF.findall(text):
        if s not in out:
            out.append(s)
    return out


def _classify(step: Step) -> None:
    t = step.title.lower()
    step.is_next_steps = any(k in t for k in ("next step", "offer", "review and iterate", "deepen and iterate", "iterate")) or len(_OFFER.findall(step.body)) >= 2
    step.is_context_gathering = any(k in t for k in ("gather", "understand", "context", "accept", "scope", "determine mode", "clarify"))
    step.parallel = "parallel" in t or "parallel" in step.body[:200].lower()
    step.checkpoints = [_strip_md(c).strip('"') for c in _CHECKPOINT.findall(step.body)]
    step.skills = _skills_in(step.body)


def _parse_steps(text: str, mode: str | None = None) -> list[Step]:
    """Split a workflow section into steps (H3 ``Step N`` / ``N.`` or bold ``**Step N**``)."""
    steps: list[Step] = []
    current: Step | None = None
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        nonlocal current, buf
        if current is not None:
            current.body = "\n".join(buf).strip("\n")
            _classify(current)
            steps.append(current)
        current, buf = None, []

    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if not in_fence:
            m = _STEP_H3.match(line)
            if m:
                flush()
                num = (m.group(1) or m.group(2) or "").replace(" ", "")
                current = Step(number=num, title=m.group(3).strip() or f"Step {num}", body="", mode=mode)
                continue
            mb = _STEP_BOLD.match(line.strip())
            if mb:
                flush()
                current = Step(number=mb.group(1), title=mb.group(2).strip() or f"Step {mb.group(1)}", body="", mode=mode)
                continue
            # a non-step H3/H4 inside a workflow ends the previous step unless it is an output header
            if current is not None and re.match(r"^#{3,4} ", line) and not _MODE_H3.match(line):
                if not re.match(r"^#{3,4} +(Workflow|Output|Format)", line, re.I):
                    buf.append(line)
                    continue
        buf.append(line)
    flush()
    return steps


def _parse_modes(text: str) -> tuple[dict[str, list[Step]], dict[str, str]]:
    modes: dict[str, list[Step]] = {}
    raw: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        if current:
            key = _mode_key(current)
            raw[key] = "\n".join(buf)
            modes[key] = _parse_steps(raw[key], mode=key)

    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = _MODE_H3.match(line) if not in_fence else None
        if m:
            flush()
            current, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    flush()
    return modes, raw


def _mode_key(title: str) -> str:
    return title.strip().lower().replace(" ", "-")


def _output_template(sections: dict[str, str], steps: Iterable[Step]) -> str:
    # 1) an explicit ## Output section
    for key in ("Output", "Output Format"):
        if key in sections:
            fences = _FENCE.findall(sections[key])
            return fences[0].strip() if fences else sections[key].strip()
    # 2) the first fenced block that starts with a "## Title" line, in any step
    for s in steps:
        for f in _FENCE.findall(s.body):
            if f.lstrip().startswith("## "):
                return f.strip()
    # 3) any fenced block anywhere that looks like a document
    for text in sections.values():
        for f in _FENCE.findall(text):
            if f.lstrip().startswith("## "):
                return f.strip()
    return ""


def parse_workflow(command: Command) -> Workflow:
    body = command.body
    title_m = re.search(r"^# +(.+?)\s*$", body, re.M)
    title = title_m.group(1).strip() if title_m else command.name
    preamble, sections = _split_h2(body)
    intro = re.sub(r"^# +.+\n?", "", preamble, count=1).strip()

    invocation = []
    if "Invocation" in sections:
        for f in _FENCE.findall(sections["Invocation"]):
            invocation.extend(l.strip() for l in f.splitlines() if l.strip().startswith("/"))

    # steps live in one of the workflow-ish H2 sections; some commands also
    # number steps in "## Scope" (ship-check) — collect from all candidates in order
    steps: list[Step] = []
    modes: dict[str, list[Step]] = {}
    mode_sections: dict[str, str] = {}
    for heading, text in sections.items():
        h = heading.lower()
        if h == "modes":
            modes, mode_sections = _parse_modes(text)
            # shared steps (e.g. "Offer next steps") outside any mode
            head = text.split("\n### ", 1)[0]
            steps.extend(_parse_steps(head))
            continue
        if h in _WORKFLOW_H2 or h.startswith("workflow") or h.startswith("the "):
            steps.extend(_parse_steps(text))
    notes: list[str] = []
    for key in ("Notes", "Guidelines", "Principles"):
        if key in sections:
            notes.extend(_strip_md(re.sub(r"^\s*[-*]\s*", "", l)) for l in sections[key].splitlines() if l.strip().startswith(("-", "*")))

    all_steps = steps + [s for ms in modes.values() for s in ms]
    skills: list[str] = []
    for s in all_steps:
        for name in s.skills:
            if name not in skills:
                skills.append(name)
    for name in _skills_in(body):  # references outside numbered steps (e.g. in Scope)
        if name not in skills:
            skills.append(name)
    shared_text = body
    for raw in mode_sections.values():
        shared_text = shared_text.replace(raw, "")
    shared_skills = _skills_in(shared_text)

    return Workflow(
        command=command,
        title=title,
        intro=intro,
        invocation_examples=invocation,
        steps=steps,
        modes=modes,
        output_template=_output_template(sections, all_steps),
        notes=notes,
        skills=skills,
        sections=sections,
        _mode_sections=mode_sections,
        _shared_skills=shared_skills,
    )


def resolve_mode(workflow: Workflow, arguments: str) -> tuple[str | None, str]:
    """Split a leading mode token off *arguments* when the command declares modes.

    ``/brainstorm ideas existing Mobile banking`` → mode ``ideas`` (first
    dimension) and remaining arguments; multi-dimension hints (``[a|b] [c|d]``)
    are consumed left-to-right and returned as ``"a/c"``.
    """
    dims = re.findall(r"\[([^\]]+)\]", workflow.command.argument_hint or "")
    if not dims and not workflow.modes:
        return None, arguments
    tokens = arguments.split()
    chosen: list[str] = []
    for dim in dims:
        opts = [o.strip().lower() for o in dim.split("|")]
        if tokens and tokens[0].lower() in opts:
            chosen.append(tokens.pop(0).lower())
        else:
            break
    # a leading token that names a parsed mode section (e.g. "all") also counts
    if not chosen and tokens and workflow.modes and workflow.match_mode(tokens[0]) is not None:
        chosen.append(tokens.pop(0).lower())
    return ("/".join(chosen) if chosen else None), " ".join(tokens)


def skill_graph(registry: Registry) -> dict:
    """A command→skills adjacency for the whole registry (used by the API/UI)."""
    # ids are prefixed by kind: upstream has skills and commands that share a name
    # (pre-mortem, stakeholder-map, ...) inside the same plugin.
    nodes: list[dict] = []
    edges: list[dict] = []
    for p in registry.plugins.values():
        for s in p.skills.values():
            nodes.append({"id": f"skill:{s.qualified_name}", "kind": "skill", "plugin": p.name, "label": s.name, "ref": s.qualified_name})
        for c in p.commands.values():
            nodes.append({"id": f"command:{c.qualified_name}", "kind": "command", "plugin": p.name, "label": c.slash, "ref": c.qualified_name})
            wf = parse_workflow(c)
            for name in wf.skills:
                if name in p.skills:
                    edges.append({"from": f"command:{c.qualified_name}", "to": f"skill:{p.name}:{name}"})
    return {"nodes": nodes, "edges": edges}


def unresolved_references(registry: Registry) -> list[tuple[Command, str]]:
    """Skill references in commands that don't resolve inside their own plugin."""
    bad: list[tuple[Command, str]] = []
    for p in registry.plugins.values():
        for c in p.commands.values():
            for name in parse_workflow(c).skills:
                if name not in p.skills:
                    bad.append((c, name))
    return bad


def skills_used_by(registry: Registry, skill: Skill) -> list[Command]:
    out = []
    p = registry.plugins[skill.plugin]
    for c in p.commands.values():
        if skill.name in parse_workflow(c).skills:
            out.append(c)
    return out
