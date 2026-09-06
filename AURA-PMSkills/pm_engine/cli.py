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
    pm-engine new marketplace|plugin|skill|command <name> [--root DIR] [--plugin P] [--skill S ...]
    pm-engine doctor                                  # environment + marketplace health check
    pm-engine setup arena [--key K] [--base-url U] [--model M] [--check]   # store credentials in ~/.aura/pm-engine.env
    pm-engine providers [--check]                     # configured backends (+ live connectivity test)
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
from .config import load_env_files, loaded_env_files, redact, save_env_values, user_env_file
from .providers import ArenaProvider, ProviderError, auto_provider, available_providers, make_provider
from .registry import Registry, RegistryError, default_extra_paths
from .runner import Engine
from .scaffold import ScaffoldError, new_command, new_marketplace, new_plugin, new_skill
from .session import Session
from .validator import validate_all
from .workflow import parse_workflow


def _registry(args) -> Registry:
    root = getattr(args, "marketplace", None)
    extra = getattr(args, "extra", None)
    if getattr(args, "no_extra", False):
        return Registry(root, extra_roots=())
    if extra:
        return Registry(root, extra_roots=[*default_extra_paths(), *extra])
    return Registry(root)


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
        elif args.stream and not args.json:
            r = None
            if args.show_prompt:
                pv = eng.resolve(text)
                print(f"# routing: {json.dumps(pv)}", file=sys.stderr)
            for item in eng.stream(text, attachments=atts, session=session, step=args.step, extra_skills=args.skill or [], max_tokens=args.max_tokens, progress=log):
                if isinstance(item, str):
                    sys.stdout.write(item)
                    sys.stdout.flush()
                else:
                    r = item
            print()
            if r and r.checkpoint:
                print(f"\n⏸ Checkpoint: {r.checkpoint}")
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
            print()
            r = None
            for item in eng.stream(line, session=session, progress=lambda m: print(f"  · {m}")):
                if isinstance(item, str):
                    sys.stdout.write(item)
                    sys.stdout.flush()
                else:
                    r = item
            print()
        except (RegistryError, ProviderError, ValueError) as e:
            print(f"error: {e}")
            continue
        if r is None:
            continue
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
    env = {k: v for k, v in os.environ.items() if not (k.startswith(("ARENA_", "ANTHROPIC_", "OPENAI_", "OLLAMA_")) or k in ("PM_ENGINE_PROVIDER", "PM_ENGINE_ENV_FILE"))}
    env["PM_ENGINE_PROVIDER"] = "offline"  # suites are hermetic: never touch a real model backend
    rc |= subprocess.run([sys.executable, str(root / "validate_plugins.py")], cwd=root, env=env).returncode
    print("▶ upstream unittest suite")
    rc |= subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v" if args.verbose else "-q"], cwd=root, env=env).returncode
    engine_tests = Path(__file__).resolve().parent.parent / "tests"
    if engine_tests.is_dir():
        print("▶ engine test suite")
        try:
            import pytest  # noqa: F401

            rc |= subprocess.run([sys.executable, "-m", "pytest", str(engine_tests), "-q" if not args.verbose else "-v"], cwd=engine_tests.parent, env=env).returncode
        except ImportError:
            rc |= subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(engine_tests), "-v" if args.verbose else "-q"], cwd=engine_tests.parent, env=env).returncode
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
    load_env_files()
    avail = available_providers()
    if not getattr(args, "check", False):
        _print(avail, True)
        return 0
    from .providers import no_retries

    out: dict = {"available": avail, "env_files": [str(p) for p in loaded_env_files()], "checks": {}}
    ok = True
    for name, configured in avail.items():
        if not configured or name == "offline":
            continue
        try:
            prov = make_provider(name, **({"model": args.model} if getattr(args, "model", None) else {}))
            if hasattr(prov, "check"):
                res = prov.check()
            else:
                with no_retries():
                    c = prov.complete("Reply with the single word OK.", [{"role": "user", "content": "ping"}], max_tokens=8, temperature=0)
                res = {"ok": True, "model": c.model, "reply": c.text.strip()[:40], "latency_s": round(c.latency_s, 2)}
        except ProviderError as e:
            res = {"ok": False, "error": str(e)[:300], "status": e.status}
        out["checks"][name] = res
        ok = ok and bool(res.get("ok"))
    if getattr(args, "json", False):
        _print(out, True)
    else:
        for name, configured in avail.items():
            mark = "✓" if configured else "·"
            print(f"{mark} {name:<10} {'configured' if configured else 'not configured'}")
        for name, res in out["checks"].items():
            if res.get("ok"):
                print(f"  ✓ {name}: reachable — model {res.get('model')}, replied {res.get('reply')!r} in {res.get('latency_s')}s" + (f" (format {res['format']})" if res.get("format") else ""))
            else:
                print(f"  ✗ {name}: {res.get('error')}")
            if res.get("warning"):
                print(f"    ⚠ {res['warning']}")
    return 0 if ok else 1


