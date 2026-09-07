---
description: "Turn a client conversation into a floor-plan brief, score the resulting concept, and sanity-check the budget — AURA's end-to-end pre-design workflow"
argument-hint: "<client request, e.g. '3-bedroom bungalow on a 450 sqm plot in Abuja, ₦45m budget'>"
---

# /design-brief -- AURA Pre-Design Workflow

Take a client's plain-language request and produce the three documents AURA needs before generating a layout: a **floor-plan brief**, a **design-quality score** of the proposed concept, and a **build-cost sanity check** of the budget. Ends in a saved markdown document the client can sign off.

## Invocation

```
/design-brief 3-bedroom bungalow, all ensuite, 450 sqm plot in Abuja, ₦45m budget
/design-brief @client-notes.md
/design-brief                    # asks what you need
```

## Workflow

### Step 1: Gather Context

Ask conversationally — most important questions first, and stop as soon as you have enough to start:

1. **Rooms**: How many bedrooms? All ensuite? Home office, BQ, garage?
2. **Site**: Plot size (m² or ft × ft), location/estate, which side the road is on
3. **Storeys**: Bungalow or duplex? Any preference?
4. **Budget**: Total construction budget in ₦, and whether it includes fees, external works and land
5. **Finish level**: Basic, standard or premium?

Accept context from uploaded files, pasted text, or the conversation. If the user has already given most of this in $ARGUMENTS, confirm the assumptions in one message rather than asking again.

### Step 2: Write the Floor-Plan Brief

Apply the **floor-plan-brief** skill:

- Build the room program from the client's words + AURA room-rule defaults, marking every default `[assumed]`
- Compute net area → GFA and check it against plot coverage; propose a duplex split if it doesn't fit
- List must-have adjacencies and must-avoid pairs, site constraints, budget band and open questions

**Checkpoint**: "Here's the brief. Should I change any room, area or assumption before I score the concept?"

### Step 3: Score the Concept

Apply the **design-quality-score** skill to the zoning/adjacency concept implied by the brief (or to a plan the user provides):

- Run the hard-violation checks against the room program
- Score the five dimensions, list fixes, and grade A–D
- If the grade is C or D, propose the changes to the brief that would lift it before moving on

**Checkpoint**: "The concept scores [n]/100 (Grade [x]). Want me to apply the top fixes to the brief, or proceed to the budget check?"

### Step 4: Sanity-Check the Budget

Apply the **build-cost-check** skill:

- Compute the expected ₦ band from GFA × finish level × building type (+ location uplift)
- Compare with the client's budget and name the drivers and escalation risks
- If the budget is below band, offer the three levers: reduce GFA, drop finish level, or phase the build

### Step 5: Generate the Output

```
## Design Brief: [Project / client]

**Date**: [today]

### Summary
[3 sentences: what will be built, whether it fits the plot and the budget, the one decision the client must make]

### 1. Floor-Plan Brief
[Full brief from Step 2]

### 2. Design Quality Score
[Score table, violations and top fixes from Step 3]

### 3. Build Cost Check
[Verdict, band and risks from Step 4]

### Decisions Needed From the Client
| # | Decision | Options | Recommended | By when |
|---|----------|---------|-------------|---------|

### Next Steps
- [Action, owner, timing]
```

Save the output as `DesignBrief-[project]-[date].md` in the user's workspace.

### Step 6: Offer Next Steps

- "Want me to **generate the layout in AURA Brain** from this room program?"
- "Should I **run a pre-mortem** on the construction plan?"
- "Want a **client-facing one-pager** of this brief?"

## Notes

- Be opinionated — recommend one building type and one finish level, and say why
- State assumptions explicitly; the client is signing off on them as much as on the rooms
- Work in metres and ₦ throughout; convert feet and $ once, visibly
- Suggest follow-ups in natural language; never hard-reference commands from other plugins
