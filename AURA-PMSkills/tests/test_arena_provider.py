"""Arena.ai provider, .env configuration, `pm-engine setup/providers --check`, and the mock gateway."""

import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `mock_gateway`
import mock_gateway  # noqa: E402

from pm_engine import config, providers  # noqa: E402
from pm_engine.cli import main  # noqa: E402
from pm_engine.providers import ArenaProvider, OfflineProvider, ProviderError, auto_provider, available_providers, make_provider  # noqa: E402
from pm_engine.runner import Engine  # noqa: E402

KEY = mock_gateway.DEFAULT_KEY


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def gateway(request):
    """Start a mock gateway; parametrize indirectly with the wire format ('openai' | 'anthropic' | 'both')."""
    fmt = getattr(request, "param", "both")
    port = _free_port()
    srv = mock_gateway.serve(port, fmt=fmt)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.base_url = f"http://127.0.0.1:{port}/v1"  # type: ignore[attr-defined]
    srv.fmt = fmt  # type: ignore[attr-defined]
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith(("ARENA_", "ANTHROPIC_", "OPENAI_", "OLLAMA_")) or k in ("PM_ENGINE_PROVIDER", "PM_ENGINE_ENV_FILE"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PM_ENGINE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)  # so ./.env lookups don't pick up a developer's real file
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    config._loaded.clear()
    before = dict(os.environ)
    yield
    config._loaded.clear()
    # save_env_values()/load_env_files() export into os.environ on purpose; make sure nothing leaks between tests
    for k in list(os.environ):
        if k.startswith(("ARENA_", "ANTHROPIC_", "OPENAI_", "OLLAMA_", "PM_ENGINE_")) and k not in before:
            del os.environ[k]


# ─── construction / selection ────────────────────────────────────────────────


def test_arena_requires_key():
    with pytest.raises(ProviderError):
        ArenaProvider()


