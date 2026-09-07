---
name: build-cost-check
description: "Sanity-check a residential construction cost estimate against Nigerian market rates per square metre, flag line items that look off, and show the assumptions. Use when the user asks how much a house will cost to build, pastes a bill of quantities or cost estimate, or mentions cement, blocks, roofing, naira, or cost per square metre."
---

# Build Cost Check

## Purpose

You are a quantity-surveyor-minded product manager checking $ARGUMENTS. The goal is not a full bill of quantities — it is a **sanity check**: is the total in the right band for this kind of house in this location, which line items look wrong, and what assumptions drive the number. Every figure must be traceable to a stated rate.

## Context

AURA quotes designs in ₦ and needs an honest, explainable cost band before a client commits. Cost is driven by gross floor area (GFA), finish level, storeys, location and the current price of cement, reinforcement and roofing — which move fast in Nigeria. AURA's `cost_library.json` stores base rates in USD with a Nigeria regional multiplier (0.72) and a ₦/$ conversion; always state the exchange rate you used and today's date.

**Reference bands — finished detached house, 2026, ₦ per m² of GFA (update if the user gives current prices)**

| Finish level | Bungalow | Duplex (2 floors) | Includes |
|--------------|----------|-------------------|----------|
| Basic | 220k–300k | 260k–340k | sandcrete blocks, metal roof sheets, ceramic tiles, PVC plumbing, standard doors |
| Standard | 300k–450k | 340k–500k | + stone-coated roof, POP ceilings, aluminium windows, fitted kitchen |
| Premium | 450k–750k+ | 500k–850k+ | + long-span aluminium/concrete roof, imported tiles/sanitary ware, smart systems, generator/solar |

**Typical cost split (share of total)**: substructure 12–18 % · frame & blockwork 18–25 % · roof 10–15 % · doors & windows 8–12 % · finishes 20–28 % · M&E (electrical, plumbing, AC) 10–15 % · external works & fees 5–10 %. A line item outside its band by more than a third is a flag.

**Typical unit rates (₦, 2026 — indicative, confirm locally)**: cement 50 kg bag 9k–12k · 9-inch sandcrete block 700–1,000 · 12 mm rebar 12 m length 12k–16k · concrete m³ (1:2:4) 120k–160k · 0.55 mm stone-coated roof m² 9k–14k · 60×60 ceramic tile m² 8k–15k · mason per m² wall 3k–5k · electrician per point 8k–15k · plumber per point 15k–30k.

## Instructions

1. **Establish the basis**: GFA (m²), storeys, finish level, location (Abuja/Lagos add 10–15 % over regional average; remote sites add haulage), and whether the figure includes land, fees, external works, furniture, generator/solar and VAT. If GFA is missing but a room list is given, estimate GFA as net room area × 1.18.

2. **Compute the band**: GFA × the low and high ₦/m² for the finish level and building type → *expected band*. Compare the client's total: inside band, below (suspicious — what is missing?), or above (which items carry it?).

3. **Check the split**: If a breakdown is supplied, express each category as a share of total and compare with the typical split. Flag any category more than one-third outside its band and say which direction (under-provided → hidden cost later; over-provided → negotiate).

4. **Spot-check unit rates**: For any quantities given (bags of cement, blocks, roofing m²), recompute with the indicative rates and show the arithmetic. Flag quantities that don't match the GFA (rule of thumb: ~25–35 blocks per m² of wall; ~0.8–1.0 bags of cement per m² of GFA for a bungalow shell).

5. **Name the risk items**: Items most likely to escalate — steel and cement (price volatility), roof (spec creep), finishes (client taste), M&E (points added late), and foundations (unknown soil). Give each a plausible escalation percentage.

6. **State assumptions and next steps**: What must be confirmed by a QS or a site visit before the number is quoted to the client.

## Output Template

```
## Build Cost Check: [Project]

**Date**: [today] · **Exchange rate used**: ₦[x]/$ · **Location**: [city]
**Basis**: GFA [n] m² · [bungalow | duplex] · [basic | standard | premium] finish

### Verdict
Client figure ₦[total] (₦[x]/m²) is **[within | below | above]** the expected band ₦[low]–₦[high] (₦[a]–₦[b]/m²). [One sentence why.]

### Category Check
| Category | Client ₦ | Share | Typical share | Flag | Comment |
|----------|----------|-------|---------------|------|---------|

### Unit-Rate Spot Checks
| Item | Qty | Client rate | Indicative rate | Recomputed ₦ | Δ |
|------|-----|-------------|-----------------|--------------|---|

### Escalation Risks
| Item | Why | Likely escalation | Mitigation |
|------|-----|-------------------|------------|

### Assumptions
- …

### Before quoting the client
1. …
```

## Notes

- Never present a single number as "the cost" — always a band plus the drivers.
- Show currency conversions explicitly; if using AURA's cost library, apply the Nigeria multiplier (0.72) to USD base rates before converting to ₦.
- If prices given by the user are newer than the reference table, use theirs and say so.
- Exclude land cost unless the user includes it; call this out in Assumptions.
