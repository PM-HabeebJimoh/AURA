import json
import subprocess
import sys
from pathlib import Path

import pytest

from pm_engine.cli import main
from pm_engine.export import export
from pm_engine.validator import validate_all, validate_engine, validate_spec

ROOT = Path(__file__).resolve().parent.parent


# ─── API ─────────────────────────────────────────────────────────────────────


def test_health_and_catalogue(client):
    h = client.get("/api/health").get_json()
    assert h["ok"] and h["plugins"] == 9 and h["skills"] == 68 and h["commands"] == 42 and h["provider"] == "offline"
    assert len(client.get("/api/plugins").get_json()) == 9
    p = client.get("/api/plugins/pm-toolkit").get_json()
    assert p["readme"].startswith("# pm-toolkit") and len(p["skills"]) == 4
    assert len(client.get("/api/skills").get_json()) == 68
    assert len(client.get("/api/skills?plugin=pm-ai-shipping").get_json()) == 2
    s = client.get("/api/skills/pm-execution:pre-mortem").get_json()
    assert s["body"] and "/pre-mortem" in s["used_by"]
    cmds = client.get("/api/commands").get_json()
    assert len(cmds) == 42 and all(c["steps"] > 0 for c in cmds)
    c = client.get("/api/commands/discover").get_json()
    assert len(c["workflow"]["steps"]) == 7
    assert client.get("/api/commands/nope").status_code == 404


def test_search_graph_validate_providers(client):
    hits = client.get("/api/search?q=north%20star%20metric&limit=3").get_json()
    assert hits[0]["name"] == "north-star-metric"
    g = client.get("/api/graph").get_json()
    assert len(g["nodes"]) == 110 and g["edges"]
    v = client.get("/api/validate").get_json()
    assert v["ok"], v["errors"]
    pr = client.get("/api/providers").get_json()
    assert pr["active"]["provider"] == "offline" and pr["available"]["offline"] is True


def test_resolve_prompt_run_and_sessions(client):
    r = client.post("/api/resolve", json={"text": "/write-prd SSO"}).get_json()
    assert r["kind"] == "command"
    p = client.post("/api/prompt", json={"text": "/sprint retro rough sprint", "step": 1}).get_json()
    assert p["mode"] == "retro" and "Step 1" in p["system"]
    p2 = client.post("/api/prompt", json={"text": "Draft an NDA between Acme and a contractor"}).get_json()
    assert p2["skills"] == ["pm-toolkit:draft-nda"]
    p3 = client.post("/api/prompt", json={"text": "/lean-canvas X"}).get_json()
    assert p3["skills"] == ["pm-product-strategy:lean-canvas"]

    run = client.post("/api/run", json={"text": "/discover AI meeting summarizer", "step": 1, "attachments": [{"name": "brief.md", "text": "context"}]})
    assert run.status_code == 200
    body = run.get_json()
    sid = body["session"]["id"]
    assert body["step"] == 1 and body["total_steps"] == 7 and body["artifact"]
    run2 = client.post("/api/run", json={"text": "all of them", "session_id": sid}).get_json()
    assert run2["step"] == 2 and run2["checkpoint"]
    sessions = client.get("/api/sessions").get_json()
    assert any(s["id"] == sid for s in sessions)
    full = client.get(f"/api/sessions/{sid}").get_json()
    assert len(full["turns"]) == 4
    assert client.delete(f"/api/sessions/{sid}").get_json()["deleted"] == sid
    assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_run_error_paths(client):
    assert client.post("/api/run", json={"text": "/writeprd X"}).status_code == 404
    assert client.post("/api/run", json={"text": "/write-prd X", "step": 99}).status_code == 400


def test_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and b"AURA PM Engine" in r.data
    assert r.headers["X-PM-Engine"]


# ─── export ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("target,dirname", [("cursor", ".cursor"), ("gemini", ".gemini"), ("opencode", ".opencode"), ("kiro", ".kiro")])
def test_export_skill_folders(registry, tmp_path, target, dirname):
    rep = export(registry, target, tmp_path)
    assert rep.skills == 68 and rep.commands == 0
    assert (tmp_path / dirname / "skills" / "lean-canvas" / "SKILL.md").is_file()
    assert len(list((tmp_path / dirname / "skills").iterdir())) == 68


def test_export_claude_includes_commands(registry, tmp_path):
    rep = export(registry, "claude", tmp_path)
    assert rep.commands == 42 and (tmp_path / ".claude" / "commands" / "discover.md").is_file()


