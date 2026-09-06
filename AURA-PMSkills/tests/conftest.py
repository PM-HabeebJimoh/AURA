import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The suite must be hermetic: never pick up a developer's real keys or env files.
for _k in list(os.environ):
    if _k.startswith(("ARENA_", "ANTHROPIC_", "OPENAI_", "OLLAMA_")) or _k in ("PM_ENGINE_PROVIDER", "PM_ENGINE_ENV_FILE"):
        del os.environ[_k]
os.environ["PM_ENGINE_PROVIDER"] = "offline"
os.environ["PM_ENGINE_ENV_FILE"] = os.devnull  # explicit file slot → nothing


@pytest.fixture(scope="session")
def marketplace_path() -> Path:
    return ROOT / "pm-skills"


@pytest.fixture(scope="session")
def aura_skills_path() -> Path:
    return ROOT / "aura-skills"


@pytest.fixture(scope="session")
def registry(marketplace_path):
    """The vendored upstream marketplace only (pinned upstream counts: 9 / 68 / 42)."""
    from pm_engine.registry import Registry

    return Registry(marketplace_path, extra_roots=())


@pytest.fixture(scope="session")
def merged_registry(marketplace_path, aura_skills_path):
    """Upstream + AURA's own ``aura-skills`` marketplace, as the CLI/server see it by default."""
    from pm_engine.registry import Registry

    return Registry(marketplace_path, extra_roots=[aura_skills_path])


@pytest.fixture(scope="session")
def index(registry):
    from pm_engine.search import SkillIndex

    return SkillIndex(registry)


@pytest.fixture()
def engine(registry, tmp_path, monkeypatch):
    from pm_engine.providers import OfflineProvider
    from pm_engine.runner import Engine

    monkeypatch.setenv("PM_ENGINE_HOME", str(tmp_path / "home"))
    return Engine(registry, OfflineProvider(), artifacts_dir=tmp_path / "artifacts")


@pytest.fixture()
def client(engine):
    from pm_engine.server import create_app

    app = create_app(engine)
    app.config["TESTING"] = True
    return app.test_client()
