import json

import pytest

from pm_engine.registry import Registry, RegistryError

EXPECTED = {
    "pm-product-discovery": (13, 5),
    "pm-product-strategy": (12, 5),
    "pm-execution": (16, 11),
    "pm-market-research": (7, 3),
    "pm-data-analytics": (3, 3),
    "pm-go-to-market": (6, 3),
    "pm-marketing-growth": (5, 2),
    "pm-toolkit": (4, 5),
    "pm-ai-shipping": (2, 5),
}


def test_loads_whole_marketplace(registry):
    st = registry.stats()
    assert st["plugins"] == 9
    assert st["skills"] == 68
    assert st["commands"] == 42
    assert registry.marketplace.name == "pm-skills"
    assert registry.marketplace.version == "2.1.0"


@pytest.mark.parametrize("plugin,counts", EXPECTED.items())
def test_per_plugin_counts_match_upstream_readme(registry, plugin, counts):
    p = registry.get_plugin(plugin)
    assert (len(p.skills), len(p.commands)) == counts
    assert p.version == "2.1.0"
    assert p.author["name"]


def test_skill_names_match_directories(registry):
    for s in registry.skills:
        assert s.path.parent.name == s.name
        assert s.description, s.qualified_name
        assert "use when" in s.description.lower()


def test_commands_have_hint_and_description(registry):
    for c in registry.commands:
        assert c.description, c.qualified_name
        assert c.argument_hint, c.qualified_name


def test_lookup_forms(registry):
    assert registry.get_command("/write-prd").plugin == "pm-execution"
    assert registry.get_command("write-prd").name == "write-prd"
    assert registry.get_command("pm-execution:write-prd").name == "write-prd"
    assert registry.get_command("/pm-execution:write-prd").name == "write-prd"
    assert registry.get_skill("lean-canvas").plugin == "pm-product-strategy"
    assert registry.get_skill("/pm-product-strategy:lean-canvas").name == "lean-canvas"


def test_unknown_refs_raise(registry):
    with pytest.raises(RegistryError):
        registry.get_command("/nope")
    with pytest.raises(RegistryError):
        registry.get_skill("pm-toolkit:nope")
    with pytest.raises(RegistryError):
        registry.get_plugin("pm-missing")


def test_same_name_skill_and_command_resolve_separately(registry):
    # upstream has both a `pre-mortem` skill and a `/pre-mortem` command
    assert registry.get_skill("pre-mortem").path.name == "SKILL.md"
    assert registry.get_command("pre-mortem").path.name == "pre-mortem.md"


def test_command_referenced_skills_resolve_in_same_plugin(registry):
    for c in registry.commands:
        plugin = registry.plugins[c.plugin]
        for name in c.referenced_skills:
            assert name in plugin.skills, f"{c.qualified_name} → {name}"


def test_modes_parsed_from_argument_hint(registry):
    assert registry.get_command("/sprint").modes == ["plan", "retro", "release-notes"]
    assert registry.get_command("/brainstorm").modes == ["ideas", "experiments"]
    assert registry.get_command("/write-prd").modes == []


def test_further_reading_links(registry):
    prd = registry.get_skill("create-prd")
    titles = [t for t, _ in prd.further_reading]
    assert any("PRD" in t for t in titles)
    for _, url in prd.further_reading:
        assert url.startswith("https://")


def test_registry_on_single_plugin_dir(marketplace_path):
    reg = Registry(marketplace_path / "pm-toolkit")
    assert set(reg.plugins) == {"pm-toolkit"}
    assert reg.marketplace is None


def test_to_dict_is_json_serialisable(registry):
    for p in registry.plugins.values():
        json.dumps(p.to_dict())
    json.dumps([s.to_dict(include_body=True) for s in registry.skills])
    json.dumps([c.to_dict(include_body=True) for c in registry.commands])


def test_env_override_path(monkeypatch, marketplace_path):
    monkeypatch.setenv("PM_SKILLS_PATH", str(marketplace_path))
    assert Registry().root == marketplace_path.resolve()
