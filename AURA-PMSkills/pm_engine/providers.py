"""LLM providers.

All providers implement :class:`Provider.complete` with the same signature so
the engine can swap them. Only the standard library is required — Anthropic
and OpenAI-compatible backends are called over plain HTTPS with
``urllib`` so no SDK install is needed.

Selection (``pm_engine.providers.auto_provider``):

1. explicit ``PM_ENGINE_PROVIDER`` = ``arena`` | ``anthropic`` | ``openai`` | ``ollama`` | ``offline``
2. ``ARENA_API_KEY`` present      → Arena.ai (``ARENA_BASE_URL``, ``ARENA_MODEL``, ``ARENA_API_FORMAT``)
3. ``ANTHROPIC_API_KEY`` present  → Anthropic
4. ``OPENAI_API_KEY`` present     → OpenAI-compatible (``OPENAI_BASE_URL`` honoured)
5. ``OLLAMA_HOST`` present        → Ollama
6. otherwise                       → :class:`OfflineProvider` (deterministic, no network)

Keys may live in the environment or in ``.env`` / ``~/.aura/pm-engine.env``
(see :mod:`pm_engine.config`) — loaded once, never overriding real env vars.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator, Protocol

from .config import load_env_files

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
# statuses that mean "this endpoint does not speak the wire format we just used" (drives Arena auto-detection)
FORMAT_MISMATCH_STATUS = {400, 404, 405, 415, 422}
MAX_RETRIES = int(os.environ.get("PM_ENGINE_MAX_RETRIES", "3"))
_retry_override: contextvars.ContextVar[int | None] = contextvars.ContextVar("pm_engine_retries", default=None)

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_OLLAMA_MODEL = "llama3.1"
DEFAULT_ARENA_MODEL = "claude-sonnet-4-5"

# Free / OpenAI-compatible gateways that `pm-engine setup <name>` knows how to configure.
# Each is used through OpenAIProvider (Authorization: Bearer <key>, POST {base}/chat/completions).
# ``budget`` (optional) is written as PM_ENGINE_MAX_INPUT_TOKENS / PM_ENGINE_MAX_OUTPUT_TOKENS so the
# engine trims context for backends whose free tier caps the *whole request* (Groq: 8K tokens per minute).
PRESETS: dict[str, dict] = {
    "openrouter": {
        "label": "OpenRouter (free — 'openrouter/free' routes to whichever free models are up; ids ending in ':free' pin one)",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openrouter/free",
        "key_help": "openrouter.ai/settings/keys → Create key (sign up with email/GitHub/Google, no card; 20 req/min, 50 req/day on free models)",
        "env_key": "OPENROUTER_API_KEY",
    },
    "gemini": {
        "label": "Google AI Studio / Gemini (free tier, OpenAI-compatible endpoint, 1M-token context)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "key_help": "aistudio.google.com/apikey → Create API key (free tier; prompts may be used to improve Google products)",
        "env_key": "GEMINI_API_KEY",
    },
    "groq": {
        "label": "Groq (free tier — fast, but every request is capped at 8K tokens incl. the reply)",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "key_help": "console.groq.com/keys → Create API key (free: 30 req/min, 1K req/day, 8K tokens/min for gpt-oss)",
        "env_key": "GROQ_API_KEY",
        "budget": {"input": 5000, "output": 2500},  # Groq counts prompt + max_tokens against the 8K/min cap
    },
}

# Presets that no longer work, with the reason (kept so `pm-engine setup <name>` explains instead of failing obscurely).
RETIRED_PRESETS: dict[str, str] = {
    "github": "GitHub Models (models.github.ai) was retired on 2026-07-30 — the playground, catalog and inference API are gone for every account. "
    "Use `pm-engine setup openrouter --key <key>` (free, no card) or `pm-engine setup gemini --key <key>` instead.",
}


def _env(*names: str, default: str | None = None) -> str | None:
    """First non-empty environment variable among *names* (read at call time so ``.env`` files count)."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUS or self.status is None and "network error" in str(self)


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_s: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_s": round(self.latency_s, 3),
        }


