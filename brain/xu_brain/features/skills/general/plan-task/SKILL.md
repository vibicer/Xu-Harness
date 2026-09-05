---
name: Plan a Task
description: Break an ambiguous task into ordered, testable steps before coding.
keywords: [plan, task, steps, roadmap, decompose, design, breakdown, todo]
match_limit: 5
---

# Plan a Task

De-risk ambiguous work by decomposing it before touching code.

## Steps
1. Restate the goal in one sentence and name the acceptance criteria.
2. List the unknowns; resolve the blocking ones before any implementation.
3. Break the work into ordered steps, each independently verifiable.
4. Note cross-step contracts (formats, schemas, interfaces) up front.
5. For each step, name how you will prove it works before moving on.

## Principles
- Own the decomposition — never outsource the top-level plan.
- Sequence only on hard dependencies; parallelize the rest.
- A step is done when its observable contract holds, not when it compiles.