def test_arena_defaults_and_env(monkeypatch):
    monkeypatch.setenv("ARENA_API_KEY", "k")
    a = ArenaProvider()
    assert a.base_url == "https://api.arena.ai/v1" and a.model == "claude-sonnet-4-5" and a.api_format == "auto" and a.formats == ["openai", "anthropic"]
    monkeypatch.setenv("ARENA_BASE_URL", "https://gw.example.com/v1/")
    monkeypatch.setenv("ARENA_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("ARENA_API_FORMAT", "anthropic")
    monkeypatch.setenv("ARENA_AUTH_HEADER", "x-api-key")
    b = ArenaProvider()
    assert b.base_url == "https://gw.example.com/v1" and b.model == "gpt-4o-mini" and b.formats == ["anthropic"]
    assert b._headers() == {"x-api-key": "k"}
    assert b._backend("anthropic").base_url == "https://gw.example.com"  # /v1 stripped: AnthropicProvider adds /v1/messages
    monkeypatch.setenv("ARENA_API_FORMAT", "weird")
    with pytest.raises(ProviderError):
        ArenaProvider()


def test_auto_provider_prefers_arena(monkeypatch):
    monkeypatch.setenv("ARENA_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k2")
    assert isinstance(auto_provider(), ArenaProvider)
    assert available_providers()["arena"] is True
    monkeypatch.setenv("PM_ENGINE_PROVIDER", "offline")
    assert isinstance(auto_provider(), OfflineProvider)
    assert isinstance(make_provider("arena", model="m"), ArenaProvider) and make_provider("arena").model == "claude-sonnet-4-5"


# ─── .env files ──────────────────────────────────────────────────────────────


def test_env_file_parsing_and_precedence(monkeypatch, tmp_path):
    text = '# comment\nexport ARENA_API_KEY="abc def"\nARENA_MODEL=gpt-4o-mini # trailing comment\nPATH=/evil\nOPENAI_API_KEY=\'q\'\nbroken line\n'
    parsed = config.parse_env_text(text)
    assert parsed == {"ARENA_API_KEY": "abc def", "ARENA_MODEL": "gpt-4o-mini", "OPENAI_API_KEY": "q"}  # PATH filtered out
    (tmp_path / ".env").write_text("ARENA_API_KEY=from-project\nARENA_MODEL=project-model\n")
    user_file = config.user_env_file()
    user_file.parent.mkdir(parents=True)
    user_file.write_text("ARENA_API_KEY=from-user\nARENA_BASE_URL=https://u.example/v1\n")
    monkeypatch.setenv("ARENA_MODEL", "from-shell")
    read = config.load_env_files()
    assert {p.name for p in read} == {".env", "pm-engine.env"}
    assert os.environ["ARENA_API_KEY"] == "from-project"  # project .env beats user file
    assert os.environ["ARENA_MODEL"] == "from-shell"  # shell beats files
    assert os.environ["ARENA_BASE_URL"] == "https://u.example/v1"  # user file fills the gaps
    assert config.load_env_files() == []  # loaded once


def test_save_env_values_merges_and_protects(tmp_path):
    f = tmp_path / "x.env"
    f.write_text("# keep me\nARENA_MODEL=old\nOTHER=1\n")
    config.save_env_values({"ARENA_API_KEY": "s3cret key", "ARENA_MODEL": "new"}, f)
    body = f.read_text()
    assert body.startswith("# keep me\n") and "ARENA_MODEL=new" in body and 'ARENA_API_KEY="s3cret key"' in body and "OTHER=1" in body
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert config.redact("s3cret key1234") == "s3cr…1234 (14 chars)" and config.redact(None) == "(not set)"


# ─── live protocol against the mock gateway ──────────────────────────────────


@pytest.mark.parametrize("gateway", ["openai", "anthropic", "both"], indirect=True)
def test_arena_complete_and_stream_all_formats(gateway, monkeypatch):
    monkeypatch.setenv("ARENA_API_KEY", KEY)
    monkeypatch.setenv("ARENA_BASE_URL", gateway.base_url)
    a = ArenaProvider(model="gpt-4o-mini")
    c = a.complete("You are AURA PM Engine.\nmore", [{"role": "user", "content": "hello arena"}], max_tokens=32)
    assert c.provider == "arena" and c.model == "gpt-4o-mini"
    assert "system: You are AURA PM Engine." in c.text and "user: hello arena" in c.text
    expected = "openai" if gateway.fmt in ("openai", "both") else "anthropic"
    assert a._resolved == expected
    streamed = "".join(a.stream("You are AURA PM Engine.\nmore", [{"role": "user", "content": "hello arena"}], max_tokens=32))
    assert streamed == c.text
    if expected == "anthropic":
        sent = gateway.handler_cls.seen[-1]
        assert sent["path"] == "/v1/messages" and sent["headers"].get("anthropic-version") and sent["headers"].get("x-api-key") == KEY
    else:
        sent = gateway.handler_cls.seen[-1]
        assert sent["path"] == "/v1/chat/completions" and sent["headers"].get("authorization") == f"Bearer {KEY}"
        assert sent["payload"]["messages"][0]["role"] == "system"


@pytest.mark.parametrize("gateway", ["anthropic"], indirect=True)
def test_arena_stream_autodetects_without_models_endpoint(gateway, monkeypatch):
    monkeypatch.setenv("ARENA_API_KEY", KEY)
    monkeypatch.setenv("ARENA_BASE_URL", gateway.base_url)
    a = ArenaProvider(model="m1")
    assert a.list_models() == [] and a._resolved is None  # anthropic-only gateway has no /models
    text = "".join(a.stream("sys", [{"role": "user", "content": "stream me"}]))
    assert "user: stream me" in text and a._resolved == "anthropic"


def test_arena_auth_failure_is_not_retried_or_misdetected(gateway, monkeypatch):
    monkeypatch.setenv("ARENA_API_KEY", "wrong-key")
    monkeypatch.setenv("ARENA_BASE_URL", gateway.base_url)
    a = ArenaProvider()
    with pytest.raises(ProviderError) as ei:
        a.complete("s", [{"role": "user", "content": "u"}])
    assert ei.value.status == 401 and not ei.value.retryable and a._resolved is None
    assert len(gateway.handler_cls.seen) == 1  # no format fallback attempted on 401
    info = a.check()
    assert info["ok"] is False and info["status"] == 401


def test_arena_retries_transient_errors(monkeypatch):
    port = _free_port()
    srv = mock_gateway.serve(port, fmt="openai", fail_first=2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("ARENA_API_KEY", KEY)
        monkeypatch.setenv("ARENA_BASE_URL", f"http://127.0.0.1:{port}/v1")
        monkeypatch.setenv("ARENA_API_FORMAT", "openai")
        c = ArenaProvider(model="gpt-4o-mini").complete("s", [{"role": "user", "content": "u"}])
        assert "mock gpt-4o-mini" in c.text
        assert len(srv.handler_cls.seen) == 3  # 2 × 529 then success
        with providers.no_retries():
            srv.handler_cls.fail_first = 1
            with pytest.raises(ProviderError) as ei:
                ArenaProvider(model="gpt-4o-mini").complete("s", [{"role": "user", "content": "u"}])
            assert ei.value.status == 529 and ei.value.retryable
    finally:
        srv.shutdown()
        srv.server_close()


def test_arena_check_and_models(gateway, monkeypatch):
    monkeypatch.setenv("ARENA_API_KEY", KEY)
    monkeypatch.setenv("ARENA_BASE_URL", gateway.base_url)
    a = ArenaProvider(model="not-a-real-model")
    assert a.list_models() == sorted(mock_gateway.MODELS)
    info = a.check()
    assert info["ok"] and info["format"] == "openai" and info["models"] == 4 and "not in the endpoint's model list" in info["warning"]
    assert "reply" in info and info["latency_s"] >= 0


# ─── through the engine, API and CLI ─────────────────────────────────────────


def test_engine_runs_upstream_command_through_arena(gateway, monkeypatch, registry, tmp_path):
    monkeypatch.setenv("ARENA_API_KEY", KEY)
    monkeypatch.setenv("ARENA_BASE_URL", gateway.base_url)
    eng = Engine(registry, auto_provider(), artifacts_dir=tmp_path / "art")
    assert eng.provider.name == "arena"
    r = eng.run("/write-prd SSO for enterprise customers")
    assert r.completion.provider == "arena" and "system: You are AURA PM Engine" in r.text and "user: Run /write-prd" in r.text
    assert r.artifact and Path(r.artifact).is_file()
    sent = gateway.handler_cls.seen[-1]["payload"]
    assert "<skill name=\"pm-execution:create-prd\">" in sent["messages"][0]["content"]  # the real skill body went over the wire
    items = list(eng.stream("/lean-canvas Marketplace", save_artifact=False))
    assert isinstance(items[-1].text, str) and "".join(i for i in items[:-1] if isinstance(i, str)) == items[-1].text


def test_api_providers_check_never_leaks_key(gateway, monkeypatch, registry, tmp_path):
    from pm_engine.server import create_app

    monkeypatch.setenv("ARENA_API_KEY", KEY)
    monkeypatch.setenv("ARENA_BASE_URL", gateway.base_url)
    app = create_app(Engine(registry, auto_provider(), artifacts_dir=tmp_path / "art"))
    app.config["TESTING"] = True
    c = app.test_client()
    body = c.get("/api/providers?check=1").get_json()
    assert body["active"]["provider"] == "arena" and body["active"]["base_url"] == gateway.base_url
    assert body["check"]["ok"] is True and body["available"]["arena"] is True
    assert KEY not in json.dumps(body)
    h = c.get("/api/health").get_json()
    assert h["provider"] == "arena"
    run = c.post("/api/run", json={"text": "/write-prd SSO"}).get_json()
    assert run["completion"]["provider"] == "arena" and "mock" in run["text"]


def test_cli_setup_providers_check_and_run(gateway, monkeypatch, capsys, marketplace_path, tmp_path):
    # `pm-engine setup arena --key … --base-url … --check` writes the user env file and verifies the connection
    rc = main(["setup", "arena", "--key", KEY, "--base-url", gateway.base_url, "--model", "gpt-4o-mini", "--check"])
    out = capsys.readouterr().out
    assert rc == 0, out
    env_file = config.user_env_file()
    assert env_file.is_file() and oct(env_file.stat().st_mode & 0o777) == "0o600"
    saved = config.parse_env_text(env_file.read_text())
    assert saved["ARENA_API_KEY"] == KEY and saved["ARENA_BASE_URL"] == gateway.base_url and saved["PM_ENGINE_PROVIDER"] == "arena"
    assert KEY not in out and "reachable" in out and "saved" in out

    # a *fresh* process state (env cleared) must pick the key up from the file
    for k in ("ARENA_API_KEY", "ARENA_BASE_URL", "ARENA_MODEL", "PM_ENGINE_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    config._loaded.clear()
    assert main(["providers", "--check", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["available"]["arena"] and body["checks"]["arena"]["ok"] and body["checks"]["arena"]["model"] == "gpt-4o-mini"
    assert str(env_file) in body["env_files"]

    assert main(["-m", str(marketplace_path), "--no-extra", "run", "/write-prd", "SSO", "--out", str(tmp_path / "art"), "--json", "-q"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["completion"]["provider"] == "arena" and r["completion"]["model"] == "gpt-4o-mini" and "mock" in r["text"]

    assert main(["-m", str(marketplace_path), "--no-extra", "run", "/north-star-metric", "marketplace", "--stream", "-q"]) == 0
    assert "mock gpt-4o-mini" in capsys.readouterr().out

    assert main(["-m", str(marketplace_path), "doctor", "--check"]) == 0
    out = capsys.readouterr().out
    assert "active provider: arena/gpt-4o-mini" in out and "reachable" in out and KEY not in out


def test_cli_setup_without_key_non_tty_fails_cleanly(capsys):
    assert main(["setup", "arena"]) == 2
    assert "--key" in capsys.readouterr().err


def test_providers_check_reports_unreachable_endpoint(monkeypatch, capsys):
    port = _free_port()  # nothing listening
    monkeypatch.setenv("ARENA_API_KEY", KEY)
    monkeypatch.setenv("ARENA_BASE_URL", f"http://127.0.0.1:{port}/v1")
    assert main(["providers", "--check"]) == 1
    out = capsys.readouterr().out
    assert "✗ arena" in out and "network error" in out
