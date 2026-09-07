"""Round-2 features: streaming, retries, SSE endpoint, multi-root registry, scaffolding, doctor, and AURA's own plugin."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pm_engine import providers
from pm_engine.cli import main
from pm_engine.providers import OfflineProvider, ProviderError, iter_chunks
from pm_engine.registry import Registry, RegistryError, default_extra_paths
from pm_engine.runner import Engine, RunResult
from pm_engine.scaffold import ScaffoldError, new_command, new_marketplace, new_plugin, new_skill
from pm_engine.validator import validate_all, validate_spec

ROOT = Path(__file__).resolve().parent.parent


# ─── providers: retries + streaming ──────────────────────────────────────────


def test_provider_error_retryable_classification():
    assert ProviderError("HTTP 529 overloaded", status=529).retryable
    assert ProviderError("HTTP 429 rate limited", status=429).retryable
    assert ProviderError("HTTP 503", status=503).retryable
    assert not ProviderError("HTTP 401 unauthorised", status=401).retryable
    assert not ProviderError("HTTP 400 bad request", status=400).retryable
    assert ProviderError("network error calling x: timed out").retryable
    assert not ProviderError("something else entirely").retryable


def test_with_retries_retries_transient_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderError("HTTP 529", status=529)
        return "ok"

    assert providers._with_retries(flaky, retries=3, base_delay=0.5) == "ok"
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]  # exponential backoff


def test_with_retries_gives_up_after_budget_and_never_retries_fatal(monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_529():
        calls["n"] += 1
        raise ProviderError("HTTP 529", status=529)

    with pytest.raises(ProviderError):
        providers._with_retries(always_529, retries=2)
    assert calls["n"] == 3  # 1 try + 2 retries

    calls["n"] = 0

    def fatal():
        calls["n"] += 1
        raise ProviderError("HTTP 401", status=401)

    with pytest.raises(ProviderError):
        providers._with_retries(fatal, retries=5)
    assert calls["n"] == 1


def test_iter_chunks_roundtrip():
    text = "x" * 1000 + " tail"
    parts = list(iter_chunks(text, size=120))
    assert "".join(parts) == text and len(parts) >= 8 and all(len(p) <= 120 for p in parts[:-1])
    assert list(iter_chunks("", size=10)) == []


def test_offline_provider_stream_matches_complete():
    prov = OfflineProvider()
    system = "<skill name=\"x\">\n## Output Template\n```\n## Report: [Topic]\n[Findings]\n```\n</skill>"
    msgs = [{"role": "user", "content": "hello"}]
    full = prov.complete(system, msgs).text
    streamed = "".join(prov.stream(system, msgs))
    assert streamed == full and len(list(prov.stream(system, msgs))) >= 1


class _StubStreamProvider:
    """A provider that only implements stream(); used to prove Engine.stream drives the provider's stream path."""

    name = "stub"
    model = "stub-1"

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, *, max_tokens=4096, temperature=0.4):  # pragma: no cover - must not be called
        raise AssertionError("Engine.stream must use provider.stream(), not complete()")

    def stream(self, system, messages, *, max_tokens=4096, temperature=0.4):
        self.calls += 1
        for piece in ("## PRD: SSO\n", "\n### Problem\n", "Users need SSO.\n"):
            yield piece


# ─── Engine.stream ───────────────────────────────────────────────────────────


def test_engine_stream_yields_chunks_then_result(engine):
    from pm_engine.session import Session

    items = list(engine.stream("/write-prd SSO for enterprise", session=Session.new()))
    assert isinstance(items[-1], RunResult)
    chunks = [i for i in items[:-1] if isinstance(i, str)]
    assert chunks and "".join(chunks) == items[-1].text
    result = items[-1]
    assert result.command.qualified_name == "pm-execution:write-prd" and result.artifact and Path(result.artifact).is_file()
    assert result.offers and result.session is not None and len(result.session.turns) == 2


