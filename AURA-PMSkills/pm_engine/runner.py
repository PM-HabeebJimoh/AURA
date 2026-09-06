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

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from .prompt import Attachment, Prompt, PromptBuilder, parse_slash
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


class Engine:
    def __init__(
        self,
        registry: Registry | None = None,
        provider: Provider | None = None,
        *,
        artifacts_dir: Path | str | None = None,
        auto_load: bool = True,
    ):
        self.registry = registry or Registry()
        self.provider = provider or auto_provider()
        self.index = SkillIndex(self.registry)
        self.prompts = PromptBuilder(self.registry)
        self.artifacts_dir = Path(artifacts_dir).expanduser() if artifacts_dir else None
        self.auto_load_enabled = auto_load

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
                return {"kind": "command", "command": cmd.qualified_name, "mode": mode, "arguments": remaining, "skills": [s.qualified_name for s in self.registry.resolve_command_skills(cmd)], "steps": len(wf.steps_for(mode))}
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

        if command:
            wf = parse_workflow(command)
            mode, _ = resolve_mode(wf, rest)
            total_steps = len(wf.steps_for(mode))
            if step is not None and not (1 <= step <= max(total_steps, 1)):
                raise ValueError(f"{command.slash} has {total_steps} steps; got step={step}")
            prompt = self.prompts.for_command(command, rest, atts, extra_skills=extra, step=step, history=session.messages() if session else ())
            skills = prompt.skills
            log(f"running {command.slash}" + (f" mode={mode}" if mode else "") + (f" step {step}/{total_steps}" if step else f" ({total_steps} steps)"))
        elif skills:
            prompt = self.prompts.for_skills(skills + [s for s in extra if s not in skills], rest, atts, history=session.messages() if session else ())
        else:
            auto_skills = self.index.auto_load(text) if self.auto_load_enabled else []
            for s in extra:
                if s not in auto_skills:
                    auto_skills.append(s)
            auto = bool(auto_skills) and not extra
            if auto_skills:
                log("auto-loaded " + ", ".join(s.qualified_name for s in auto_skills))
            prompt = self.prompts.for_free_text(text, auto_skills, atts)
            skills = prompt.skills

        messages = (session.messages() if session else []) + [{"role": "user", "content": prompt.user}]
        return {"prompt": prompt, "messages": messages, "command": command, "skills": skills, "mode": mode, "step": step, "total_steps": total_steps, "auto": auto, "slash": slash, "rest": rest, "text": text, "log": log}

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
        max_tokens: int = 4096,
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
        completion = self.provider.complete(ctx["prompt"].system, ctx["messages"], max_tokens=max_tokens, temperature=temperature)
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
        max_tokens: int = 4096,
        temperature: float = 0.4,
        save_artifact: bool = True,
        progress: ProgressFn | None = None,
    ) -> Iterator[str | RunResult]:
        """Like :meth:`run` but yields text chunks as they arrive; the final item is the :class:`RunResult`."""
        ctx = self._prepare(text, attachments=attachments, session=session, step=step, extra_skills=extra_skills, progress=progress)
        t0 = time.time()
        pieces: list[str] = []
        for chunk in self.provider.stream(ctx["prompt"].system, ctx["messages"], max_tokens=max_tokens, temperature=temperature):
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