def cmd_setup(args) -> int:
    """Store provider credentials in the user-level env file and (optionally) test them."""
    import getpass

    values: dict[str, str] = {}
    if args.provider == "arena":
        key = args.key or os.environ.get("ARENA_API_KEY_NEW") or ""
        if not key:
            if not sys.stdin.isatty():
                print("error: pass --key (stdin is not a terminal)", file=sys.stderr)
                return 2
            key = getpass.getpass("Arena API key (input hidden): ").strip()
        if not key:
            print("error: empty key", file=sys.stderr)
            return 2
        values["ARENA_API_KEY"] = key
        if args.base_url:
            values["ARENA_BASE_URL"] = args.base_url.rstrip("/")
        if args.model:
            values["ARENA_MODEL"] = args.model
        if args.api_format:
            values["ARENA_API_FORMAT"] = args.api_format
        if args.auth_header:
            values["ARENA_AUTH_HEADER"] = args.auth_header
        if not args.no_default:
            values["PM_ENGINE_PROVIDER"] = "arena"
    else:  # anthropic | openai
        key = args.key or ""
        if not key:
            if not sys.stdin.isatty():
                print("error: pass --key (stdin is not a terminal)", file=sys.stderr)
                return 2
            key = getpass.getpass(f"{args.provider} API key (input hidden): ").strip()
        values[{"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}[args.provider]] = key
        if args.base_url:
            values[{"anthropic": "ANTHROPIC_BASE_URL", "openai": "OPENAI_BASE_URL"}[args.provider]] = args.base_url.rstrip("/")
        if args.model:
            values[{"anthropic": "PM_ENGINE_ANTHROPIC_MODEL", "openai": "PM_ENGINE_OPENAI_MODEL"}[args.provider]] = args.model
        if not args.no_default:
            values["PM_ENGINE_PROVIDER"] = args.provider
    path = save_env_values(values, Path(args.env_file) if args.env_file else None)
    print(f"saved {', '.join(k for k in values)} → {path}  (mode 0600; not tracked by git)")
    for k in values:
        if k.endswith("_KEY"):
            print(f"  {k} = {redact(values[k])}")
    if args.check:
        print("checking connectivity…")
        class _A:  # minimal namespace for cmd_providers
            check = True
            json = False
            model = None
        return cmd_providers(_A())
    print("next: `pm-engine providers --check`  then  `pm-engine run \"/write-prd …\" --stream`")
    return 0


def _default_authoring_root() -> Path:
    return Path(__file__).resolve().parent.parent / "aura-skills"


def cmd_new(args) -> int:
    root = Path(args.root).expanduser().resolve() if args.root else _default_authoring_root()
    try:
        if args.kind == "marketplace":
            created = new_marketplace(root, args.name, owner=args.author, description=args.description or "")
        elif args.kind == "plugin":
            if not (root / ".claude-plugin" / "marketplace.json").is_file():
                new_marketplace(root, root.name if _NAME_OK(root.name) else "aura-skills", owner=args.author)
                print(f"created marketplace at {root}", file=sys.stderr)
            created = new_plugin(root, args.name, description=args.description or "", author=args.author)
        else:
            if not args.plugin:
                print("error: --plugin is required for skills and commands", file=sys.stderr)
                return 2
            pdir = root / args.plugin
            if not pdir.is_dir():
                print(f"error: plugin '{args.plugin}' not found under {root} (create it with: pm-engine new plugin {args.plugin})", file=sys.stderr)
                return 2
            if args.kind == "skill":
                created = new_skill(pdir, args.name, description=args.description or "", triggers=args.triggers or "")
            else:
                created = new_command(pdir, args.name, description=args.description or "", argument_hint=args.argument_hint, skills=args.skill or [])
    except ScaffoldError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        _print(created.to_dict(), True)
    else:
        print(f"created {created.kind}: {created.path}")
        for f in created.files:
            print(f"  {f}")
        if created.kind in ("skill", "command"):
            print("next: edit the file, then run `pm-engine validate -v`")
    return 0


def _NAME_OK(name: str) -> bool:
    import re as _re

    return bool(_re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", name))


def cmd_doctor(args) -> int:
    import platform

    ok = True
    print(f"pm-engine {__version__} · Python {platform.python_version()} · {platform.system()} {platform.machine()}")
    for mod, why in (("flask", "API + web UI"), ("yaml", "front-matter fidelity (optional)"), ("pytest", "engine tests (dev)")):
        try:
            __import__(mod)
            print(f"  ✓ {mod:<8} available   ({why})")
        except ImportError:
            print(f"  · {mod:<8} missing     ({why})")
    try:
        reg = _registry(args)
    except (RegistryError, OSError, ValueError) as e:
        print(f"  ✗ registry failed to load: {e}")
        return 1
    st = reg.stats()
    print(f"  ✓ primary marketplace: {st['root']}  ({st['marketplace']['name'] + ' v' + st['marketplace']['version'] if st['marketplace'] else 'bare plugin dir'})")
    for r in st["extra_roots"]:
        print(f"  ✓ extra root: {r}")
    print(f"  ✓ {st['plugins']} plugins · {st['skills']} skills · {st['commands']} commands")
    up = reg.root / "UPSTREAM"
    if up.is_file():
        from .updater import current_upstream

        info = current_upstream(reg.root)
        print(f"  ✓ vendored upstream: {info.get('commit', '?')[:12]} (v{info.get('version', '?')})")
    rep = validate_all(reg)
    if rep.ok:
        print(f"  ✓ validation passed ({len(rep.warnings)} warnings)")
    else:
        ok = False
        print(f"  ✗ validation: {len(rep.errors)} errors — run `pm-engine validate`")
    prov = available_providers()
    files = loaded_env_files()
    if files:
        print(f"  ✓ env files: {', '.join(str(f) for f in files)}")
    else:
        print(f"  · env files: none found (create one with `pm-engine setup arena` → {user_env_file()})")
    try:
        active = auto_provider()
    except ProviderError as e:
        ok = False
        print(f"  ✗ provider configuration: {e}")
        active = None
    if active is not None:
        print(f"  ✓ active provider: {active.name}/{active.model}" + ("  (offline scaffolds — run `pm-engine setup arena` or set ARENA_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY / OLLAMA_HOST for model output)" if active.name == "offline" else ""))
        if isinstance(active, ArenaProvider):
            print(f"    base url {active.base_url} · format {active.api_format} · key {redact(active.api_key)}")
            if getattr(args, "check", False):
                res = active.check()
                if res.get("ok"):
                    print(f"    ✓ reachable — model {res['model']} replied {res['reply']!r} in {res['latency_s']}s (format {res['format']})")
                else:
                    ok = False
                    print(f"    ✗ not reachable: {res.get('error')}")
    for name, configured in prov.items():
        if name != "offline":
            print(f"    {'✓' if configured else '·'} {name}")
    from .session import sessions_dir

    print(f"  ✓ sessions dir: {sessions_dir()} ({len(list(Session.list()))} sessions)")
    print("doctor: " + ("all good" if ok else "issues found"))
    return 0 if ok else 1


# ─── parser ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pm-engine", description="AURA PM Engine — run the PM Skills marketplace anywhere.")
    p.add_argument("--version", action="version", version=f"pm-engine {__version__}")
    p.add_argument("--marketplace", "-m", help="path to a marketplace / plugin directory (default: vendored pm-skills or $PM_SKILLS_PATH)")
    p.add_argument("--extra", action="append", help="additional marketplace/plugin directory to merge (repeatable; also $PM_SKILLS_EXTRA and ./aura-skills)")
    p.add_argument("--no-extra", action="store_true", help="load only the primary marketplace")
    p.add_argument("--provider", choices=["arena", "anthropic", "openai", "ollama", "offline"], help="LLM backend (default: auto from env / .env files)")
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
    s.add_argument("--stream", action="store_true", help="stream the response as it is generated")
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
    s.add_argument("--check", action="store_true", help="make one tiny request to every configured provider")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_providers)

    s = sub.add_parser("setup", help="store an API key / endpoint for a provider (arena, anthropic, openai)")
    s.add_argument("provider", choices=["arena", "anthropic", "openai"])
    s.add_argument("--key", help="API key (omit to be prompted without echo)")
    s.add_argument("--base-url", help="API base URL (arena default: https://api.arena.ai/v1)")
    s.add_argument("--model", help="default model id")
    s.add_argument("--api-format", choices=["auto", "openai", "anthropic"], help="arena wire format (default auto-detect)")
    s.add_argument("--auth-header", help="arena auth header: bearer (default) | x-api-key | <custom header name>")
    s.add_argument("--env-file", help="where to save (default: ~/.aura/pm-engine.env or $PM_ENGINE_HOME/pm-engine.env)")
    s.add_argument("--no-default", action="store_true", help="do not make this the default provider (PM_ENGINE_PROVIDER)")
    s.add_argument("--check", action="store_true", help="test the connection after saving")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("new", help="scaffold a marketplace, plugin, skill, or command in the upstream format")
    s.add_argument("kind", choices=["marketplace", "plugin", "skill", "command"])
    s.add_argument("name", help="kebab-case name")
    s.add_argument("--root", help="authoring marketplace directory (default: AURA-PMSkills/aura-skills)")
    s.add_argument("--plugin", help="plugin to add the skill/command to")
    s.add_argument("--description", "-d")
    s.add_argument("--triggers", help="skill trigger sentence ('Use when …')")
    s.add_argument("--argument-hint", default="<product, feature, or question>")
    s.add_argument("--skill", action="append", help="skill(s) the new command chains (must exist in the same plugin)")
    s.add_argument("--author", default="AURA")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("doctor", help="check environment, marketplace health, and provider configuration")
    s.add_argument("--check", action="store_true", help="also make a live request to the active provider")
    s.set_defaults(fn=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    load_env_files()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except (RegistryError, ProviderError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