class Provider(Protocol):
    name: str
    model: str

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Completion: ...

    def stream(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Iterator[str]: ...


class _StreamMixin:
    """Default ``stream`` for providers without native streaming: complete, then chunk."""

    def stream(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Iterator[str]:
        text = self.complete(system, messages, max_tokens=max_tokens, temperature=temperature).text  # type: ignore[attr-defined]
        yield from iter_chunks(text)


_TOO_BIG_MARKERS = ("request too large", "context length", "context_length", "maximum context", "too many tokens", "reduce your message size", "reduce the length", "prompt is too long", "input is too long")


def _size_hint(status: int, detail: str) -> str:
    """Append actionable advice when the backend rejected the request for being too big."""
    d = detail.lower()
    if status == 413 or (status in (400, 422, 429) and any(m in d for m in _TOO_BIG_MARKERS)):
        return (
            " — the request exceeds this backend's size limit. Set a context budget so the engine trims history/attachments and loads "
            "skills lazily (e.g. PM_ENGINE_MAX_INPUT_TOKENS=5000 PM_ENGINE_MAX_OUTPUT_TOKENS=2500, or `pm-engine setup <preset> --max-input-tokens 5000`), "
            "run big workflows step by step (--all-steps / --step N), or switch to a larger-context backend (`pm-engine setup gemini|openrouter`)."
        )
    return ""


def _request(url: str, payload: dict | None, headers: dict, timeout: float, method: str = "POST"):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = {"Content-Type": "application/json", **headers} if body is not None else dict(headers)
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 — https to configured API host
    except urllib.error.HTTPError as e:  # pragma: no cover - network
        detail = e.read().decode("utf-8", errors="replace")[:2000]
        raise ProviderError(f"HTTP {e.code} from {url}: {detail}{_size_hint(e.code, detail)}", status=e.code) from e
    except urllib.error.URLError as e:  # pragma: no cover - network
        raise ProviderError(f"network error calling {url}: {e.reason}") from e


def _with_retries(fn: Callable[[], object], *, retries: int | None = None, base_delay: float = 1.0):
    """Call *fn*, retrying transient provider errors with exponential backoff."""
    if retries is None:
        retries = _retry_override.get()
    if retries is None:
        retries = MAX_RETRIES
    attempt = 0
    while True:
        try:
            return fn()
        except ProviderError as e:
            attempt += 1
            if not e.retryable or attempt > retries:
                raise
            time.sleep(min(base_delay * (2 ** (attempt - 1)), 20.0))


def _post_json(url: str, payload: dict, headers: dict, timeout: float = 180.0) -> dict:
    def go() -> dict:
        with _request(url, payload, headers, timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return _with_retries(go)  # type: ignore[return-value]


def _get_json(url: str, headers: dict, timeout: float = 60.0) -> dict:
    def go() -> dict:
        with _request(url, None, headers, timeout, method="GET") as resp:
            return json.loads(resp.read().decode("utf-8"))

    return _with_retries(go)  # type: ignore[return-value]


class no_retries:
    """Context manager: make provider calls fail fast (used by connectivity checks)."""

    def __enter__(self):
        self._token = _retry_override.set(0)
        return self

    def __exit__(self, *exc):
        _retry_override.reset(self._token)
        return False


def _post_sse(url: str, payload: dict, headers: dict, timeout: float = 300.0) -> Iterator[dict]:
    """POST and yield decoded JSON events from an SSE / NDJSON response body."""
    resp = _with_retries(lambda: _request(url, payload, headers, timeout))
    with resp:  # type: ignore[union-attr]
        for raw in resp:  # type: ignore[union-attr]
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":") or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                return
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ─── Anthropic ───────────────────────────────────────────────────────────────


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None, base_url: str | None = None, extra_headers: dict | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        self.model = model or _env("PM_ENGINE_ANTHROPIC_MODEL", "ANTHROPIC_MODEL", default=DEFAULT_ANTHROPIC_MODEL)
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        self.extra_headers = dict(extra_headers or {})

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", **self.extra_headers}

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Completion:
        t0 = time.time()
        payload = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "system": system, "messages": messages}
        data = _post_json(f"{self.base_url}/v1/messages", payload, self._headers())
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage = data.get("usage", {})
        return Completion(text=text, provider=self.name, model=data.get("model", self.model), input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), latency_s=time.time() - t0, raw=data)

    def stream(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Iterator[str]:
        payload = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "system": system, "messages": messages, "stream": True}
        for ev in _post_sse(f"{self.base_url}/v1/messages", payload, {**self._headers(), "Accept": "text/event-stream"}):
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield delta["text"]
            elif ev.get("type") == "error":  # pragma: no cover - network
                raise ProviderError(f"anthropic stream error: {ev.get('error')}")


