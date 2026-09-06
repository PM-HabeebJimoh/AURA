# AURA-PMSkills — PM Skills Marketplace + PM Engine

This module brings the **[PM Skills Marketplace](https://github.com/phuryn/pm-skills)** (68 product-management skills and 42 chained workflows across 9 plugins, by Paweł Huryn) into AURA — and adds a **full engine** that can run that content anywhere, not only inside Claude Code.

```
AURA-PMSkills/
├── pm-skills/          ← the marketplace, vendored verbatim from upstream (v2.1.0 @ 18468a9)
│   ├── .claude-plugin/marketplace.json
│   ├── pm-product-discovery/ … pm-ai-shipping/   (9 plugins: skills/*/SKILL.md + commands/*.md)
│   ├── validate_plugins.py, tests/               (upstream validator + consistency suite)
│   └── UPSTREAM                                  (pinned upstream commit)
├── aura-skills/        ← AURA's own marketplace (same spec) — merged on top of pm-skills automatically
│   └── aura-architecture/   (3 skills + `/design-brief` workflow: floor-plan brief → design score → cost check)
├── pm_engine/          ← the engine (pure Python, stdlib core)
├── tests/              ← engine test-suite (163 tests; includes a mock LLM gateway for the Arena path)
└── pyproject.toml      ← `pip install -e .` → `pm-engine` CLI
```

## What the engine does

Upstream ships *content* that Claude Code interprets at runtime. The engine makes that content executable and inspectable on its own:

| Capability | Module | Upstream behaviour it reproduces |
|---|---|---|
| Discover marketplaces → plugins → skills → commands | `registry.py` | plugin manifests, `SKILL.md` front matter, `commands/*.md` |
| Route `/command`, `/plugin:command`, `/skill`, `/plugin:skill`, free text | `runner.py` | slash invocation & force-loading skills |
| **Auto-load** the right skills from plain-English requests (BM25 + trigger phrases) | `search.py` | "skills are loaded automatically when relevant" |
| Parse each command into an executable **workflow**: steps, modes, checkpoints, chained skills, output template, follow-up offers | `workflow.py` | `### Step N`, `[plan\|retro] <ctx>` modes, `**Checkpoint**`, "Offer Next Steps" |
| Assemble system/user prompts, substitute `$ARGUMENTS`, delimit attachments as untrusted data | `prompt.py` | `$ARGUMENTS` placeholder, untrusted-input rule |
| Execute with **Arena.ai**, **Anthropic**, **OpenAI-compatible**, **Ollama**, or a deterministic **offline** provider — **streaming** on every backend, retries with back-off on 429/5xx/529; keys from env or `.env` files (`pm-engine setup`) | `providers.py`, `config.py` | — |
| Persist multi-turn **sessions**; run a workflow one step at a time and pause at checkpoints | `session.py`, `runner.py` | checkpoint pauses |
| **Export** skills to Claude / Cursor / Gemini CLI / OpenCode / Kiro / Codex (commands → skills) / JSON bundle / rendered prompts | `export.py` | README "Other AI assistants" section |
| **Validate**: upstream plugin-spec checks + engine-level checks (unresolved skill refs, unparsable workflows, count drift) | `validator.py` | `validate_plugins.py` + `tests/` |
| Merge **several marketplaces** (vendored `pm-skills` + `aura-skills` + `$PM_SKILLS_EXTRA` / `--extra`) into one namespace; duplicates are an error | `registry.py` | multiple `/plugin marketplace add` |
| **Scaffold** spec-compliant marketplaces, plugins, skills and commands (`pm-engine new`) | `scaffold.py` | upstream plugin conventions |
| Re-vendor from upstream with a pinned commit | `updater.py` | — |
| CLI, JSON API, web UI | `cli.py`, `server.py`, `static/` | — |

## Install

```bash
cd AURA-PMSkills
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # flask + pyyaml + pytest; the engine core itself is stdlib-only
pm-engine --version
```

### Connect a model backend

Without a key the engine runs fully offline (deterministic template scaffolds). To get real model output, connect **Arena.ai** — or any of the other backends:

```bash
# Arena.ai — one command: stores the key in ~/.aura/pm-engine.env (0600, git-ignored), makes it the default, tests it
pm-engine setup arena --key <YOUR_ARENA_API_KEY> --check
#   options: --base-url https://…/v1   --model <id>   --api-format auto|openai|anthropic   --auth-header bearer|x-api-key
#   (omit --key to be prompted without echo)

pm-engine providers --check      # one tiny request per configured backend → "✓ arena: reachable — model …"
pm-engine doctor --check         # full health check incl. the live provider round-trip
pm-engine run "/write-prd SSO for enterprise" --stream
```

Or configure through the environment / a `.env` file (see [`.env.example`](.env.example); real env vars always win, then `./.env`, then `~/.aura/pm-engine.env`):

| Variable | Purpose | Default |
|---|---|---|
| `ARENA_API_KEY` | Arena.ai key — when set, Arena is selected automatically | — |
| `ARENA_BASE_URL` | API root of the Arena model endpoint | `https://api.arena.ai/v1` |
| `ARENA_MODEL` | model id to request | `claude-sonnet-4-5` |
| `ARENA_API_FORMAT` | wire format: `auto` (try OpenAI `chat/completions`, fall back to Anthropic `messages`) · `openai` · `anthropic` | `auto` |
| `ARENA_AUTH_HEADER` | `bearer` (`Authorization: Bearer`) · `x-api-key` · any header name | `bearer` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` (+`OPENAI_BASE_URL`) / `OLLAMA_HOST` | other backends | — |
| `PM_ENGINE_PROVIDER` | force `arena` · `anthropic` · `openai` · `ollama` · `offline` | auto |
| `PM_ENGINE_MAX_RETRIES` | retries with back-off on 429/5xx/529 and network errors | `3` |

Selection order when nothing is forced: `ARENA_API_KEY` → `ANTHROPIC_API_KEY` → `OPENAI_API_KEY` → `OLLAMA_HOST` → offline. Keys are never written to logs, sessions, artifacts or API responses (`/api/providers` only reports the base URL, model and a redacted check result).

**Offline sandbox?** `python -m tests.mock_gateway --port 9001` starts a local stand-in that speaks both wire formats (`pm-engine setup arena --key test-arena-key --base-url http://127.0.0.1:9001/v1 --check`) — the same server the test-suite uses to prove the Arena path end-to-end.

## Use

```bash
pm-engine list                      # 9 plugins · 68 skills · 42 commands
pm-engine list commands --plugin pm-execution
pm-engine show /discover            # command source + parsed workflow
pm-engine workflow /sprint          # step graph incl. modes, checkpoints, offers, output template
pm-engine search north star metric  # BM25 search over skills + commands
pm-engine resolve "What are the riskiest assumptions for our AI assistant?"   # routing preview

# run a command (all steps), a single step, or step-by-step with a saved session
pm-engine run "/write-prd SSO support for enterprise customers" --out ./artifacts
pm-engine run "/discover AI meeting summarizer for remote teams" --step 1 --save-session
pm-engine run "Carry all 10 ideas forward" --session <id>        # continues at the next step
pm-engine run "/plan-launch Dev productivity tool" --all-steps    # pauses at checkpoints

# force a skill, auto-load skills from plain text, attach files
pm-engine run "/lean-canvas Marketplace for freelance PMs"
pm-engine run "Summarize this interview into JTBD and action items" --file interview.txt
pm-engine run "/sprint retro The last sprint shipped late" --show-prompt

pm-engine run "/design-brief 3-bed bungalow, 450 sqm plot, Abuja, ₦45m" --stream   # AURA plugin, tokens as they arrive
pm-engine chat                      # interactive REPL with session memory (streams)
pm-engine serve --port 8080         # JSON API + web UI (binds 0.0.0.0; UI streams via /api/stream)
pm-engine export cursor .           # → .cursor/skills/*  (also: claude gemini opencode kiro codex bundle prompts)
pm-engine validate -v               # upstream spec + engine checks → exit 1 on errors
pm-engine test                      # upstream validator + upstream unittest suite + engine pytest suite
pm-engine doctor [--check]          # environment, roots, counts, validation, provider (+ live round-trip), sessions
pm-engine setup arena --key … --check   # store + verify Arena.ai credentials (also: setup anthropic|openai)
pm-engine providers [--check]       # which backends are configured (+ one tiny request each)
pm-engine update-skills             # re-vendor upstream main (updates pm-skills/UPSTREAM)

# author your own plugins (written to ./aura-skills by default — picked up automatically)
pm-engine new plugin aura-architecture -d "…"
pm-engine new skill floor-plan-brief --plugin aura-architecture -d "…" --triggers "Use when …"
pm-engine new command design-brief --plugin aura-architecture --skill floor-plan-brief -d "…"
pm-engine --extra ~/my-skills list  # merge any other marketplace / plugin dir (or $PM_SKILLS_EXTRA); --no-extra = upstream only
```

### Python API

```python
from pm_engine import Engine
from pm_engine.session import Session

engine = Engine()                                   # vendored marketplace + auto-selected provider
print(engine.resolve("/brainstorm ideas existing Mobile banking"))

s = Session.new()
r = engine.run("/discover AI meeting summarizer", session=s, step=1)
print(r.text, r.checkpoint, r.offers)
r = engine.run("carry all ideas forward", session=s)  # → step 2

wf = engine.workflow("/discover")                    # parsed workflow: steps, skills, checkpoints, template

for item in engine.stream("/write-prd SSO", session=s):   # str chunks…, then the final RunResult
    print(item, end="") if isinstance(item, str) else print("\n", item.artifact)

from pm_engine.registry import Registry
Engine(Registry("pm-skills", extra_roots=["aura-skills", "~/my-skills"]))   # explicit multi-root
```

### HTTP API

`GET /api/health · /api/plugins[/name] · /api/skills[/ref] · /api/commands[/ref] · /api/search?q= · /api/graph · /api/validate · /api/providers[?check=1] · /api/sessions[/id]`
`POST /api/resolve {text} · /api/prompt {text, step?} · /api/run {text, session_id?, step?, attachments?[{name,text}], skills?[]}`
`POST /api/stream` — same body as `/api/run`, answers as Server-Sent Events: `event: chunk {text}` … then one `event: result {…RunResult}` (or `event: error {error}`).

## Test & confirm

```bash
cd AURA-PMSkills
(cd pm-skills && python validate_plugins.py && python -m unittest discover -s tests)   # upstream: 110 components, 15 tests
python pm-skills/validate_plugins.py aura-skills                                       # upstream validator on AURA's plugin: PASS
pm-engine validate -v                                                                  # both roots: 10 plugins · 71 skills · 43 commands, 0 errors, 0 warnings
python -m pytest tests -q                                                              # 163 passed (Arena path exercised against the mock gateway)
pm-engine doctor                                                                       # "doctor: all good"
```

A ready-made GitHub Actions workflow running the same three layers on Python 3.10–3.13 is in [`ci/pm-engine-tests.yml`](ci/README.md) — move it to `.github/workflows/` to enable it.

## Design notes

* **Content is never modified.** `pm-skills/` is byte-identical to upstream (minus the 5 MB install GIF and upstream's CI workflow). Upstream's own validator and test-suite run unchanged against it, so the vendored copy is always a valid Claude Code / Codex marketplace: `claude plugin marketplace add ./AURA-PMSkills/pm-skills`.
* **Upstream design rules are enforced at runtime**: skills referenced by a command are resolved *inside the same plugin only*; `$ARGUMENTS` is substituted in commands and skills, never in front matter; the model is told to suggest cross-plugin follow-ups in natural language.
* **Offline provider** produces deterministic scaffolds from the command's output template / the skill's template, so the whole pipeline is testable in CI without network or keys, and clearly labels itself as a scaffold.
* Attachments are wrapped in `<attachment>` blocks and the system prompt carries upstream's untrusted-input rule.
* **Two roots, one namespace.** `pm-skills/` stays pristine so `pm-engine update-skills` is a clean re-vendor; AURA's own content lives in `aura-skills/` (its own `marketplace.json`, validated by the same upstream script, installable with `claude plugin marketplace add ./AURA-PMSkills/aura-skills`). The registry merges both; per-root README counts, marketplace listings and version sync are validated per root.
* **Streaming is first-class.** Anthropic (`content_block_delta`), OpenAI (`choices[].delta`) and Ollama (`message.content`) stream natively; the offline provider chunks its scaffold; `Engine.stream()` yields text then the same `RunResult` as `run()`, so sessions, artifacts and offers behave identically.

## License

Engine: MIT. Marketplace content: MIT © Paweł Huryn (see `pm-skills/LICENSE`).
