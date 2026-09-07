"""Round 4: lazy skill loading per mode/step and the input-token context budget."""

import json

import pytest

from pm_engine.cli import main
from pm_engine.prompt import Attachment, PromptBuilder, estimate_tokens
from pm_engine.providers import OfflineProvider
from pm_engine.runner import Engine
from pm_engine.session import Session
from pm_engine.workflow import parse_workflow


# ─── skill scoping ───────────────────────────────────────────────────────────


def test_workflow_skills_for_mode(registry):
    wf = parse_workflow(registry.get_command("/sprint"))
    assert wf.skills == ["sprint-plan", "retro", "release-notes"]
    assert wf.skills_for("retro") == ["retro"] and wf.skills_for("plan") == ["sprint-plan"]
    assert wf.skills_for(None) == wf.skills  # no mode → everything (document order)
    bm = parse_workflow(registry.get_command("/business-model"))
    assert bm.skills_for("lean") == ["lean-canvas"]
    assert bm.skills_for("all") == bm.skills  # template-only mode keeps every framework


def test_workflow_skills_up_to_step(registry):
    wf = parse_workflow(registry.get_command("/discover"))
    assert wf.skills_up_to(None, 1) == []  # "gather context" step loads nothing
    assert wf.skills_up_to(None, 2) == ["brainstorm-ideas-existing", "brainstorm-ideas-new"]
    assert wf.skills_up_to(None, 4) == ["brainstorm-ideas-existing", "brainstorm-ideas-new", "identify-assumptions-existing", "identify-assumptions-new", "prioritize-assumptions"]
    assert wf.skills_up_to(None, 7) == wf.skills  # by the last step everything has been introduced
    assert wf.skills_up_to(None, None) == wf.skills
    # skills referenced outside numbered steps (e.g. a Scope section) are always in play
    sc = parse_workflow(registry.get_command("/ship-check"))
    assert set(sc.skills_up_to(None, 1)) >= {"shipping-artifacts"}


def test_prompt_scopes_skills_by_step_and_mode(registry):
    pb = PromptBuilder(registry)
    cmd = registry.get_command("/market-scan")
    full = pb.for_command(cmd, "EV charging")
    s1 = pb.for_command(cmd, "EV charging", step=1)
    s2 = pb.for_command(cmd, "EV charging", step=2)
    assert len(full.skills) == 4 and s1.skills == [] and len(s2.skills) == 4
    assert s1.approx_tokens < full.approx_tokens / 3
    assert s1.metadata["skills_scope"] == "step" and full.metadata["skills_scope"] == "all"
    retro = pb.for_command(registry.get_command("/sprint"), "retro rough sprint")
    assert [s.name for s in retro.skills] == ["retro"] and retro.metadata["skills_scope"] == "mode"
    # extra skills are still appended after scoping
    p = pb.for_command(cmd, "x", step=1, extra_skills=[registry.get_skill("swot-analysis")])
    assert [s.name for s in p.skills] == ["swot-analysis"]


def test_lean_step_scoping(registry):
    wf = parse_workflow(registry.get_command("/discover"))
    assert wf.skills_at(None, 1) == [] and wf.skills_at(None, 7) == []  # gather-context / next-steps load nothing
    assert wf.skills_at(None, 3) == ["identify-assumptions-existing", "identify-assumptions-new"]  # just this step's
    assert wf.skills_at(None, 6) == []  # "Create Discovery Plan" names no skill → earlier results are in the history
    assert wf.skills_at(None, None) == wf.skills
    sc = parse_workflow(registry.get_command("/ship-check"))
    assert sc.skills_at(None, 1) == ["shipping-artifacts"] and sc.skills_at(None, 3) == ["intended-vs-implemented"]
    pb = PromptBuilder(registry)
    cmd = registry.get_command("/plan-launch")
    lean = pb.for_command(cmd, "x", step=4, lean=True)
    cumulative = pb.for_command(cmd, "x", step=4)
    assert [s.name for s in lean.skills] == ["gtm-strategy"] and len(cumulative.skills) == 3
    assert lean.metadata["skills_scope"] == "step-lean" and lean.approx_tokens < cumulative.approx_tokens


def test_almost_every_command_fits_a_small_backend_step_by_step(registry):
    """Run step by step with lean loading, every upstream workflow fits an 8K-request backend
    except the two steps that apply four frameworks at once (documented; `doctor` lists them)."""
    pb = PromptBuilder(registry)
    too_big = {}
    for plugin in registry.plugins.values():
        for cmd in plugin.commands.values():
            wf = parse_workflow(cmd)
            for i in range(1, len(wf.steps_for(None)) + 1):
                n = pb.for_command(cmd, "a realistic product description here", step=i, lean=True).approx_tokens
                if n > 6000:
                    too_big[cmd.qualified_name] = max(n, too_big.get(cmd.qualified_name, 0))
    assert set(too_big) == {"pm-product-strategy:business-model", "pm-product-strategy:market-scan"}, too_big
    # …and business-model only when asked for *all* four canvases; a single canvas is small
    assert pb.for_command(registry.get_command("/business-model"), "lean X", step=2, lean=True).approx_tokens < 4000


