import json

import pytest

from pm_engine.prompt import Attachment
from pm_engine.registry import RegistryError
from pm_engine.session import Session


def test_resolve_routes(engine):
    r = engine.resolve("/write-prd SSO")
    assert r["kind"] == "command" and r["command"] == "pm-execution:write-prd" and r["steps"] == 4
    r = engine.resolve("/brainstorm ideas existing Mobile banking")
    assert r["mode"] == "ideas/existing" and r["arguments"] == "Mobile banking"
    r = engine.resolve("/lean-canvas Marketplace")
    assert r["kind"] == "skill" and r["skill"] == "pm-product-strategy:lean-canvas"
    r = engine.resolve("/writeprd SSO")
    assert r["kind"] == "unknown" and r["did_you_mean"][0]["name"] == "write-prd"
    r = engine.resolve("What's a good North Star Metric for a marketplace?")
    assert r["kind"] == "free-text" and r["auto_skills"][0] == "pm-marketing-growth:north-star-metric"


def test_run_command_offline_writes_artifact(engine):
    r = engine.run("/write-prd SSO support for enterprise customers")
    assert r.command.name == "write-prd"
    assert r.skills[0].name == "create-prd"
    assert "Product Requirements Document: SSO support" in r.text
    assert r.artifact and r.artifact.is_file() and r.artifact.name.startswith("write-prd-sso-support")
    assert len(r.offers) == 3
    assert r.total_steps == 4 and r.step is None
    json.dumps(r.to_dict(include_prompt=True))


def test_run_force_skill(engine):
    r = engine.run("/pm-product-strategy:lean-canvas Marketplace for PMs")
    assert r.command is None and [s.name for s in r.skills] == ["lean-canvas"]
    assert not r.auto_loaded


def test_run_auto_load(engine):
    r = engine.run("Design a growth loop for a B2B SaaS with a freemium tier")
    assert r.auto_loaded and r.skills[0].name == "growth-loops"


def test_run_free_text_without_match(engine):
    r = engine.run("hello there")
    assert r.skills == [] and r.command is None and r.artifact is None
    assert r.text


def test_run_unknown_slash_suggests(engine):
    with pytest.raises(RegistryError) as ei:
        engine.run("/writeprd SSO")
    assert "write-prd" in str(ei.value)


def test_run_with_extra_skills_and_attachment(engine):
    r = engine.run("Summarize this", attachments=[Attachment("t.txt", "Q: ...\nA: ...")], extra_skills=["summarize-interview"])
    assert "summarize-interview" in [s.name for s in r.skills]
    assert '<attachment name="t.txt"' in r.prompt.user


def test_step_by_step_session_flow(engine):
    s = Session.new()
    r1 = engine.run("/discover AI meeting summarizer for remote teams", session=s, step=1)
    assert r1.step == 1 and r1.total_steps == 7 and r1.checkpoint is None
    assert s.active_command == "pm-product-discovery:discover" and s.current_step == 1
    # no slash + active command → continues at step 2
    r2 = engine.run("carry all forward", session=s)
    assert r2.step == 2 and r2.checkpoint and "10 ideas" in r2.checkpoint
    assert "User reply: carry all forward" in r2.prompt.system or "User reply: carry all forward" in r2.prompt.user
    # explicit jump to the last step completes the workflow
    r7 = engine.run("", session=s, step=7)
    assert r7.step == 7 and r7.offers and s.active_command is None
    assert len(s.turns) == 6
    loaded = Session.load(s.id)
    assert loaded.title.startswith("Run /discover") and len(loaded.artifacts) == 3
    assert loaded.messages()[0]["role"] == "user"


def test_run_all_steps_stops_at_checkpoint(engine):
    seen = []

    def on_step(r):
        seen.append(r.step)
        return r.checkpoint is None  # stop on first checkpoint (step 2)

    results = engine.run_all_steps("/discover Smart notifications", on_step=on_step)
    assert seen == [1, 2] and len(results) == 2
    assert results[-1].session.current_step == 2


def test_step_out_of_range(engine):
    with pytest.raises(ValueError):
        engine.run("/write-prd X", step=9)


def test_mode_command_run(engine):
    r = engine.run("/sprint retro The last sprint shipped late")
    assert r.mode == "retro" and r.total_steps == 3
    assert [s.name for s in r.skills] == ["sprint-plan", "retro", "release-notes"]
    assert 'mode="retro"' in r.prompt.system


def test_session_persistence_roundtrip(engine, tmp_path):
    s = Session.new("t")
    engine.run("/north-star Two-sided marketplace", session=s)
    ids = [x.id for x in Session.list()]
    assert s.id in ids
    assert Session.load(s.id).summary()["turns"] == 2
    s.delete()
    assert s.id not in [x.id for x in Session.list()]
