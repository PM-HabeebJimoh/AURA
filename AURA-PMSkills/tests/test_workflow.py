import pytest

from pm_engine.workflow import parse_workflow, resolve_mode, skill_graph, unresolved_references


def test_every_command_parses_into_steps(registry):
    total = 0
    for c in registry.commands:
        wf = parse_workflow(c)
        n = len(wf.steps) + sum(len(v) for v in wf.modes.values())
        assert n > 0, c.qualified_name
        assert wf.title
        assert wf.invocation_examples, c.qualified_name
        total += n
    assert total >= 190  # 201 at upstream v2.1.0


def test_no_unresolved_skill_references(registry):
    assert unresolved_references(registry) == []


def test_discover_workflow_structure(registry):
    wf = parse_workflow(registry.get_command("/discover"))
    assert [s.number for s in wf.steps] == ["1", "2", "3", "4", "5", "6", "7"]
    assert wf.steps[1].skills == ["brainstorm-ideas-existing", "brainstorm-ideas-new"]
    assert wf.steps[1].checkpoints and "10 ideas" in wf.steps[1].checkpoints[0]
    assert wf.steps[3].skills == ["prioritize-assumptions"]
    assert wf.steps[-1].is_next_steps
    assert len(wf.offers) == 4
    assert wf.output_template.startswith("## Discovery Plan")
    assert set(wf.skills) == {
        "brainstorm-ideas-existing", "brainstorm-ideas-new", "identify-assumptions-existing", "identify-assumptions-new",
        "prioritize-assumptions", "brainstorm-experiments-existing", "brainstorm-experiments-new",
    }
    assert wf.notes and any("15-30 minute" in n for n in wf.notes)


def test_ship_check_parallel_step(registry):
    wf = parse_workflow(registry.get_command("/ship-check"))
    par = [s for s in wf.steps if s.parallel]
    assert par and par[0].number == "3+4"
    assert wf.output_template.startswith("## Shipping Packet")


def test_numbered_h3_steps_style(registry):
    wf = parse_workflow(registry.get_command("/security-audit-static"))
    assert len(wf.steps) == 5
    assert wf.steps[0].title.startswith("Map entry points")
    assert wf.command.allowed_tools.startswith("Read")


def test_mode_commands(registry):
    wf = parse_workflow(registry.get_command("/sprint"))
    assert set(wf.modes) == {"plan", "retro", "release-notes"}
    assert [s.title for s in wf.modes["plan"]][0] == "Gather Sprint Context"
    assert wf.modes["plan"][1].skills == ["sprint-plan"]
    assert wf.modes["retro"][1].skills == ["retro"]
    assert wf.modes["release-notes"][1].skills == ["release-notes"]
    assert wf.steps == []  # no shared steps outside modes
    assert len(wf.steps_for("retro")) == 3


def test_interview_mode_offers(registry):
    wf = parse_workflow(registry.get_command("/interview"))
    assert set(wf.modes) == {"prep", "summarize"}
    assert wf.offers_for("summarize")
    assert wf.offers_for("prep") == []


def test_business_model_template_only_modes_fall_back_to_shared_workflow(registry):
    wf = parse_workflow(registry.get_command("/business-model"))
    assert wf.match_mode("lean") == "lean-canvas"
    assert wf.match_mode("full") == "full-business-model-canvas"
    assert wf.match_mode("value-prop") == "value-proposition"
    assert wf.match_mode("startup") == "startup-canvas"
    assert wf.match_mode("all") == "all"
    assert len(wf.steps_for("lean")) == 3  # shared "Workflow (All Modes)"
    assert wf.mode_template("lean").startswith("## Lean Canvas")


@pytest.mark.parametrize(
    "cmd,args,mode,rest",
    [
        ("/brainstorm", "ideas existing Mobile banking", "ideas/existing", "Mobile banking"),
        ("/brainstorm", "experiments Marketplace", "experiments", "Marketplace"),
        ("/brainstorm", "Mobile banking", None, "Mobile banking"),
        ("/sprint", "retro paste feedback", "retro", "paste feedback"),
        ("/business-model", "all SaaS tool", "all", "SaaS tool"),
        ("/write-prd", "plan SSO", None, "plan SSO"),
        ("/write-stories", "job Checkout redesign", "job", "Checkout redesign"),
    ],
)
def test_resolve_mode(registry, cmd, args, mode, rest):
    wf = parse_workflow(registry.get_command(cmd))
    assert resolve_mode(wf, args) == (mode, rest)


def test_offers_are_questions_only(registry):
    # transform-roadmap has quoted example lines that are NOT offers
    wf = parse_workflow(registry.get_command("/transform-roadmap"))
    assert all(o.endswith("?") for o in wf.offers)
    assert len(wf.offers) == 3


def test_context_questions_extracted(registry):
    wf = parse_workflow(registry.get_command("/write-prd"))
    gather = wf.steps[1]
    assert gather.is_context_gathering
    assert any("problem" in q.lower() for q in gather.questions)


def test_skill_graph(registry):
    g = skill_graph(registry)
    ids = {n["id"] for n in g["nodes"]}
    assert len(ids) == 68 + 42
    for e in g["edges"]:
        assert e["from"] in ids and e["to"] in ids
        assert e["from"].split(":")[1] == e["to"].split(":")[1]  # never cross-plugin
    assert len(g["edges"]) >= 60


def test_workflow_to_dict_json(registry):
    import json

    for c in registry.commands:
        json.dumps(parse_workflow(c).to_dict())
