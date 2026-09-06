# aura-architecture

AURA-specific PM skills for AI-driven architectural design: floor-plan briefs, design-quality scoring, and construction-cost sanity checks for the Nigerian market.

## Overview

Skills are nouns (domain knowledge Claude auto-loads); commands are verbs (user-triggered workflows that chain skills).

## Installation

```bash
claude plugin marketplace add ./AURA-PMSkills/aura-skills
claude plugin install aura-architecture@aura-skills
```

## Skills (3)
- **floor-plan-brief** — Turn a client's plain-language housing request into a structured floor-plan brief: room program, adjacency needs, zoning (public/private/service), site constraints, and budget band.
- **design-quality-score** — Score a floor plan or room graph on circulation, zoning, daylight, area efficiency and buildability, and explain each deduction with a concrete fix.
- **build-cost-check** — Sanity-check a residential construction cost estimate against Nigerian market rates per square metre, flag line items that look off, and show the assumptions.

## Commands (1)
- `/aura-architecture:design-brief` — Turn a client conversation into a floor-plan brief, score the resulting concept, and sanity-check the budget — AURA's end-to-end pre-design workflow.

## License

MIT