def test_resolve_reports_scoped_skills(engine):
    r = engine.resolve("/sprint retro rough sprint")
    assert r["skills"] == ["pm-execution:retro"] and r["steps"] == 3
    assert len(engine.resolve("/sprint")["skills"]) == 3


# ─── context budget ──────────────────────────────────────────────────────────


def test_estimate_tokens_and_attachment_truncation():
    assert estimate_tokens("") == 0 and estimate_tokens("abc" * 100) == 86  # ≈3.5 chars/token, rounded up
    a = Attachment("notes.md", "x" * 1000, "md")
    assert a.truncated(2000) is a
    t = a.truncated(100)
    assert t.name == "notes.md" and t.text.startswith("x" * 100) and "truncated 900 characters" in t.text


def test_budget_defaults_and_env(registry, monkeypatch):
    e = Engine(registry, OfflineProvider())
    assert e.max_input_tokens == 0 and e.max_output_tokens == 4096 and e.budget == {"max_input_tokens": None, "max_output_tokens": 4096}
    monkeypatch.setenv("PM_ENGINE_MAX_INPUT_TOKENS", "6000")
    monkeypatch.setenv("PM_ENGINE_MAX_OUTPUT_TOKENS", "1024")
    e = Engine(registry, OfflineProvider())
    assert e.max_input_tokens == 6000 and e.max_output_tokens == 1024
    assert Engine(registry, OfflineProvider(), max_input_tokens=100, max_output_tokens=50).budget == {"max_input_tokens": 100, "max_output_tokens": 50}
    monkeypatch.setenv("PM_ENGINE_MAX_INPUT_TOKENS", "not-a-number")
    assert Engine(registry, OfflineProvider()).max_input_tokens == 0


class _Recording(OfflineProvider):
    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []

    def complete(self, system, messages, *, max_tokens=4096, temperature=0.4):
        self.calls.append({"system": system, "messages": messages, "max_tokens": max_tokens})
        return super().complete(system, messages, max_tokens=max_tokens, temperature=temperature)