def test_export_codex_converts_commands(registry, tmp_path):
    rep = export(registry, "codex", tmp_path, plugins=["pm-execution"])
    assert rep.skills == 16 and rep.commands == 11
    text = (tmp_path / ".codex" / "skills" / "write-prd-workflow" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: write-prd-workflow")
    assert "converted from a slash command" in text


def test_export_bundle_and_prompts(registry, tmp_path):
    rep = export(registry, "bundle", tmp_path)
    data = json.loads(rep.destination.read_text(encoding="utf-8"))
    assert data["marketplace"]["version"] == "2.1.0"
    assert sum(len(p["skills"]) for p in data["plugins"]) == 68
    assert all("workflow" in c for p in data["plugins"] for c in p["commands"])
    rep2 = export(registry, "prompts", tmp_path / "prompts", plugins=["pm-toolkit"])
    assert rep2.skills == 4 and rep2.commands == 5
    f = tmp_path / "prompts" / "pm-toolkit" / "command-proofread.prompt.md"
    assert "<command name=\"/proofread\"" in f.read_text(encoding="utf-8")


def test_export_unknown_target(registry, tmp_path):
    with pytest.raises(ValueError):
        export(registry, "vim", tmp_path)


# ─── validation ──────────────────────────────────────────────────────────────


def test_upstream_spec_validation_passes(registry):
    rep = validate_spec(registry)
    assert rep.ok, rep.errors
    assert rep.info["spec_totals"] == {"skills": 68, "commands": 42, "plugins": 9}


def test_engine_validation_passes(registry):
    rep = validate_engine(registry)
    assert rep.ok, rep.errors
    assert rep.info["registry"]["workflow_steps"] >= 190
    assert rep.info["version"] == "2.1.0"
    assert validate_all(registry).ok


def test_engine_validation_catches_broken_plugin(tmp_path):
    from pm_engine.registry import Registry

    plug = tmp_path / "pm-broken"
    (plug / ".claude-plugin").mkdir(parents=True)
    (plug / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "pm-broken", "version": "0.0.1", "description": "broken plugin for tests"}))
    (plug / "commands").mkdir()
    (plug / "commands" / "do-it.md").write_text("---\ndescription: does it\nargument-hint: \"<x>\"\n---\n# /do-it\n\n## Workflow\n\n### Step 1: Go\nApply the **missing-skill** skill.\n")
    rep = validate_engine(Registry(plug))
    assert not rep.ok and any("missing-skill" in e for e in rep.errors)


def test_upstream_test_suite_passes(marketplace_path):
    """The vendored upstream unittest suite (README counts, version sync, changelog) must stay green."""
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=marketplace_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "Ran 15 tests" in proc.stderr


def test_upstream_validator_script_passes(marketplace_path):
    proc = subprocess.run([sys.executable, "validate_plugins.py"], cwd=marketplace_path, capture_output=True, text=True)
    assert proc.returncode == 0
    assert "ALL CHECKS PASSED" in proc.stdout


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_list_and_show(capsys, marketplace_path):
    assert main(["-m", str(marketplace_path), "list"]) == 0
    out = capsys.readouterr().out
    assert "Skills: 68" in out and "Commands: 42" in out
    assert main(["-m", str(marketplace_path), "list", "commands", "--plugin", "pm-toolkit", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 5
    assert main(["-m", str(marketplace_path), "show", "/discover"]) == 0
    assert "Steps: 7" in capsys.readouterr().out
    assert main(["-m", str(marketplace_path), "show", "lean-canvas", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["plugin"] == "pm-product-strategy"
    assert main(["-m", str(marketplace_path), "show", "nope"]) == 2


def test_cli_search_workflow_resolve(capsys, marketplace_path):
    assert main(["-m", str(marketplace_path), "search", "north", "star", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "north-star-metric"
    assert main(["-m", str(marketplace_path), "workflow", "/sprint"]) == 0
    out = capsys.readouterr().out
    assert "Mode: plan" in out and "Mode: retro" in out
    assert main(["-m", str(marketplace_path), "resolve", "/write-prd", "SSO"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "command"


def test_cli_run_validate_export(capsys, marketplace_path, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_ENGINE_HOME", str(tmp_path / "home"))
    assert main(["-m", str(marketplace_path), "--provider", "offline", "run", "/write-prd", "SSO", "--out", str(tmp_path / "art"), "--json", "-q"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["command"] == "pm-execution:write-prd" and Path(r["artifact"]).is_file()
    assert main(["-m", str(marketplace_path), "--provider", "offline", "run", "/writeprd", "SSO", "-q"]) == 2
    assert main(["-m", str(marketplace_path), "validate"]) == 0
    assert "ALL CHECKS PASSED" in capsys.readouterr().out
    assert main(["-m", str(marketplace_path), "export", "cursor", str(tmp_path / "exp")]) == 0
    assert (tmp_path / "exp" / ".cursor" / "skills").is_dir()
    assert main(["-m", str(marketplace_path), "sessions"]) == 0
    assert main(["providers"]) == 0


def test_cli_entrypoint_module():
    proc = subprocess.run([sys.executable, "-m", "pm_engine.cli", "--version"], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0 and "pm-engine" in proc.stdout
