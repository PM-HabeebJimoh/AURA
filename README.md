# AURA

| Module | What it is |
|---|---|
| `AURA-Brain/` | Phase 1 architectural learning engine — floor-plan parsing, room graphs, cost library, market watch, design improver, Streamlit demo UI |
| `AURA-TeslaCore/` | Design agents (zoning, circulation) |
| `AURA-PMSkills/` | **PM Skills Marketplace + PM Engine** — the [phuryn/pm-skills](https://github.com/phuryn/pm-skills) marketplace (9 plugins · 68 skills · 42 workflows) vendored verbatim, plus a full runtime that executes those skills and command workflows via CLI, JSON API, web UI, and any LLM (Anthropic / OpenAI-compatible / Ollama / offline). See [AURA-PMSkills/README.md](AURA-PMSkills/README.md). |

## PM Engine quick start

```bash
cd AURA-PMSkills
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pm-engine list                                        # 9 plugins · 68 skills · 42 commands
pm-engine run "/write-prd SSO support for enterprise" # runs a chained PM workflow
pm-engine serve --port 8080                           # API + web UI
pm-engine test                                        # upstream validator + upstream suite + engine suite
```

The vendored marketplace is also installable directly in Claude Code / Codex:

```bash
claude plugin marketplace add ./AURA-PMSkills/pm-skills
claude plugin install pm-execution@pm-skills
```