def test_budget_drops_oldest_history_first(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_ENGINE_HOME", str(tmp_path / "home"))
    prov = _Recording()
    eng = Engine(registry, prov, max_input_tokens=3000, max_output_tokens=777)
    s = Session.new()
    for i in range(6):  # 12 turns of ~250 tokens each
        eng.run(f"/lean-canvas product {i} " + ("lorem ipsum " * 80), session=s)
    r = eng.run("/lean-canvas final product", session=s)
    b = r.prompt.metadata["budget"]
    assert b["max_input_tokens"] == 3000 and b["history_turns_dropped"] > 0 and b["over_by"] == 0 and b["attachments_truncated"] == []
    sent = prov.calls[-1]["messages"]
    assert sent[0]["role"] == "user" and sent[-1]["content"] == r.prompt.user  # alternation preserved, newest turn last
    assert len(sent) < len(s.messages()) + 1
    assert estimate_tokens(r.prompt.system) + sum(estimate_tokens(m["content"]) for m in sent) <= 3000
    assert prov.calls[-1]["max_tokens"] == 777  # engine default reply cap
    assert len(s.turns) == 14  # the session itself keeps everything; only the request is trimmed


def test_budget_truncates_attachments_with_marker(registry, tmp_path):
    prov = _Recording()
    eng = Engine(registry, prov, max_input_tokens=2500)
    big = Attachment("transcript.txt", "Interviewer: tell me more.\n" * 800)  # ≈5.4K tokens on its own
    logs: list[str] = []
    r = eng.run("/lean-canvas a note-taking app", attachments=[big], progress=logs.append)
    b = r.prompt.metadata["budget"]
    assert b["attachments_truncated"] == ["transcript.txt"] and b["over_by"] == 0
    assert "truncated" in r.prompt.user and "to fit the model's context budget" in r.prompt.user
    assert r.prompt.approx_tokens <= 2500
    assert any("truncated attachment" in m for m in logs)


def test_budget_warns_when_system_prompt_alone_is_too_big(registry):
    eng = Engine(registry, OfflineProvider(), max_input_tokens=2000)
    logs: list[str] = []
    r = eng.run("/market-scan EV charging", progress=logs.append)  # 4 skills ≈ 7.8K tokens
    b = r.prompt.metadata["budget"]
    assert b["over_by"] > 0 and any("exceeds PM_ENGINE_MAX_INPUT_TOKENS" in m for m in logs)
    # …but step 1 of the same command fits comfortably
    r1 = eng.run("/market-scan EV charging", step=1)
    assert r1.prompt.metadata["budget"]["over_by"] == 0


def test_no_budget_means_untouched_request(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_ENGINE_HOME", str(tmp_path / "home"))
    prov = _Recording()
    eng = Engine(registry, prov)
    s = Session.new()
    for i in range(3):
        eng.run(f"/lean-canvas product {i}", session=s)
    r = eng.run("/lean-canvas final", session=s)
    assert "budget" not in r.prompt.metadata and len(prov.calls[-1]["messages"]) == 7
    assert prov.calls[-1]["max_tokens"] == 4096
    eng.run("/lean-canvas again", session=s, max_tokens=123)
    assert prov.calls[-1]["max_tokens"] == 123


def test_budget_report_and_doctor(registry, capsys, marketplace_path, monkeypatch):
    rep = Engine(registry, OfflineProvider(), max_input_tokens=6000).budget_report()
    assert rep["commands"] == 42 and rep["largest"]["command"] == "pm-product-strategy:business-model"
    assert set(rep["over_full"]) == {"pm-product-strategy:business-model", "pm-product-strategy:market-scan", "pm-product-discovery:discover", "pm-go-to-market:plan-launch"}
    assert rep["over_step"] == ["pm-product-strategy:business-model", "pm-product-strategy:market-scan"]
    assert Engine(registry, OfflineProvider()).budget_report()["over_full"] == []
    monkeypatch.setenv("PM_ENGINE_MAX_INPUT_TOKENS", "6000")
    assert main(["-m", str(marketplace_path), "--no-extra", "doctor"]) == 0
    out = capsys.readouterr().out
    assert "context budget: ≈6000 input" in out and "4 of 42 commands exceed it when run in one go" in out and "2 exceed it even step by step" in out
    monkeypatch.delenv("PM_ENGINE_MAX_INPUT_TOKENS")
    assert main(["-m", str(marketplace_path), "--no-extra", "doctor"]) == 0
    assert "context budget: none" in capsys.readouterr().out


def test_server_reports_budget(client):
    body = client.get("/api/providers").get_json()
    assert body["budget"] == {"max_input_tokens": None, "max_output_tokens": 4096}
    r = client.post("/api/run", json={"text": "/write-prd X", "step": 1, "save_session": False, "include_prompt": True}).get_json()
    assert r["prompt"]["metadata"]["skills_scope"] == "step" and r["skills"] == []
    r = client.post("/api/run", json={"text": "/write-prd X", "save_session": False, "include_prompt": True}).get_json()
    assert r["prompt"]["metadata"]["skills_scope"] == "all" and r["skills"] == ["pm-execution:create-prd"]


# ─── end to end against a size-capped gateway (Groq-style 413) ───────────────


def test_budget_survives_a_size_capped_gateway(registry, tmp_path, monkeypatch):
    import socket
    import sys
    import threading
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mock_gateway
    from pm_engine.providers import OpenAIProvider, ProviderError

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    srv = mock_gateway.serve(port, fmt="openai", max_request_tokens=8000)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("PM_ENGINE_HOME", str(tmp_path / "home"))
        prov = OpenAIProvider(api_key=mock_gateway.DEFAULT_KEY, base_url=f"http://127.0.0.1:{port}/v1", model="openai/gpt-oss-120b")
        big = Attachment("research.md", "Customer said: pricing is confusing.\n" * 900)  # ≈ 8.5K real tokens
        # no budget → the gateway rejects the request with a 413 and the error carries the fix
        with pytest.raises(ProviderError) as ei:
            Engine(registry, prov).run("/write-prd billing revamp", attachments=[big], step=3)
        assert ei.value.status == 413 and "PM_ENGINE_MAX_INPUT_TOKENS" in str(ei.value)
        srv.handler_cls.seen.clear()  # type: ignore[attr-defined]
        # budget → same call succeeds: attachment trimmed, reply cap lowered
        eng = Engine(registry, prov, max_input_tokens=5000, max_output_tokens=2500)
        r = eng.run("/write-prd billing revamp", attachments=[big], step=3)
        assert r.completion.provider == "openai" and "mock openai/gpt-oss-120b" in r.text
        assert r.prompt.metadata["budget"]["attachments_truncated"] == ["research.md"]
        # a whole session of a multi-step workflow also stays under the cap
        s = Session.new()
        results = eng.run_all_steps("/discover AI meeting summarizer", session=s)
        assert len(results) == 7 and all("mock" in x.text for x in results)
        seen = srv.handler_cls.seen  # type: ignore[attr-defined]
        assert all(len("".join(m["content"] for m in req["payload"]["messages"])) // 4 + req["payload"]["max_tokens"] <= 8000 for req in seen)
    finally:
        srv.shutdown()
        srv.server_close()
