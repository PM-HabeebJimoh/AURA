# aura-skills

AURA's own skills marketplace — 3 PM skills and 1 chained workflows across 1 plugins, written to the same spec as [pm-skills](../pm-skills) and validated by the same upstream validator. The PM Engine merges this directory on top of the vendored marketplace automatically (`pm-engine list` → 10 plugins · 71 skills · 43 commands).

| Plugin | Skills | Commands | What it does |
|---|---|---|---|
| `aura-architecture` | `floor-plan-brief`, `design-quality-score`, `build-cost-check` | `/design-brief` | AURA's pre-design workflow: client request → structured floor-plan brief (room program, adjacencies, zoning, GFA vs plot) → 0–100 design-quality score with fixes → ₦/m² build-cost sanity check for the Nigerian market. Grounded in `AURA-Brain/knowledge/` (room rules, adjacency matrix, cost library). |

## Use

```bash
pm-engine run "/design-brief 3-bedroom bungalow, all ensuite, 450 sqm plot in Abuja, ₦45m budget" --stream
pm-engine run "How much will a 4-bedroom duplex cost to build in Lagos?"    # auto-loads build-cost-check
pm-engine show /design-brief
```

Install directly in Claude Code:

```bash
claude plugin marketplace add ./AURA-PMSkills/aura-skills
claude plugin install aura-architecture@aura-skills
```

## Author more

```bash
pm-engine new plugin <name> -d "…"                                   # this directory is the default root
pm-engine new skill <name> --plugin <plugin> -d "…" --triggers "Use when …"
pm-engine new command <name> --plugin <plugin> --skill <skill> [--skill …] -d "…"
pm-engine validate -v && python ../pm-skills/validate_plugins.py .   # both must pass
```

Rules (enforced by the validators): kebab-case names; every skill has a `SKILL.md` with `name` + `description` front matter; commands have `description` + `argument-hint` and may only chain skills from their own plugin; the plugin README's `## Skills (N)` / `## Commands (N)` counts must match disk.
