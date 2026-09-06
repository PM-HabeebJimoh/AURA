# AURA

| Module | What it is |
|---|---|
| `AURA-Brain/` | Phase 1 architectural learning engine — floor-plan parsing, room graphs, cost library, market watch, design improver, Streamlit demo UI |
| `AURA-TeslaCore/` | Design agents (zoning, circulation) |
| `AURA-PMSkills/` | **PM Skills Marketplace + PM Engine** — the [phuryn/pm-skills](https://github.com/phuryn/pm-skills) marketplace (9 plugins · 68 skills · 42 workflows) vendored verbatim, AURA's own `aura-skills` marketplace (`aura-architecture`: floor-plan brief → design-quality score → build-cost check), plus a full runtime that executes those skills and command workflows via CLI, JSON API, streaming web UI, and any LLM (Anthropic / OpenAI-compatible / Ollama / offline). See [AURA-PMSkills/README.md](AURA-PMSkills/README.md). |

## PM Engine quick start

```bash
cd AURA-PMSkills
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pm-engine list                                        # 10 plugins · 71 skills · 43 commands (pm-skills + aura-skills)
pm-engine run "/write-prd SSO support for enterprise" # runs a chained PM workflow
pm-engine run "/design-brief 3-bed bungalow, Abuja, ₦45m" --stream   # AURA's own workflow, streamed
pm-engine new skill my-skill --plugin aura-architecture              # scaffold spec-compliant content
pm-engine serve --port 8080                           # API + web UI
pm-engine doctor                                      # one-screen health check
pm-engine test                                        # upstream validator + upstream suite + engine suite
```

The vendored marketplace is also installable directly in Claude Code / Codex:

```bash
claude plugin marketplace add ./AURA-PMSkills/pm-skills
claude plugin install pm-execution@pm-skills
```
