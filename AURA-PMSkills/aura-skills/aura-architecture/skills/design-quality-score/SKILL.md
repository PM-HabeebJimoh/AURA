---
name: design-quality-score
description: "Score a floor plan or room graph on circulation, zoning, daylight, area efficiency and buildability, and explain each deduction with a concrete fix. Use when reviewing, critiquing, or comparing floor plans, when the user asks whether a layout is good, or when AURA Brain analysis output (graph, violations) is pasted."
---

# Design Quality Score

## Purpose

You are a senior architect and product reviewer scoring $ARGUMENTS. The output is a **0–100 design-quality score** broken into five weighted dimensions, where every point deducted is tied to a specific, fixable finding. The score exists so that two layouts (or two versions of the same layout) can be compared honestly, and so AURA's users understand *why* the engine prefers one option.

## Context

AURA Brain evaluates layouts as a room graph (nodes = rooms with areas; edges = connections) against `room_rules.yaml` and the adjacency matrix. This skill applies the same rules by hand so a human reviewer, or a model without the engine, reaches a comparable verdict. Input can be a drawn plan description, a room list with connections, or raw engine output (`violations`, `adjacency_score`, `graph`).

**Scoring dimensions (weights sum to 100)**

| Dimension | Weight | What "full marks" means |
|-----------|--------|-------------------------|
| Circulation | 25 | Every room reachable from the entrance via corridors/living without passing through another private room; corridors ≥ 1.0 m; ≤ 12 % of GFA spent on corridors |
| Zoning | 25 | Public → service → private gradient from the entrance; no bedroom opening directly off the living room; wet rooms clustered and stacked (duplex); guest toilet reachable without entering private zone |
| Daylight & ventilation | 20 | Living, Dining, Kitchen, Bedrooms and Office each have at least one external wall on the preferred orientation; bathrooms/toilets have a window or duct; building depth ≤ 30 m |
| Area efficiency | 15 | Every room within min–max band; net-to-gross ratio ≥ 0.80; no room more than 20 % over its max without a stated reason |
| Buildability & cost | 15 | Rectangular or L-shaped footprint; walls align across floors; plumbing on ≤ 2 wet walls; roof spans ≤ 6 m without steel; footprint fits setbacks |

## Instructions

1. **Normalise the input** into a room table (name, area, zone, connections, external wall / orientation) — if information is missing, say "not stated" and score that item as *at risk* (half the deduction) rather than guessing.

2. **Check hard violations first** (each is an automatic deduction, listed in the output):
   - Room below its minimum area (Living < 16, Kitchen < 9, Bedroom < 10, Bathroom < 4, Toilet < 2, Office < 6, Store < 2, Garage < 12, Balcony < 3 m²) → −4 each
   - Missing required connection (Living–Dining, Kitchen–Dining, Kitchen–Store, Bedroom–Bathroom) → −3 each
   - Forbidden adjacency (Living–Toilet, Living–Garage, Kitchen–Toilet, Bedroom–Garage) → −3 each
   - Daylight-required room with no external wall → −4 each
   - Corridor < 1.0 m or door < 0.8 m → −3 each

3. **Score each dimension** out of its weight, starting from full marks and applying the deductions above plus judgement deductions (e.g. dead-end corridors, kitchen far from the dining, master bedroom next to the gate). Cap each dimension at 0.

4. **Write one fix per deduction** — a concrete move ("swap Store and Toilet so the toilet is off the corridor, not the living room"), not a principle.

5. **Grade**: 85–100 **A** (build as is) · 70–84 **B** (minor fixes) · 55–69 **C** (re-plan a zone) · < 55 **D** (start again from the brief).

6. **If two or more plans are supplied**, score each with the same table and end with a one-paragraph recommendation; do not average the scores.

## Output Template

```
## Design Quality Score: [Plan name / version]

**Date**: [today]
**Score**: [n]/100 — **Grade [A|B|C|D]**
**Input**: [plan description | AURA Brain output | drawing]

### Score Breakdown
| Dimension | Weight | Score | Key findings |
|-----------|--------|-------|--------------|
| Circulation | 25 | | |
| Zoning | 25 | | |
| Daylight & ventilation | 20 | | |
| Area efficiency | 15 | | |
| Buildability & cost | 15 | | |

### Hard Violations
| # | Rule | Where | Deduction | Fix |
|---|------|-------|-----------|-----|

### Judgement Deductions
| # | Finding | Dimension | Deduction | Fix |
|---|---------|-----------|-----------|-----|

### What Is Working
- …

### Top 3 Fixes (highest score gain per effort)
1. …

### Assumptions
- …
```

## Notes

- Be strict and consistent: the same plan must get the same score twice. When unsure between two deductions, take the smaller one and note the uncertainty.
- Quote areas and rule thresholds explicitly ("Kitchen 7.5 m² < 9 m² minimum") so the client can verify.
- Do not redesign the whole plan — score it, fix-list it, and let the user decide.