def test_engine_stream_uses_provider_stream_and_records_session(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_ENGINE_HOME", str(tmp_path / "home"))
    prov = _StubStreamProvider()
    eng = Engine(registry, prov, artifacts_dir=tmp_path / "art")
    from pm_engine.session import Session

    items = list(eng.stream("/write-prd SSO", session=Session.new(), save_artifact=False))
    assert prov.calls == 1
    assert items[:-1] == ["## PRD: SSO\n", "\n### Problem\n", "Users need SSO.\n"]
    res = items[-1]
    assert res.text == "## PRD: SSO\n\n### Problem\nUsers need SSO.\n" and res.completion.provider == "stub" and res.completion.model == "stub-1"
    assert res.artifact is None
    assert res.session.turns[-1].role == "assistant" and res.session.turns[-1].content == res.text
    assert Session.load(res.session.id).turns[-1].provider == "stub"


def test_engine_stream_step_and_auto_load(engine):
    items = list(engine.stream("/discover AI meeting notes", step=2))
    res = items[-1]
    assert res.step == 2 and res.total_steps == 7 and "Step 2" in res.text
    items = list(engine.stream("Draft an NDA between Acme and a contractor", save_artifact=False))
    assert [s.qualified_name for s in items[-1].skills] == ["pm-toolkit:draft-nda"]


# ─── /api/stream (SSE) ───────────────────────────────────────────────────────


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events = []
    for block in raw.strip().split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if ev:
            events.append((ev, data))
    return events


def test_api_stream_emits_chunks_then_result(client):
    resp = client.post("/api/stream", json={"text": "/write-prd SSO", "attachments": [{"name": "notes.md", "text": "ctx"}]})
    assert resp.status_code == 200 and resp.mimetype == "text/event-stream"
    assert resp.headers.get("Cache-Control", "").startswith("no-cache")
    events = _parse_sse(resp.get_data(as_text=True))
    kinds = [k for k, _ in events]
    assert kinds[-1] == "result" and kinds.count("chunk") >= 1 and set(kinds) == {"chunk", "result"}
    result = events[-1][1]
    assert "".join(d["text"] for k, d in events if k == "chunk") == result["text"]
    assert result["command"] == "pm-execution:write-prd" and result["session"]["id"]
    # the session persisted and can be continued through the normal endpoint
    sid = result["session"]["id"]
    s = client.get(f"/api/sessions/{sid}").get_json()
    assert len(s["turns"]) == 2


def test_api_stream_error_event_for_unknown_command(client):
    resp = client.post("/api/stream", json={"text": "/definitely-not-a-command x"})
    assert resp.status_code == 200
    events = _parse_sse(resp.get_data(as_text=True))
    assert events and events[-1][0] == "error" and "definitely-not-a-command" in events[-1][1]["error"]


# ─── multi-root registry ─────────────────────────────────────────────────────


def test_default_registry_merges_aura_skills(marketplace_path, aura_skills_path, monkeypatch):
    monkeypatch.delenv("PM_SKILLS_EXTRA", raising=False)
    assert aura_skills_path in default_extra_paths()
    reg = Registry(marketplace_path)  # extra_roots=None → defaults
    assert reg.roots == [marketplace_path.resolve(), aura_skills_path.resolve()]
    st = reg.stats()
    assert st["plugins"] == 10 and st["skills"] == 71 and st["commands"] == 43
    assert st["extra_roots"] == [str(aura_skills_path.resolve())]
    assert {m["name"] for m in st["marketplaces"]} == {"pm-skills", "aura-skills"}
    assert reg.plugin_root("aura-architecture") == aura_skills_path.resolve()
    assert reg.plugin_root("pm-toolkit") == marketplace_path.resolve()
    # primary-only view is unchanged
    assert Registry(marketplace_path, extra_roots=()).stats()["plugins"] == 9


def test_extra_roots_env_and_bare_plugin_dir(marketplace_path, aura_skills_path, monkeypatch):
    monkeypatch.setenv("PM_SKILLS_EXTRA", str(aura_skills_path / "aura-architecture"))
    paths = default_extra_paths()
    assert paths[0] == (aura_skills_path / "aura-architecture").resolve()
    reg = Registry(marketplace_path, extra_roots=[aura_skills_path / "aura-architecture"])
    assert "aura-architecture" in reg.plugins and reg.stats()["plugins"] == 10
    # the same plugin twice (bare dir + its marketplace) is a hard error, not a silent shadow
    with pytest.raises(RegistryError):
        Registry(marketplace_path, extra_roots=[aura_skills_path, aura_skills_path / "aura-architecture"])


def test_merged_registry_resolves_and_searches_aura_skills(merged_registry):
    from pm_engine.search import SkillIndex

    cmd = merged_registry.get_command("design-brief")
    assert cmd.plugin == "aura-architecture"
    assert cmd.referenced_skills == ["floor-plan-brief", "design-quality-score", "build-cost-check"]
    assert merged_registry.get_skill("aura-architecture:build-cost-check").plugin == "aura-architecture"
    idx = SkillIndex(merged_registry)
    top = idx.search("how much will a 4 bedroom duplex cost to build in Lagos", limit=1)[0]
    assert top.name == "build-cost-check"
    top = idx.search("is this floor plan any good? bedroom opens off the living room", limit=1)[0]
    assert top.plugin == "aura-architecture"


def test_merged_registry_validates_clean(merged_registry):
    rep = validate_all(merged_registry)
    assert rep.ok, rep.errors
    assert rep.warnings == [], rep.warnings
    assert rep.info["registry"] == {"plugins": 10, "skills": 71, "commands": 43, "workflow_steps": rep.info["registry"]["workflow_steps"]}
    assert set(rep.info["roots"]) == {str(r) for r in merged_registry.roots}
    assert rep.info["version"] == "2.1.0"  # primary marketplace version, unaffected by aura-skills 0.1.0


def test_aura_plugin_passes_upstream_validator(aura_skills_path):
    proc = subprocess.run([sys.executable, str(ROOT / "pm-skills" / "validate_plugins.py"), str(aura_skills_path)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL CHECKS PASSED" in proc.stdout


def test_run_aura_command_offline(merged_registry, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_ENGINE_HOME", str(tmp_path / "home"))
    eng = Engine(merged_registry, OfflineProvider(), artifacts_dir=tmp_path / "art")
    res = eng.run("/design-brief 3-bedroom bungalow, 450 sqm plot in Abuja, ₦45m budget")
    assert res.command.qualified_name == "aura-architecture:design-brief"
    assert [s.qualified_name for s in res.skills] == ["aura-architecture:floor-plan-brief", "aura-architecture:design-quality-score", "aura-architecture:build-cost-check"]
    assert "Design Brief" in res.text and Path(res.artifact).is_file()
    step4 = eng.run("/aura-architecture:design-brief 3-bed bungalow", step=4, save_artifact=False)
    assert step4.total_steps == 6 and "Sanity-Check the Budget" in step4.text


# ─── scaffold ────────────────────────────────────────────────────────────────


def test_scaffold_full_plugin_validates(tmp_path):
    root = tmp_path / "mkt"
    m = new_marketplace(root, "demo-skills", owner="Tester", description="test marketplace")
    assert (root / ".claude-plugin" / "marketplace.json").is_file() and m.kind == "marketplace"
    p = new_plugin(root, "pm-demo", description="Demo plugin for tests")
    assert (p.path / ".claude-plugin" / "plugin.json").is_file() and (p.path / "README.md").is_file()
    listed = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())["plugins"]
    assert [x["name"] for x in listed] == ["pm-demo"] and listed[0]["source"] == "./pm-demo"
    s1 = new_skill(p.path, "alpha-analysis", description="Analyse alpha things", triggers="Use when the user mentions alpha.")
    s2 = new_skill(p.path, "beta-analysis", description="Analyse beta things")
    c = new_command(p.path, "analyze-all", description="Chain alpha and beta", skills=["alpha-analysis", "beta-analysis"])
    assert s1.path.name == "alpha-analysis" and c.path.name == "analyze-all.md"
    readme = (p.path / "README.md").read_text()
    assert "## Skills (2)" in readme and "## Commands (1)" in readme and "/pm-demo:analyze-all" in readme

    reg = Registry(root, extra_roots=())
    assert reg.stats() == {**reg.stats(), "plugins": 1, "skills": 2, "commands": 1}
    cmd = reg.get_command("analyze-all")
    assert cmd.referenced_skills == ["alpha-analysis", "beta-analysis"]
    rep = validate_all(reg)
    assert rep.ok, rep.errors
    assert validate_spec(reg).ok
    # duplicate + bad names + unknown skill are rejected
    with pytest.raises(ScaffoldError):
        new_skill(p.path, "alpha-analysis")
    with pytest.raises(ScaffoldError):
        new_skill(p.path, "Bad_Name")
    with pytest.raises(ScaffoldError):
        new_command(p.path, "broken", skills=["nope"])
    with pytest.raises(ScaffoldError):
        new_plugin(root, "pm-demo")


def test_scaffolded_plugin_passes_upstream_validator(tmp_path):
    root = tmp_path / "mkt"
    new_marketplace(root, "demo-skills")
    p = new_plugin(root, "pm-demo", description="Demo plugin for the upstream validator")
    new_skill(p.path, "gamma-check", description="Check gamma", triggers="Use when gamma is mentioned.")
    new_command(p.path, "gamma", description="Run the gamma check", skills=["gamma-check"])
    proc = subprocess.run([sys.executable, str(ROOT / "pm-skills" / "validate_plugins.py"), str(root)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ─── CLI: new / doctor / --extra / --stream ───────────────────────────────────


def test_cli_new_and_extra_and_stream(capsys, marketplace_path, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_ENGINE_HOME", str(tmp_path / "home"))
    root = tmp_path / "my-skills"
    assert main(["new", "plugin", "pm-cli-demo", "--root", str(root), "-d", "CLI demo plugin", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["kind"] == "plugin" and (root / ".claude-plugin" / "marketplace.json").is_file()
    assert main(["new", "skill", "delta-review", "--root", str(root), "--plugin", "pm-cli-demo", "-d", "Review deltas"]) == 0
    assert main(["new", "command", "delta", "--root", str(root), "--plugin", "pm-cli-demo", "--skill", "delta-review", "-d", "Run a delta review"]) == 0
    capsys.readouterr()
    # missing --plugin / unknown plugin / bad name → exit 2
    assert main(["new", "skill", "x-y", "--root", str(root)]) == 2
    assert main(["new", "skill", "x-y", "--root", str(root), "--plugin", "nope"]) == 2
    assert main(["new", "plugin", "Bad Name", "--root", str(root)]) == 2
    capsys.readouterr()

    # --extra merges the new root; --no-extra excludes everything but the primary
    assert main(["-m", str(marketplace_path), "--no-extra", "--extra", str(root), "list", "--json"]) == 0
    capsys.readouterr()
    assert main(["-m", str(marketplace_path), "--extra", str(root), "show", "/delta", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["plugin"] == "pm-cli-demo"
    assert main(["-m", str(marketplace_path), "--no-extra", "show", "/delta"]) == 2
    capsys.readouterr()

    # streaming run prints the text incrementally and still saves an artifact
    assert main(["-m", str(marketplace_path), "--extra", str(root), "--provider", "offline", "run", "/delta", "the Q3 numbers", "--stream", "--out", str(tmp_path / "art"), "-q"]) == 0
    out = capsys.readouterr().out
    assert "Delta" in out and list((tmp_path / "art").glob("*.md"))


def test_cli_doctor(capsys, marketplace_path, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_ENGINE_HOME", str(tmp_path / "home"))
    assert main(["-m", str(marketplace_path), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "doctor: all good" in out and "validation passed" in out and "active provider: offline" in out
    assert "10 plugins · 71 skills · 43 commands" in out and "extra root:" in out
    assert main(["-m", str(marketplace_path), "--no-extra", "doctor"]) == 0
    assert "9 plugins · 68 skills · 42 commands" in capsys.readouterr().out


def test_api_health_reflects_merged_registry(merged_registry, tmp_path, monkeypatch):
    from pm_engine.server import create_app

    monkeypatch.setenv("PM_ENGINE_HOME", str(tmp_path / "home"))
    app = create_app(Engine(merged_registry, OfflineProvider(), artifacts_dir=tmp_path / "art"))
    app.config["TESTING"] = True
    c = app.test_client()
    h = c.get("/api/health").get_json()
    assert h["plugins"] == 10 and h["skills"] == 71 and h["commands"] == 43
    p = c.get("/api/plugins/aura-architecture").get_json()
    assert p["readme"].startswith("# aura-architecture") and len(p["skills"]) == 3
    assert c.get("/api/validate").get_json()["ok"]
