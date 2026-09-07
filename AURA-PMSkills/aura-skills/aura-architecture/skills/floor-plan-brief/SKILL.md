---
name: floor-plan-brief
description: "Turn a client's plain-language housing request into a structured floor-plan brief: room program, adjacency needs, zoning (public/private/service), site constraints, and budget band. Use when a user describes a house they want built, asks for a floor plan, room list, or design brief, or mentions bedrooms, ensuite, bungalow, duplex, or plot size."
---

# Floor Plan Brief

## Purpose

You are an architect-turned-product-manager producing a **floor-plan brief** for $ARGUMENTS. A brief is the contract between what the client said and what the design (or the AURA Brain layout engine) will actually generate: an explicit room program, adjacency requirements, zoning, site constraints and a budget band — with every assumption written down.

## Context

Clients describe houses in outcomes ("3-bedroom bungalow, all ensuite, big kitchen, Abuja") not in specifications. AURA's layout engine, however, needs a room graph: named rooms with minimum areas, required connections and zone membership. This skill converts the former into the latter using AURA's room rules (`AURA-Brain/knowledge/room_rules.yaml`) and adjacency matrix (`AURA-Brain/knowledge/adjacency_matrix.json`).

**Reference: AURA room rules (defaults — override only when the client says so)**

| Room | Min area (m²) | Max area (m²) | Must connect to | Avoid next to | Daylight |
|------|---------------|---------------|-----------------|---------------|----------|
| Living | 16 | 35 | Dining | Toilet, Garage | required |
| Dining | 8 | — | Kitchen, Living | — | required |
| Kitchen | 9 | 20 | Dining, Store | Toilet | required |
| Bedroom | 10 | 25 | Bathroom | — | required |
| Bathroom | 4 | 10 | — | — | ventilation |
| Toilet (guest) | 2 | — | Corridor | Living, Kitchen | ventilation |
| Office | 6 | — | Corridor / Living | — | required |
| Store | 2 | 5 | Kitchen / Corridor | — | no |
| Garage | 12 | — | Store / Corridor | Bedroom, Living | ventilation |
| Balcony | 3 | — | Living / Bedroom | — | required |

Global constraints: min ceiling height 2.4 m, min corridor width 1.0 m, min door width 0.8 m, max building depth 30 m.

Zones: **public** = Living, Dining, Kitchen · **private** = Bedrooms, Bathrooms · **service/wet** = Kitchen, Bathroom, Toilet, Laundry, Store, Garage.

## Instructions

1. **Extract what was said**: Pull out every hard requirement from the request — number and type of rooms, ensuite/shared bathrooms, storeys (bungalow vs duplex), plot size, location, budget, special needs (elderly parent, home office, generator house, boys' quarters, security gate house). Quote the client's own words for each.

2. **Fill the gaps explicitly**: For anything not said, apply the defaults above and mark it `[assumed]`. Typical Nigerian residential assumptions: guest toilet near the living room, kitchen store, a covered veranda, a water tank/ generator position, BQ if the plot is ≥ 600 m².

3. **Build the room program**: One row per room with target area (between min and max), zone, and the connections it *must* have. Sum the net floor area, add 15–20 % for walls and circulation to get gross floor area (GFA), and compare it with the plot size × allowable coverage (assume 50 % ground coverage for detached houses unless told otherwise). If GFA doesn't fit on one floor, say so and propose a duplex split (public + service downstairs, private upstairs).

4. **Set the adjacency needs**: List the top adjacencies as *must-have* (weight ≥ 0.85 in AURA's matrix: Kitchen–Dining 0.98, Living–Dining 0.95, Bedroom–Bathroom 0.90, Kitchen–Store 0.90) and *avoid* pairs (Living–Toilet, Kitchen–Toilet, Garage–Bedroom).

5. **Capture site constraints**: Orientation (north/east living preferred; bedrooms east/west), setbacks (assume 3 m front, 1.5 m sides, 3 m rear unless a local code is provided), access side, prevailing breeze, drainage/flood risk, noise sources.

6. **Set the budget band**: Convert budget to ₦ per m² of GFA. Flag it as *tight* (< ₦250k/m²), *standard* (₦250k–450k/m²) or *premium* (> ₦450k/m²) for a finished detached house in 2026 Nigeria. Don't cost anything here — that is the `build-cost-check` skill's job.

7. **Write the open questions**: Everything the client must confirm before layout generation, ordered by how much it changes the design.

## Output Template

```
## Floor Plan Brief: [Project / client name]

**Date**: [today]
**Source request**: "[client's words]"
**Building type**: [bungalow | duplex | terrace | flat] · **Plot**: [W × D m, area m²] · **Location**: [city / estate]

### Room Program
| # | Room | Qty | Target area (m²) | Zone | Must connect to | Notes |
|---|------|-----|------------------|------|-----------------|-------|

Net area: [x] m² · Circulation & walls (+[y] %): [z] m² · **GFA: [n] m²** · Floors: [1 | 2]
Coverage check: GFA ÷ floors = [footprint] m² vs allowable [plot × coverage] m² → [fits | does not fit]

### Adjacency Requirements
**Must be adjacent**: [pairs] · **Must be separated**: [pairs]

### Zoning Diagram (text)
[Public → Service → Private ordering from the entrance, one line per zone]

### Site Constraints
- Orientation: … · Setbacks: … · Access: … · Risks: …

### Budget Band
[₦ budget] ÷ [GFA] = ₦[x]/m² → **[tight | standard | premium]**

### Assumptions
- [assumed] …

### Open Questions (answer before layout generation)
1. …
```

## Notes

- Always state areas in m²; Nigerian clients often quote plots in feet (e.g. "50 × 100") — convert (50 × 100 ft = 15.24 × 30.48 m ≈ 465 m²).
- Never invent a plot size or budget; if missing, produce the brief with a placeholder and put it at the top of Open Questions.
- The brief should be pasteable into AURA Brain as the room list; keep room names to the canonical set above (Living, Dining, Kitchen, Bedroom, Bathroom, Toilet, Office, Store, Garage, Balcony, Corridor).
