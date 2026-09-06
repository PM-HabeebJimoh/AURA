"""LLM providers.

All providers implement :class:`Provider.complete` with the same signature so
the engine can swap them. Only the standard library is required — Anthropic
and OpenAI-compatible backends are called over plain HTTPS with
``urllib`` so no SDK install is needed.

Selection (``pm_engine.providers.auto_provider``):

1. explicit ``PM_ENGINE_PROVIDER`` = ``anthropic`` | ``openai`` | ``ollama`` | ``offline``
2. ``ANTHROPIC_API_KEY`` present  → Anthropic
3. ``OPENAI_API_KEY`` present     → OpenAI-compatible (``OPENAI_BASE_URL`` honoured)
4. ``OLLAMA_HOST`` present        → Ollama
5. otherwise                       → :class:`OfflineProvider` (deterministic, no network)
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator, Protocol

DEFAULT_ANTHROPIC_MODEL = os.environ.get("PM_ENGINE_ANTHROPIC_MODEL", "claude-sonnet-4-5")
DEFAULT_OPENAI_MODEL = os.environ.get("PM_ENGINE_OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_OLLAMA_MODEL = os.environ.get("PM_ENGINE_OLLAMA_MODEL", "llama3.1")


class ProviderError(RuntimeError):
    pass


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


def _post_json(url: str, payload: dict, headers: dict, timeout: float = 180.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — https to configured API host
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # pragma: no cover - network
        detail = e.read().decode("utf-8", errors="replace")[:2000]
        raise ProviderError(f"HTTP {e.code} from {url}: {detail}") from e
    except urllib.error.URLError as e:  # pragma: no cover - network
        raise ProviderError(f"network error calling {url}: {e.reason}") from e


# ─── Anthropic ───────────────────────────────────────────────────────────────


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_ANTHROPIC_MODEL, base_url: str | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        self.model = model
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Completion:
        t0 = time.time()
        payload = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "system": system, "messages": messages}
        data = _post_json(
            f"{self.base_url}/v1/messages",
            payload,
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
        )
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage = data.get("usage", {})
        return Completion(text=text, provider=self.name, model=data.get("model", self.model), input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), latency_s=time.time() - t0, raw=data)


# ─── OpenAI-compatible (OpenAI, Azure-compatible gateways, LM Studio, vLLM…) ──


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_OPENAI_MODEL, base_url: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        if not self.api_key and "api.openai.com" in self.base_url:
            raise ProviderError("OPENAI_API_KEY is not set")
        self.model = model

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Completion:
        t0 = time.time()
        payload = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "messages": [{"role": "system", "content": system}, *messages]}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = _post_json(f"{self.base_url}/chat/completions", payload, headers)
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage", {})
        return Completion(text=text, provider=self.name, model=data.get("model", self.model), input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"), latency_s=time.time() - t0, raw=data)


# ─── Ollama ──────────────────────────────────────────────────────────────────


class OllamaProvider:
    name = "ollama"

    def __init__(self, host: str | None = None, model: str = DEFAULT_OLLAMA_MODEL):
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.model = model

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 4096, temperature: float = 0.4) -> Completion:
        t0 = time.time()
        payload = {"model": self.model, "stream": False, "options": {"temperature": temperature, "num_predict": max_tokens}, "messages": [{"role": "system", "content": system}, *messages]}
        data = _post_json(f"{self.host}/api/chat", payload, {})
        text = (data.get("message") or {}).get("content", "")
        return Completion(text=text, provider=self.name, model=data.get("model", self.model), input_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"), latency_s=time.time() - t0, raw=data)


# ─── Offline (deterministic; used by tests and when no key is configured) ────


class OfflineProvider:
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
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
    "offline": OfflineProvider,
}


def make_provider(name: str, **kwargs) -> Provider:
    name = name.lower()
    if name not in _REGISTRY:
        raise ProviderError(f"unknown provider '{name}'. Choose from: {', '.join(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)  # type: ignore[return-value]


def auto_provider(model: str | None = None) -> Provider:
    forced = os.environ.get("PM_ENGINE_PROVIDER")
    kwargs = {"model": model} if model else {}
    if forced:
        return make_provider(forced, **kwargs)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider(**kwargs)
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIProvider(**kwargs)
    if os.environ.get("OLLAMA_HOST"):
        return OllamaProvider(**kwargs)
    return OfflineProvider()


def available_providers() -> dict[str, bool]:
    return {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "ollama": bool(os.environ.get("OLLAMA_HOST")),
        "offline": True,
    }


def iter_chunks(text: str, size: int = 400) -> Iterator[str]:
    """Utility for pseudo-streaming a completed text to SSE clients."""
    for i in range(0, len(text), size):
        yield text[i : i + size]
