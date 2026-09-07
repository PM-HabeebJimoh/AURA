"""The :class:`Engine` — the runtime that executes PM skills and commands.

Routing rules (matching Claude Code's behaviour with the marketplace):

* ``/command args``          → run the command workflow (all steps, or one step at a time)
* ``/plugin:command args``   → same, disambiguated by plugin
* ``/skill args`` or ``/plugin:skill args`` → force-load that skill
* free text                  → auto-load the most relevant skills (BM25), or none

Every run returns a :class:`RunResult` with the prompt that was built, the
completion, the skills used, and the path of any artifact written.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from .config import load_env_files
from .prompt import CHARS_PER_TOKEN, Attachment, Prompt, PromptBuilder, estimate_tokens, parse_slash
from .providers import Completion, Provider, auto_provider
from .registry import Command, Registry, RegistryError, Skill
from .search import Hit, SkillIndex
from .session import Session, Turn
from .workflow import Workflow, parse_workflow, resolve_mode

ProgressFn = Callable[[str], None]


@dataclass
class RunResult:
    prompt: Prompt
    completion: Completion
    skills: list[Skill]
    command: Command | None
    mode: str | None
    step: int | None
    total_steps: int
    session: Session | None
    artifact: Path | None = None
    checkpoint: str | None = None
    offers: list[str] = field(default_factory=list)
    auto_loaded: bool = False

    @property
    def text(self) -> str:
        return self.completion.text

    def to_dict(self, include_prompt: bool = False) -> dict:
        d = {
            "text": self.text,
            "completion": self.completion.to_dict(),
            "skills": [s.qualified_name for s in self.skills],
            "auto_loaded": self.auto_loaded,
            "command": self.command.qualified_name if self.command else None,
            "mode": self.mode,
            "step": self.step,
            "total_steps": self.total_steps,
            "checkpoint": self.checkpoint,
            "offers": self.offers,
            "artifact": str(self.artifact) if self.artifact else None,
            "session": self.session.summary() if self.session else None,
        }
        if include_prompt:
            d["prompt"] = self.prompt.to_dict()
        return d


DEFAULT_MAX_OUTPUT_TOKENS = 4096


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


class Engine:
    """Executes skills and command workflows against a provider.

    ``max_input_tokens`` (default ``$PM_ENGINE_MAX_INPUT_TOKENS``, 0 = unlimited) is a
    *context budget*: when a prompt would exceed it the engine first drops the oldest
    session turns, then truncates attachments, and finally warns (via ``progress``)
    if the system prompt alone is too big — so small free-tier backends (8K-token
    requests) get a predictable request instead of an HTTP 413.
    ``max_output_tokens`` (``$PM_ENGINE_MAX_OUTPUT_TOKENS``, default 4096) is the
    reply cap used when a call does not pass ``max_tokens`` explicitly.
    """

    def __init__(
        self,
        registry: Registry | None = None,
        provider: Provider | None = None,
        *,
        artifacts_dir: Path | str | None = None,
        auto_load: bool = True,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
    ):
        load_env_files()
        self.registry = registry or Registry()
        self.provider = provider or auto_provider()
        self.index = SkillIndex(self.registry)
        self.prompts = PromptBuilder(self.registry)
        self.artifacts_dir = Path(artifacts_dir).expanduser() if artifacts_dir else None
        self.auto_load_enabled = auto_load
        self.max_input_tokens = _env_int("PM_ENGINE_MAX_INPUT_TOKENS", 0) if max_input_tokens is None else max(0, max_input_tokens)
        self.max_output_tokens = _env_int("PM_ENGINE_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS) if max_output_tokens is None else max_output_tokens

    @property
    def budget(self) -> dict:
        return {"max_input_tokens": self.max_input_tokens or None, "max_output_tokens": self.max_output_tokens}

    def budget_report(self) -> dict:
        """How the marketplace's command prompts (no attachments/history) compare with the budget."""
        rows = []
        for plugin in self.registry.plugins.values():
            for cmd in plugin.commands.values():
                wf = parse_workflow(cmd)
                full = self.prompts.for_command(cmd, "").approx_tokens
                per_step = [self.prompts.for_command(cmd, "", step=i, lean=True).approx_tokens for i in range(1, len(wf.steps_for(None)) + 1)]
                rows.append({"command": cmd.qualified_name, "full": full, "max_step": max(per_step) if per_step else full})
        rows.sort(key=lambda r: -r["full"])
        limit = self.max_input_tokens
        return {
            "max_input_tokens": limit or None,
            "commands": len(rows),
            "largest": rows[0] if rows else None,
            "over_full": [r["command"] for r in rows if limit and r["full"] > limit],
            "over_step": [r["command"] for r in rows if limit and r["max_step"] > limit],
        }

    # -- discovery helpers ---------------------------------------------

    def search(self, query: str, **kw) -> list[Hit]:
        return self.index.search(query, **kw)

    def workflow(self, command_ref: str) -> Workflow:
        return parse_workflow(self.registry.get_command(command_ref))

    def resolve(self, text: str) -> dict:
        """Explain how *text* would be routed without calling the model."""
        slash, rest = parse_slash(text)
        if slash:
            try:
                cmd = self.registry.get_command(slash)
                wf = parse_workflow(cmd)
                mode, remaining = resolve_mode(wf, rest)
                wanted = set(wf.skills_for(mode))
                skills = [s.qualified_name for s in self.registry.resolve_command_skills(cmd) if s.name in wanted or s.name not in wf.skills]
                return {"kind": "command", "command": cmd.qualified_name, "mode": mode, "arguments": remaining, "skills": skills, "steps": len(wf.steps_for(mode))}
            except RegistryError:
                pass
            try:
                sk = self.registry.get_skill(slash)
                return {"kind": "skill", "skill": sk.qualified_name, "arguments": rest}
            except RegistryError:
                return {"kind": "unknown", "slash": slash, "arguments": rest, "did_you_mean": [h.to_dict() for h in self.index.did_you_mean(slash)]}
        auto = self.index.auto_load(text) if self.auto_load_enabled else []
        cmd = self.index.suggest_command(text)
        return {"kind": "free-text", "auto_skills": [s.qualified_name for s in auto], "suggested_command": cmd.slash if cmd else None, "arguments": text}

    # -- execution -------------------------------------------------------

    def _prepare(
        self,
        text: str,
        *,
        attachments: Iterable[Attachment] = (),
        session: Session | None = None,
        step: int | None = None,
        extra_skills: Sequence[str] = (),
        progress: ProgressFn | None = None,
    ) -> dict:
        """Route *text* and build the prompt. Shared by :meth:`run` and :meth:`stream`."""
        atts = list(attachments)
        extra = [self.registry.get_skill(s) for s in extra_skills]
        slash, rest = parse_slash(text)
        log = progress or (lambda _m: None)

        command: Command | None = None
        skills: list[Skill] = []
        mode: str | None = None
        auto = False
        total_steps = 0

        # continue an in-progress command when the session has one and no new slash was given
        if slash is None and session and session.active_command and (step is not None or session.current_step < session.total_steps):
            command = self.registry.get_command(session.active_command)
            if step is None:
                step = session.current_step + 1
            rest = session.arguments if not text.strip() else f"{session.arguments}\n\nUser reply: {text.strip()}"
            log(f"continuing {command.slash} at step {step}")

        if command is None and slash:
            try:
                command = self.registry.get_command(slash)
            except RegistryError:
                try:
                    skills = [self.registry.get_skill(slash)]
                    log(f"force-loaded skill {skills[0].qualified_name}")
                except RegistryError as e:
                    suggestions = ", ".join(("/" if h.kind == "command" else "") + h.qualified_name for h in self.index.did_you_mean(slash))
                    raise RegistryError(f"{e}. Did you mean: {suggestions or 'n/a'}") from None

        history = session.messages() if session else []
        if command:
            wf = parse_workflow(command)
            mode, _ = resolve_mode(wf, rest)
            total_steps = len(wf.steps_for(mode))
            if step is not None and not (1 <= step <= max(total_steps, 1)):
                raise ValueError(f"{command.slash} has {total_steps} steps; got step={step}")
            build = lambda a, lean=False: self.prompts.for_command(command, rest, a, extra_skills=extra, step=step, history=history, lean=lean)  # noqa: E731
            prompt = build(atts)
            skills = prompt.skills
        elif skills:
            forced = skills + [s for s in extra if s not in skills]
            build = lambda a: self.prompts.for_skills(forced, rest, a, history=history)  # noqa: E731
            prompt = build(atts)
        else:
            auto_skills = self.index.auto_load(text) if self.auto_load_enabled else []
            for s in extra:
                if s not in auto_skills:
                    auto_skills.append(s)
            auto = bool(auto_skills) and not extra
            if auto_skills:
                log("auto-loaded " + ", ".join(s.qualified_name for s in auto_skills))
            build = lambda a: self.prompts.for_free_text(text, auto_skills, a)  # noqa: E731
            prompt = build(atts)
            skills = prompt.skills

        if self.max_input_tokens:
            prompt, history = self._fit_budget(prompt, history, atts, build, log, lean_ok=command is not None and step is not None)
            if prompt.skills != skills:
                skills = prompt.skills
        if command:
            log(f"running {command.slash}" + (f" mode={mode}" if mode else "") + (f" step {step}/{total_steps}" if step else f" ({total_steps} steps)") + (f" · skills: {', '.join(s.name for s in skills)}" if skills else " · no skills loaded for this step"))

        messages = history + [{"role": "user", "content": prompt.user}]
        return {"prompt": prompt, "messages": messages, "command": command, "skills": skills, "mode": mode, "step": step, "total_steps": total_steps, "auto": auto, "slash": slash, "rest": rest, "text": text, "log": log}

    def _fit_budget(self, prompt: Prompt, history: list[dict], atts: list[Attachment], build: Callable[..., Prompt], log: ProgressFn, *, lean_ok: bool = False) -> tuple[Prompt, list[dict]]:
        """Fit the request into ``max_input_tokens``.

        Order: (1) a step-wise command run reloads only the current step's skills
        (earlier steps' *results* are in the history, which matters more than their
        instructions); (2) drop the oldest history turns; (3) truncate attachments
        proportionally; (4) if the system prompt alone is still too big, warn.
        """
        budget = self.max_input_tokens
        hist = list(history)
        fixed = estimate_tokens(prompt.system) + estimate_tokens(prompt.user)
        hist_tok = [estimate_tokens(m["content"]) for m in hist]
        lean = False
        if lean_ok and fixed + sum(hist_tok) > budget:
            slim = build(atts, lean=True)
            if slim.approx_tokens < fixed:
                prompt, fixed, lean = slim, slim.approx_tokens, True
        dropped = 0
        while hist and fixed + sum(hist_tok) > budget:
            hist.pop(0)
            hist_tok.pop(0)
            dropped += 1
            while hist and hist[0]["role"] != "user":  # keep the alternation user→assistant→…
                hist.pop(0)
                hist_tok.pop(0)
                dropped += 1
        truncated: list[str] = []
        if fixed > budget and atts:
            base = build([], lean=True).approx_tokens if lean else build([]).approx_tokens  # everything except the attachments
            total = sum(len(a.text) for a in atts)
            room = int(max(0, budget - base) * CHARS_PER_TOKEN) - 160 * len(atts)  # wrapper + truncation marker per attachment
            for _ in range(4):  # proportional cut, re-measured (markers/wrappers cost a few tokens)
                room = max(room, 0)
                cut = [a.truncated(int(room * len(a.text) / total)) if total > room else a for a in atts]
                candidate = build(cut, lean=True) if lean else build(cut)
                size = estimate_tokens(candidate.system) + estimate_tokens(candidate.user)
                if size <= budget or room == 0:
                    break
                room -= int((size - budget) * CHARS_PER_TOKEN) + 32
            truncated = [a.name for a, c in zip(atts, cut) if c is not a]
            if truncated:
                prompt, fixed = candidate, size
        estimated = fixed + sum(hist_tok)
        over = max(0, estimated - budget)
        prompt.metadata["budget"] = {"max_input_tokens": budget, "estimated_tokens": estimated, "lean_skills": lean, "history_turns_dropped": dropped, "attachments_truncated": truncated, "over_by": over}
        if lean:
            log(f"context budget {budget}: loading only this step's skills ({', '.join(s.name for s in prompt.skills) or 'none'})")
        if dropped:
            log(f"context budget {budget}: dropped the {dropped} oldest session turn(s)")
        if truncated:
            log(f"context budget {budget}: truncated attachment(s) {', '.join(truncated)}")
        if over:
            log(f"⚠ prompt ≈{estimated} tokens exceeds PM_ENGINE_MAX_INPUT_TOKENS={budget} even without history — the backend may reject it; run the command step by step (--step / --all-steps) or use a backend with a larger context")
        return prompt, hist

    def _finish(self, ctx: dict, completion: Completion, *, session: Session | None, save_artifact: bool) -> RunResult:
        command: Command | None = ctx["command"]
        skills: list[Skill] = ctx["skills"]
        mode, step, total_steps, rest, slash = ctx["mode"], ctx["step"], ctx["total_steps"], ctx["rest"], ctx["slash"]
        prompt: Prompt = ctx["prompt"]

        checkpoint: str | None = None
        offers: list[str] = []
        if command:
            wf = parse_workflow(command)
            steps = wf.steps_for(mode)
            if step is not None and steps:
                st = steps[step - 1]
                checkpoint = st.checkpoints[0] if st.checkpoints else None
            if step is None or step == total_steps:
                offers = wf.offers_for(mode)

        if session is not None:
            session.add(Turn(role="user", content=prompt.user, command=command.qualified_name if command else None, mode=mode, step=step, skills=[s.qualified_name for s in skills]))
            session.add(Turn(role="assistant", content=completion.text, command=command.qualified_name if command else None, mode=mode, step=step, skills=[s.qualified_name for s in skills], provider=completion.provider, model=completion.model))
            if command:
                session.active_command = command.qualified_name
                session.active_mode = mode
                if slash:
                    session.arguments = rest
                session.total_steps = total_steps
                session.current_step = step if step is not None else total_steps
                if session.current_step >= total_steps:
                    session.active_command = None  # workflow complete
            session.save()

        artifact: Path | None = None
        if save_artifact and self.artifacts_dir and (command or skills):
            artifact = self._write_artifact(command, skills, rest or ctx["text"], completion.text, step)
            if session is not None:
                session.artifacts.append(str(artifact))
                session.save()

        return RunResult(prompt=prompt, completion=completion, skills=skills, command=command, mode=mode, step=step, total_steps=total_steps, session=session, artifact=artifact, checkpoint=checkpoint, offers=offers, auto_loaded=ctx["auto"])

    def run(
        self,
        text: str,
        *,
        attachments: Iterable[Attachment] = (),
        session: Session | None = None,
        step: int | None = None,
        extra_skills: Sequence[str] = (),
        max_tokens: int | None = None,
        temperature: float = 0.4,
        save_artifact: bool = True,
        progress: ProgressFn | None = None,
    ) -> RunResult:
        """Route and execute a single request.

        ``step`` runs only one step of a command workflow (1-based). Passing a
        ``session`` whose ``active_command`` is set and no slash in *text*
        continues that command (next step).
        """
        ctx = self._prepare(text, attachments=attachments, session=session, step=step, extra_skills=extra_skills, progress=progress)
        t0 = time.time()
        completion = self.provider.complete(ctx["prompt"].system, ctx["messages"], max_tokens=max_tokens or self.max_output_tokens, temperature=temperature)
        ctx["log"](f"{completion.provider}/{completion.model} responded in {time.time() - t0:.1f}s")
        return self._finish(ctx, completion, session=session, save_artifact=save_artifact)

    def stream(
        self,
        text: str,
        *,
        attachments: Iterable[Attachment] = (),
        session: Session | None = None,
        step: int | None = None,
        extra_skills: Sequence[str] = (),
        max_tokens: int | None = None,
        temperature: float = 0.4,
        save_artifact: bool = True,
        progress: ProgressFn | None = None,
    ) -> Iterator[str | RunResult]:
        """Like :meth:`run` but yields text chunks as they arrive; the final item is the :class:`RunResult`."""
        ctx = self._prepare(text, attachments=attachments, session=session, step=step, extra_skills=extra_skills, progress=progress)
        t0 = time.time()
        pieces: list[str] = []
        for chunk in self.provider.stream(ctx["prompt"].system, ctx["messages"], max_tokens=max_tokens or self.max_output_tokens, temperature=temperature):
            pieces.append(chunk)
            yield chunk
        full = "".join(pieces)
        completion = Completion(text=full, provider=self.provider.name, model=self.provider.model, input_tokens=len(ctx["prompt"].system + ctx["prompt"].user) // 4, output_tokens=len(full) // 4, latency_s=time.time() - t0)
        ctx["log"](f"{completion.provider}/{completion.model} streamed {len(full)} chars in {completion.latency_s:.1f}s")
        yield self._finish(ctx, completion, session=session, save_artifact=save_artifact)

    def run_all_steps(self, text: str, *, session: Session | None = None, on_step: Callable[[RunResult], bool | None] | None = None, **kw) -> list[RunResult]:
        """Execute a command one step at a time (pausing at checkpoints).

        ``on_step`` receives each :class:`RunResult`; returning ``False`` stops
        the run (e.g. when a checkpoint needs the user's answer).
        """
        slash, _ = parse_slash(text)
        if not slash:
            raise ValueError("run_all_steps needs a /command")
        command = self.registry.get_command(slash)
        wf = parse_workflow(command)
        mode, _ = resolve_mode(wf, text.split(" ", 1)[1] if " " in text else "")
        n = len(wf.steps_for(mode))
        results: list[RunResult] = []
        session = session or Session.new()
        for i in range(1, n + 1):
            r = self.run(text if i == 1 else "", session=session, step=i, **kw)
            results.append(r)
            if on_step and on_step(r) is False:
                break
        return results

    # -- artifacts -------------------------------------------------------

    def _write_artifact(self, command: Command | None, skills: Sequence[Skill], request: str, text: str, step: int | None) -> Path:
        assert self.artifacts_dir is not None
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        base = command.name if command else (skills[0].name if skills else "response")
        slug = re.sub(r"[^a-z0-9]+", "-", request.lower()).strip("-")[:48] or "output"
        name = f"{base}-{slug}" + (f"-step{step}" if step else "") + ".md"
        path = self.artifacts_dir / name
        path.write_text(text, encoding="utf-8")
        return path