# ─── OpenAI-compatible (OpenAI, Azure-compatible gateways, LM Studio, vLLM…) ──


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None, base_url: str | None = None, extra_headers: dict | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        if not self.api_key and "api.openai.com" in self.base_url:
            raise ProviderError("OPENAI_API_KEY is not set")
        self.model = model or _env("PM_ENGINE_OPENAI_MODEL", "OPENAI_MODEL", default=DEFAULT_OPENAI_MODEL)
        self.extra_headers = dict(extra_headers or {})

    def _headers(self) -> dict:
        return {**({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}), **self.extra_headers}

    def list_models(self) -> list[str]:
        data = _get_json(f"{self.base_url}/models", self._headers())
        items = data.get("data") if isinstance(data, dict) else data
        return sorted(str(m.get("id") or m.get("name")) for m in (items or []) if isinstance(m, dict) and (m.get("id") or m.get("name")))

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Completion:
        t0 = time.time()
        payload = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "messages": [{"role": "system", "content": system}, *messages]}
        data = _post_json(f"{self.base_url}/chat/completions", payload, self._headers())
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage", {})
        return Completion(text=text, provider=self.name, model=data.get("model", self.model), input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"), latency_s=time.time() - t0, raw=data)

    def stream(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Iterator[str]:
        payload = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "stream": True, "messages": [{"role": "system", "content": system}, *messages]}
        for ev in _post_sse(f"{self.base_url}/chat/completions", payload, self._headers()):
            for choice in ev.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    yield piece


# ─── Ollama ──────────────────────────────────────────────────────────────────


class OllamaProvider:
    name = "ollama"

    def __init__(self, host: str | None = None, model: str | None = None):
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.model = model or _env("PM_ENGINE_OLLAMA_MODEL", "OLLAMA_MODEL", default=DEFAULT_OLLAMA_MODEL)

    def list_models(self) -> list[str]:
        data = _get_json(f"{self.host}/api/tags", {})
        return sorted(str(m.get("name")) for m in data.get("models", []) if m.get("name"))

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Completion:
        t0 = time.time()
        payload = {"model": self.model, "stream": False, "options": {"temperature": temperature, "num_predict": max_tokens}, "messages": [{"role": "system", "content": system}, *messages]}
        data = _post_json(f"{self.host}/api/chat", payload, {})
        text = (data.get("message") or {}).get("content", "")
        return Completion(text=text, provider=self.name, model=data.get("model", self.model), input_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"), latency_s=time.time() - t0, raw=data)

    def stream(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Iterator[str]:
        payload = {"model": self.model, "stream": True, "options": {"temperature": temperature, "num_predict": max_tokens}, "messages": [{"role": "system", "content": system}, *messages]}
        for ev in _post_sse(f"{self.host}/api/chat", payload, {}):
            piece = (ev.get("message") or {}).get("content")
            if piece:
                yield piece
            if ev.get("done"):
                return



# ─── Arena.ai ────────────────────────────────────────────────────────────────


class ArenaProvider:
    """Arena.ai (arena.ai) model gateway.

    Configuration (environment or ``.env``):

    * ``ARENA_API_KEY``     — required
    * ``ARENA_BASE_URL``    — required: the API root shown in your Arena API documentation / dashboard
      (arena.ai publishes no public model API at the time of writing, so there is no safe default)
    * ``ARENA_MODEL``       — model id (default ``claude-sonnet-4-5``)
    * ``ARENA_API_FORMAT``  — ``openai`` | ``anthropic`` | ``auto`` (default ``auto``)
    * ``ARENA_AUTH_HEADER`` — ``bearer`` (``Authorization: Bearer``), ``x-api-key``, or any header name

    Arena fronts many vendors' models, so the wire format is configurable:
    ``openai`` talks ``POST {base}/chat/completions`` (+ ``GET {base}/models``);
    ``anthropic`` talks ``POST {base}/messages`` with ``anthropic-version``.
    In ``auto`` mode the first request is sent in OpenAI format and, if the
    endpoint rejects the *shape* of the request (400/404/405/415/422), it is
    retried once in Anthropic format; the working format is then remembered.
    Auth failures (401/403), rate limits and server errors are **not** used
    for detection — they propagate as :class:`ProviderError` like any provider.
    """

    name = "arena"

    def __init__(self, api_key: str | None = None, model: str | None = None, base_url: str | None = None, api_format: str | None = None, auth_header: str | None = None):
        self.api_key = api_key or os.environ.get("ARENA_API_KEY")
        if not self.api_key:
            raise ProviderError("ARENA_API_KEY is not set (put it in the environment, ./.env, or ~/.aura/pm-engine.env; no key → `pm-engine setup openrouter --key <key>` for a free backend)")
        base = base_url or _env("ARENA_BASE_URL", "ARENA_API_URL")
        if not base:
            raise ProviderError("ARENA_BASE_URL is not set — Arena.ai has no public model API endpoint to default to; set it to the API root from your Arena API access (e.g. https://<host>/v1), or use a free backend: `pm-engine setup openrouter --key <key>`")
        self.base_url = base.rstrip("/")
        self.model = model or _env("ARENA_MODEL", "PM_ENGINE_ARENA_MODEL", default=DEFAULT_ARENA_MODEL) or DEFAULT_ARENA_MODEL
        fmt = (api_format or _env("ARENA_API_FORMAT", default="auto") or "auto").lower()
        if fmt not in ("auto", "openai", "anthropic"):
            raise ProviderError(f"ARENA_API_FORMAT must be auto|openai|anthropic, got '{fmt}'")
        self.api_format = fmt
        self.auth_header = (auth_header or _env("ARENA_AUTH_HEADER", default="bearer") or "bearer").lower()
        self._resolved: str | None = None if fmt == "auto" else fmt

    # -- wiring ----------------------------------------------------------

    def _headers(self) -> dict:
        if self.auth_header == "bearer":
            return {"Authorization": f"Bearer {self.api_key}"}
        return {self.auth_header: self.api_key}

    def _backend(self, fmt: str):
        if fmt == "anthropic":
            base = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
            headers = self._headers() if self.auth_header != "bearer" else {}
            if self.auth_header == "bearer":
                # Anthropic-format endpoints usually expect x-api-key; send both forms so either gateway convention works
                headers = {"x-api-key": self.api_key, "Authorization": f"Bearer {self.api_key}"}
            b = AnthropicProvider(api_key=self.api_key, model=self.model, base_url=base, extra_headers=headers)
        else:
            b = OpenAIProvider(api_key=self.api_key, model=self.model, base_url=self.base_url, extra_headers=self._headers() if self.auth_header != "bearer" else {})
        return b

    @property
    def formats(self) -> list[str]:
        if self._resolved:
            return [self._resolved]
        return ["openai", "anthropic"]

    def _call(self, op: str, *args, **kwargs):
        last: ProviderError | None = None
        for fmt in self.formats:
            try:
                out = getattr(self._backend(fmt), op)(*args, **kwargs)
                self._resolved = fmt
                return out
            except ProviderError as e:
                last = e
                if self._resolved or e.status not in FORMAT_MISMATCH_STATUS:
                    raise
        assert last is not None
        raise ProviderError(f"Arena endpoint {self.base_url} accepted neither OpenAI nor Anthropic request format: {last}", status=last.status)

    # -- Provider API ----------------------------------------------------

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Completion:
        c: Completion = self._call("complete", system, messages, max_tokens=max_tokens, temperature=temperature)
        c.provider = self.name
        return c

    def stream(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Iterator[str]:
        # streaming generators fail lazily, so detect the format with a cheap non-streaming probe only when unknown
        if not self._resolved:
            self.list_models() if self.api_format == "auto" else None
        if not self._resolved:  # /models unavailable → decide with the real request
            for fmt in self.formats:
                gen = self._backend(fmt).stream(system, messages, max_tokens=max_tokens, temperature=temperature)
                try:
                    first = next(gen)
                except StopIteration:
                    self._resolved = fmt
                    return
                except ProviderError as e:
                    if e.status in FORMAT_MISMATCH_STATUS and fmt != self.formats[-1]:
                        continue
                    raise
                self._resolved = fmt
                yield first
                yield from gen
                return
            return
        yield from self._backend(self._resolved).stream(system, messages, max_tokens=max_tokens, temperature=temperature)

    def list_models(self) -> list[str]:
        """Model ids offered by the endpoint (OpenAI-style ``GET /models``); empty list if unsupported."""
        try:
            models = self._backend("openai").list_models()
        except ProviderError as e:
            if e.status in FORMAT_MISMATCH_STATUS | {501}:
                return []
            raise
        if models and not self._resolved and self.api_format == "auto":
            self._resolved = "openai"
        return models

    def check(self) -> dict:
        """One cheap round-trip; returns a dict for ``pm-engine doctor`` / ``/api/providers``."""
        t0 = time.time()
        info: dict = {"provider": self.name, "base_url": self.base_url, "model": self.model, "format": self._resolved or self.api_format}
        with no_retries():
            try:
                models = self.list_models()
                info["models"] = len(models)
                if models and self.model not in models:
                    info["warning"] = f"model '{self.model}' not in the endpoint's model list"
                c = self.complete("Reply with the single word OK.", [{"role": "user", "content": "ping"}], max_tokens=8, temperature=0)
                info.update(ok=True, format=self._resolved, reply=c.text.strip()[:40], model=c.model, latency_s=round(time.time() - t0, 2))
            except ProviderError as e:
                info.update(ok=False, error=str(e)[:300], status=e.status, latency_s=round(time.time() - t0, 2))
        return info


# ─── Offline (deterministic; used by tests and when no key is configured) ────


class OfflineProvider(_StreamMixin):
    """Produces a structured, deterministic draft **without** any model.

    It reads the command's output template / skill instructions out of the
    system prompt and fills them with the user's request, so the whole engine
    (registry → workflow → prompt → session → export) is exercisable and
    testable in CI. It clearly labels its output as a scaffold.
    """

    name = "offline"
    model = "template-v1"

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Completion:
        t0 = time.time()
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        request = user.splitlines()[0] if user else ""
        request = re.sub(r"^Run /[\w:-]+\s*", "", request).strip()
        mode_m = re.search(r'<command name="/[\w-]+"[^>]*\bmode="([^"]+)"', system)
        if mode_m:  # drop the leading mode token(s) ("retro …", "ideas existing …")
            for tok in mode_m.group(1).split("/"):
                request = re.sub(rf"^{re.escape(tok)}\s*", "", request, flags=re.I)
        request = request or "the request"

        command = re.search(r'<command name="(/[\w-]+)"', system)
        skills = re.findall(r'<skill name="([\w:-]+)"', system)
        template = _extract_template(system)
        step_pos = re.search(r"You are executing \*\*Step ([\w+]+): (.+?)\*\* of (\d+)", system)

        out: list[str] = []
        head = f"# Draft for: {request}"
        out.append(head)
        out.append("")
        meta = []
        if command:
            meta.append(f"command `{command.group(1)}`")
        if skills:
            meta.append("skills " + ", ".join(f"`{s}`" for s in skills))
        out.append(f"_Offline scaffold ({'; '.join(meta) if meta else 'no skill loaded'}). Set ANTHROPIC_API_KEY or OPENAI_API_KEY for a full model-generated result._")
        out.append("")
        if step_pos:
            out.append(f"## Step {step_pos.group(1)} of {step_pos.group(3)}: {step_pos.group(2)}")
            out.append("")
            out.append(_step_scaffold(system, step_pos.group(1)))
        elif template:
            out.append(_fill_template(template, request))
        else:
            out.append(_skill_scaffold(system, request))
        text = "\n".join(out).rstrip() + "\n"
        return Completion(text=text[: max_tokens * 4], provider=self.name, model=self.model, input_tokens=len(system + user) // 4, output_tokens=len(text) // 4, latency_s=time.time() - t0)


def _extract_template(system: str) -> str:
    fences = re.findall(r"```[a-zA-Z0-9_-]*\n(.*?)```", system, re.S)
    for f in fences:
        if f.lstrip().startswith("## ") and "[" in f:
            return f.strip()
    return ""


def _fill_template(template: str, request: str) -> str:
    lines = []
    for line in template.splitlines():
        line = re.sub(r"\[(?:Feature|Product|Topic|Team|Initiative|Market|Company|Sprint|Job Title|repo / area|Product Name|Feature Name|Product/Feature Area|Product/Feature|Research Topic|plan in one line)[^\]]*\]", request, line)
        line = re.sub(r"\[today\]", time.strftime("%Y-%m-%d"), line)
        line = re.sub(r"\[user\]", "AURA PM Engine", line)
        lines.append(line)
    return "\n".join(lines)


def _step_scaffold(system: str, number: str) -> str:
    m = re.search(rf"^(?:### Steps? {re.escape(number)}[:.]?|\*\*Step {re.escape(number)}[:.]?)\s*(.*?)$\n(.*?)(?=^### |\*\*Step |\Z)", system, re.S | re.M)
    if not m:
        return "- (step instructions not found in prompt)"
    body = m.group(2).strip()
    bullets = [l for l in body.splitlines() if l.strip().startswith(("-", "*", "1.", "2.", "3."))]
    return "\n".join(bullets[:12]) if bullets else body[:800]


def _skill_scaffold(system: str, request: str) -> str:
    """Scaffold from the first loaded skill: its output template if it has one,
    otherwise its Instructions / Analysis Steps as a checklist, plus section headings."""
    m = re.search(r'<skill name="([\w:-]+)">\n(.*?)\n</skill>', system, re.S)
    if not m:
        return "- Follow the loaded skill's instructions for this request."
    body = m.group(2)
    out: list[str] = []
    # 1) a fenced template inside the skill
    for f in re.findall(r"```[a-zA-Z0-9_-]*\n(.*?)```", body, re.S):
        if "[" in f and ("**" in f or f.lstrip().startswith(("#", "|"))):
            out.append(_fill_template(f.strip(), request))
            break
    # 2) numbered instructions → checklist
    im = re.search(r"#{2,3} (?:Instructions|Analysis Steps[^\n]*|How It Works|Output Process)\s*\n(.*?)(?=\n#{2,3} |\Z)", body, re.S)
    steps = []
    if im:
        for line in im.group(1).splitlines():
            mm = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
            if mm:
                clean = re.sub(r"\*\*(.+?)\*\*", r"\1", mm.group(2)).strip()
                steps.append(f"{mm.group(1)}. {clean}")
    if steps:
        out.append("## Steps to complete\n\n" + "\n".join(steps[:12]))
    # 3) template section headings (### Section 1: ..., ### Part 2: ...)
    heads = re.findall(r"^#{3} ((?:Section|Part|Step|Block) [^\n]+)$", body, re.M)
    if heads and not out:
        out.append("## Sections to fill\n\n" + "\n".join(f"- {h}" for h in heads))
    if not out:
        out.append(f"## Plan for {request}\n\n- Follow the loaded skill's instructions for this request.")
    return "\n\n".join(out)


# ─── Selection ───────────────────────────────────────────────────────────────

_REGISTRY: dict[str, Callable[..., Provider]] = {
    "arena": ArenaProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
    "offline": OfflineProvider,
}


def make_provider(name: str, **kwargs) -> Provider:
    load_env_files()
    name = name.lower()
    if name not in _REGISTRY:
        raise ProviderError(f"unknown provider '{name}'. Choose from: {', '.join(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)  # type: ignore[return-value]


def auto_provider(model: str | None = None) -> Provider:
    load_env_files()
    forced = os.environ.get("PM_ENGINE_PROVIDER")
    kwargs = {"model": model} if model else {}
    if forced:
        return make_provider(forced, **kwargs)
    if os.environ.get("ARENA_API_KEY"):
        return ArenaProvider(**kwargs)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider(**kwargs)
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIProvider(**kwargs)
    if os.environ.get("OLLAMA_HOST"):
        return OllamaProvider(**kwargs)
    return OfflineProvider()


def available_providers() -> dict[str, bool]:
    load_env_files()
    return {
        "arena": bool(os.environ.get("ARENA_API_KEY")),
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "ollama": bool(os.environ.get("OLLAMA_HOST")),
        "offline": True,
    }


def iter_chunks(text: str, size: int = 120) -> Iterator[str]:
    """Utility for pseudo-streaming a completed text to SSE clients."""
    for i in range(0, len(text), size):
        yield text[i : i + size]
