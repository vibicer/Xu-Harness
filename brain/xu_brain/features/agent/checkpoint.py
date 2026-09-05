"""Compaction / checkpoint transcript vocabulary (shared constants).

Pure-data leaf: no intra-agent imports, so the import graph stays a
clean DAG. Owned by the compaction engine, replayed-aware to the
prompt builder, read by the context meter gate and the turn-history
commit.
"""
from __future__ import annotations

_COMPRESS_AT = 0.60  # default; overridden by config

# Compaction summary: a fixed section schema so every checkpoint has the same
# shape, the resume action is never dropped, and the model merges a prior
# checkpoint instead of stacking a new one beside it.
_SUMMARY_OPEN = "<compacted-summary>"
_SUMMARY_CLOSE = "</compacted-summary>"

_COMPACTION_INSTRUCTION = "\n".join([
    "You are now acting as a compaction engine for this AI assistant. Condense the "
    "conversation ABOVE into a structured checkpoint that lets another model resume "
    "the work with no loss of essential context.",
    "",
    "Output EXACTLY the Markdown structure below: keep every section, in order. Use "
    'terse bullets, not prose paragraphs. Write "(none)" for an empty section — never '
    "drop a section.",
    "",
    "## Primary Request and Intent",
    "- [the user's original and evolving goals; quote verbatim where the exact wording matters]",
    "",
    "## Key Technical Concepts",
    "- [technologies, frameworks, patterns, and conventions in play]",
    "",
    "## Files and Code",
    "- [exact path: why it matters, key changes or snippets]",
    "",
    "## Errors and Fixes",
    "- [error: how it was resolved, plus any related user feedback]",
    "",
    "## Pending Work",
    "- [explicitly requested work not yet completed]",
    "",
    "## Current Work",
    "- [precisely what was in progress at this checkpoint]",
    "",
    "## Next Step",
    '- [the single next action, directly in line with the most recent request, or "(none)"]',
    "",
    "## Critical Context",
    "- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]",
    "",
    "Rules:",
    "- Preserve exact file paths, commands, error strings, identifiers, numeric values, "
    "function signatures, and syntax fragments.",
    "- Capture user feedback and explicit instructions faithfully, especially corrections.",
    "- Do NOT mention this summarization request or that the context was compacted.",
    "- Output only the checkpoint text: do not call any tool or take any other action.",
    f"- If the conversation already contains a {_SUMMARY_OPEN} block, it is a PRIOR "
    "checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale "
    "ones, and merge newer information into a single consolidated summary under the "
    "same structure.",
])

# Framing that makes the landed summary established context for the *resuming*
# model rather than a report addressed to the user.
_CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span of the "
    "conversation to free up context. Treat the captured context as established "
    "background and build on it without restating it. Continue the task directly from "
    "the messages that follow, without acknowledging this checkpoint."
)

# Sections the schema promises. Used to detect a truncated checkpoint: a summary
# that stops before "## Next Step" lost the resume action.
_SUMMARY_REQUIRED_SECTIONS = ("## Next Step", "## Critical Context")

# Transcript markers. Both are `system` rows: the checkpoint is the one system
# row replayed to the provider (`_build_messages`), the failure note is never
# replayed — it exists purely so the chat shows that a compaction did not land.
_CHECKPOINT_MARKER = "[conversation summary]"
_FAILURE_MARKER = "[compaction failed]"

# Engine errors that mean "nothing to do", not "something broke": they are
# logged at debug and kept out of the transcript. Auto-compaction re-fires every
# turn while over threshold, so surfacing these would spam a row per turn.
_BENIGN_COMPACTION_ERRORS = frozenset({
    "not enough history to compress",
    "compaction did not shrink; abandoning",
})
