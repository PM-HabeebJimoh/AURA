"""``pm-engine`` command-line interface.

    pm-engine list [plugins|skills|commands] [--plugin P] [--json]
    pm-engine show  <skill|/command>                 # full content + parsed workflow
    pm-engine search <query>                          # BM25 over skills + commands
    pm-engine resolve "<text>"                        # how a request would be routed
    pm-engine run  "/write-prd SSO for enterprise" [--step N] [--file f.md] [--session ID] [--out DIR]
    pm-engine run  "riskiest assumptions for our AI assistant"   # auto-loads skills
    pm-engine chat [--session ID]                     # interactive REPL
    pm-engine workflow /discover [--json]             # parsed step graph
    pm-engine validate                                # spec + engine checks (exit 1 on errors)
    pm-engine test                                    # run vendored upstream test-suite + engine tests
    pm-engine export <target> <dest> [--plugin P ...] # claude|cursor|gemini|opencode|kiro|codex|bundle|prompts
    pm-engine serve [--host 0.0.0.0] [--port 8080]    # JSON API + web UI
    pm-engine update-skills [--ref main]              # re-vendor upstream
    pm-engine sessions [--delete ID]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from . import __version__
from .export import export
from .prompt import Attachment
from .providers import ProviderError, auto_provider, available_providers, make_provider
from .registry import Registry, RegistryError
from .runner import Engine
from .session import Session
from .validator import validate_all
from .workflow import parse_workflow


def _registry(args) -> Registry:
    return Registry(args.marketplace) if getattr(args, "marketplace", None) else Registry()


def _engine(args) -> Engine:
    reg = _registry(args)
    provider = None
    if getattr(args, "provider", None):
        provider = make_provider(args.provider, **({"model": args.model} if getattr(args, "model", None) else {}))
    elif getattr(args, "model", None):
        provider = auto_provider(model=args.model)
    out = getattr(args, "out", None)
    return Engine(reg, provider, artifacts_dir=out)


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    else:
        print(obj)


# ─── sub-commands ────────────────────────────────────────────────────────────


def cmd_list(args) -> int:
    reg = _registry(args)
    what = args.what
    if what == "plugins":
        rows = [p.to_dict() for p in reg.plugins.values()]
        if args.json:
            return _print(rows, True) or 0
        for p in rows:
            print(f"{p['name']:<24} v{p['version']:<7} {p['skill_count']:>2} skills {p['command_count']:>2} commands  — {p['description'][:80]}")
        return 0
    if what == "skills":
        items = [s for s in reg.skills if not args.plugin or s.plugin == args.plugin]
        if args.json:
            return _print([s.to_dict() for s in items], True) or 0
        for s in items:
            print(f"{s.plugin}:{s.name:<34} {s.description[:100]}")
        return 0
    if what == "commands":
        items = [c for c in reg.commands if not args.plugin or c.plugin == args.plugin]
        if args.json:
            return _print([c.to_dict() for c in items], True) or 0
        for c in items:
            print(f"/{c.name:<26} [{c.plugin}] {c.argument_hint:<50} {c.description[:70]}")
        return 0
    st = reg.stats()
    if args.json:
        return _print(st, True) or 0
    mp = st["marketplace"]
    print(f"Marketplace: {mp['name'] if mp else '-'} v{mp['version'] if mp else '-'}  ({st['root']})")
    print(f"Plugins: {st['plugins']}  Skills: {st['skills']}  Commands: {st['commands']}")
    for n, c in st["per_plugin"].items():
        print(f"  {n:<24} {c['skills']:>2} skills  {c['commands']:>2} commands")
    return 0


def cmd_show(args) -> int:
    reg = _registry(args)
    ref = args.ref
    try:
        cmd = reg.get_command(ref)
    except RegistryError:
        cmd = None
    if cmd:
        wf = parse_workflow(cmd)
        if args.json:
            d = cmd.to_dict(include_body=True)
            d["workflow"] = wf.to_dict()
            return _print(d, True) or 0
        print(f"/{cmd.name}  [{cmd.plugin}]  {cmd.argument_hint}\n{cmd.description}\n")
        print(f"Skills: {', '.join(wf.skills) or '-'}")
        print(f"Steps: {len(wf.steps)}" + (f"  Modes: {', '.join(wf.modes)}" if wf.modes else ""))
        print("-" * 70)
        print(cmd.body)
        return 0
    try:
        sk = reg.get_skill(ref)
    except RegistryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        return _print(sk.to_dict(include_body=True), True) or 0
    print(f"{sk.qualified_name}\n{sk.description}\n" + "-" * 70)
    print(sk.body)
    return 0


def cmd_search(args) -> int:
    eng = _engine(args)
    hits = eng.search(" ".join(args.query), kind=args.kind, limit=args.limit, plugin=args.plugin)
    if args.json:
        return _print([h.to_dict() for h in hits], True) or 0
    if not hits:
        print("no matches")
        return 1
    for h in hits:
        tag = "/" + h.name if h.kind == "command" else h.name
        print(f"{h.score:6.2f}  {h.kind:<7} {h.plugin}:{tag:<32} {h.description[:80]}")
    return 0


def cmd_resolve(args) -> int:
    eng = _engine(args)
    _print(eng.resolve(" ".join(args.text)), True)
    return 0


def cmd_workflow(args) -> int:
    reg = _registry(args)
    wf = parse_workflow(reg.get_command(args.command))
    if args.json:
        return _print(wf.to_dict(), True) or 0
    print(f"{wf.title}\n{wf.intro[:300]}\n")
    print("Invocation:")
    for ex in wf.invocation_examples:
        print(f"  {ex}")
    print(f"\nSkills used: {', '.join(wf.skills) or '-'}")

    def show(steps, indent="  "):
        for s in steps:
            flags = []
            if s.skills:
                flags.append("skills=" + ",".join(s.skills))
            if s.checkpoints:
                flags.append("checkpoint")
            if s.parallel:
                flags.append("parallel")
            if s.is_next_steps:
                flags.append("next-steps")
            print(f"{indent}Step {s.number}: {s.title}" + (f"   [{' '.join(flags)}]" if flags else ""))
            for c in s.checkpoints:
                print(f"{indent}    ⏸ {c}")

    if wf.modes:
        for m, steps in wf.modes.items():
            print(f"\nMode: {m}")
            show(steps, "    ")
        if wf.steps:
            print("\nShared steps:")
            show(wf.steps)
    else:
        print("\nSteps:")
        show(wf.steps)
    if wf.offers:
        print("\nOffers after completion:")
        for o in wf.offers:
            print(f"  - {o}")
    if wf.output_template:
        print("\nOutput template:\n" + textwrap.indent(wf.output_template[:1200], "  "))
    return 0


def cmd_run(args) -> int:
    eng = _engine(args)
    atts = [Attachment.from_path(f) for f in (args.file or [])]
    session = Session.load(args.session) if args.session else (Session.new() if args.save_session or args.all_steps else None)
    text = " ".join(args.text)
    log = (lambda m: print(f"  · {m}", file=sys.stderr)) if not args.quiet else None
    try:
        if args.all_steps:
            results = eng.run_all_steps(text, session=session, attachments=atts, max_tokens=args.max_tokens, progress=log, on_step=lambda r: (print(r.text), print(f"\n⏸ {r.checkpoint}\n") if r.checkpoint else None, True)[-1])
            r = results[-1]
        else:
            r = eng.run(text, attachments=atts, session=session, step=args.step, extra_skills=args.skill or [], max_tokens=args.max_tokens, progress=log)
            if args.json:
                return _print(r.to_dict(include_prompt=args.show_prompt), True) or 0
            if args.show_prompt:
                print("=== SYSTEM ===\n" + r.prompt.system + "\n\n=== USER ===\n" + r.prompt.user + "\n\n=== RESPONSE ===")
            print(r.text)
            if r.checkpoint:
                print(f"\n⏸ Checkpoint: {r.checkpoint}")
    except (RegistryError, ProviderError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if r.offers and not args.quiet:
        print("\nNext, I can:", file=sys.stderr)
        for o in r.offers:
            print(f"  - {o}", file=sys.stderr)
    if r.artifact:
        print(f"\nsaved → {r.artifact}", file=sys.stderr)
    if session:
        print(f"session: {session.id}", file=sys.stderr)
    return 0


def cmd_chat(args) -> int:
    eng = _engine(args)
    session = Session.load(args.session) if args.session else Session.new()
    print(f"AURA PM Engine v{__version__} — provider {eng.provider.name}/{eng.provider.model}. Type /help, /quit.")
    print(f"session {session.id}")
    while True:
        try:
            line = input("\npm> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line == "/help":
            print("Commands: " + ", ".join(f"/{c.name}" for c in eng.registry.commands))
            print("Force a skill with /skill-name, or just describe what you need.")
            continue
        if line.startswith("/attach "):
            path = line.split(" ", 1)[1].strip()
            try:
                att = Attachment.from_path(path)
            except OSError as e:
                print(f"error: {e}")
                continue
            session.add(__import__("pm_engine.session", fromlist=["Turn"]).Turn(role="user", content=att.render()))
            print(f"attached {att.name} ({len(att.text)} chars)")
            continue
        try:
            r = eng.run(line, session=session, progress=lambda m: print(f"  · {m}"))
        except (RegistryError, ProviderError, ValueError) as e:
            print(f"error: {e}")
            continue
        print()
        print(r.text)
        if r.checkpoint:
            print(f"\n⏸ {r.checkpoint}")
        if r.offers:
            print("\nNext, I can:")
            for o in r.offers:
                print(f"  - {o}")
    session.save()
    print(f"saved session {session.id}")
    return 0


def cmd_validate(args) -> int:
    reg = _registry(args)
    rep = validate_all(reg)
    if args.json:
        _print(rep.to_dict(), True)
    else:
        for e in rep.errors:
            print(f"✗ {e}")
        if args.verbose:
            for w in rep.warnings:
                print(f"⚠ {w}")
        info = rep.info.get("registry", {})
        print(f"plugins={info.get('plugins')} skills={info.get('skills')} commands={info.get('commands')} workflow_steps={info.get('workflow_steps')} version={rep.info.get('version')}")
        print("✓ ALL CHECKS PASSED" if rep.ok else f"✗ {len(rep.errors)} errors", f"({len(rep.warnings)} warnings)")
    return 0 if rep.ok else 1


def cmd_test(args) -> int:
    reg = _registry(args)
    root = reg.root
    rc = 0
    print(f"▶ upstream validator ({root})")
    rc |= subprocess.run([sys.executable, str(root / "validate_plugins.py")], cwd=root).returncode
    print("▶ upstream unittest suite")
    rc |= subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v" if args.verbose else "-q"], cwd=root).returncode
    engine_tests = Path(__file__).resolve().parent.parent / "tests"
    if engine_tests.is_dir():
        print("▶ engine test suite")
        try:
            import pytest  # noqa: F401

            rc |= subprocess.run([sys.executable, "-m", "pytest", str(engine_tests), "-q" if not args.verbose else "-v"], cwd=engine_tests.parent).returncode
        except ImportError:
            rc |= subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(engine_tests), "-v" if args.verbose else "-q"], cwd=engine_tests.parent).returncode
    print("✓ all suites passed" if rc == 0 else "✗ failures")
    return rc


def cmd_export(args) -> int:
    reg = _registry(args)
    rep = export(reg, args.target, args.dest, plugins=args.plugin or None)
    if args.json:
        _print(rep.to_dict(), True)
    else:
        print(f"exported {rep.skills} skills, {rep.commands} commands → {rep.destination}")
    return 0


def cmd_serve(args) -> int:
    from .server import create_app

    eng = _engine(args)
    app = create_app(eng)
    print(f"AURA PM Engine API on http://{args.host}:{args.port}  (provider {eng.provider.name}/{eng.provider.model})")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


def cmd_update(args) -> int:
    from .updater import current_upstream, update

    reg = _registry(args)
    before = current_upstream(reg.root)
    res = update(reg.root, ref=args.ref)
    print(f"vendored upstream {res.commit[:12]} (v{res.version}) → {res.destination}; plugins: {', '.join(res.plugins)}")
    if before:
        print(f"previous: {before}")
    return 0


def cmd_sessions(args) -> int:
    if args.delete:
        Session.load(args.delete).delete()
        print(f"deleted {args.delete}")
        return 0
    rows = [s.summary() for s in Session.list()]
    if args.json:
        return _print(rows, True) or 0
    for s in rows:
        print(f"{s['id']}  turns={s['turns']:<3} {('cmd=' + s['active_command']) if s['active_command'] else '':<36} {s['title'][:60]}")
    if not rows:
        print("no sessions")
    return 0


def cmd_providers(args) -> int:
    _print(available_providers(), True)
    return 0


# ─── parser ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pm-engine", description="AURA PM Engine — run the PM Skills marketplace anywhere.")
    p.add_argument("--version", action="version", version=f"pm-engine {__version__}")
    p.add_argument("--marketplace", "-m", help="path to a marketplace / plugin directory (default: vendored pm-skills or $PM_SKILLS_PATH)")
    p.add_argument("--provider", choices=["anthropic", "openai", "ollama", "offline"], help="LLM backend (default: auto from env)")
    p.add_argument("--model", help="model id for the provider")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="list plugins / skills / commands")
    s.add_argument("what", nargs="?", choices=["plugins", "skills", "commands", "stats"], default="stats")
    s.add_argument("--plugin")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="show a skill or command")
    s.add_argument("ref")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("search", help="search skills and commands")
    s.add_argument("query", nargs="+")
    s.add_argument("--kind", choices=["skill", "command"])
    s.add_argument("--plugin")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("resolve", help="show how a request would be routed")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("workflow", help="show the parsed workflow of a command")
    s.add_argument("command")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_workflow)

    s = sub.add_parser("run", help="run a command, skill, or free-text request")
    s.add_argument("text", nargs="+")
    s.add_argument("--file", "-f", action="append", help="attach a file (repeatable)")
    s.add_argument("--skill", action="append", help="force-load an extra skill (repeatable)")
    s.add_argument("--step", type=int, help="run only this workflow step (1-based)")
    s.add_argument("--all-steps", action="store_true", help="run each step in turn, pausing at checkpoints")
    s.add_argument("--session", help="continue a saved session")
    s.add_argument("--save-session", action="store_true")
    s.add_argument("--out", help="directory to save artifacts")
    s.add_argument("--max-tokens", type=int, default=4096)
    s.add_argument("--show-prompt", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--quiet", "-q", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("chat", help="interactive REPL")
    s.add_argument("--session")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_chat)

    s = sub.add_parser("validate", help="validate the marketplace (spec + engine)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--verbose", "-v", action="store_true")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("test", help="run upstream + engine test suites")
    s.add_argument("--verbose", "-v", action="store_true")
    s.set_defaults(fn=cmd_test)

    s = sub.add_parser("export", help="export skills to another assistant")
    s.add_argument("target", choices=["claude", "cursor", "gemini", "opencode", "kiro", "codex", "bundle", "prompts"])
    s.add_argument("dest")
    s.add_argument("--plugin", action="append")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("serve", help="start the JSON API + web UI")
    s.add_argument("--host", default=os.environ.get("PM_ENGINE_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PM_ENGINE_PORT", "8080")))
    s.add_argument("--out", default=os.environ.get("PM_ENGINE_ARTIFACTS"))
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("update-skills", help="re-vendor the marketplace from upstream")
    s.add_argument("--ref", default="main")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("sessions", help="list / delete saved sessions")
    s.add_argument("--delete")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("providers", help="show which LLM providers are configured")
    s.set_defaults(fn=cmd_providers)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except RegistryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
