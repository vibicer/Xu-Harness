---
name: Code Review
description: Review code changes for correctness, readability, and risk before merge.
keywords: [review, code, refactor, pr, diff, pull request, lint, correctness]
match_limit: 5
---

# Code Review

A disciplined review pass for a diff or patch before it lands.

## Steps
1. Summarize the change in one sentence — what it does and why.
2. Walk each hunk: check correctness, edge cases, error handling, and naming.
3. Flag risk: data loss, security, race conditions, untested paths.
4. Suggest concrete edits as diffs where useful; prefer small, surgical changes.
5. Give a verdict: approve / request changes / block, with reasons.

## Principles
- Review the *change*, not the author.
- Distinguish must-fix from nice-to-have; batch nitpicks.
- If a test covers the changed contract, call it out; if none, ask for one.
