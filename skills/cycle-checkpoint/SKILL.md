---
name: cycle-checkpoint
description: Run the pre-compaction documentation checkpoint manually, at any context level — no need to wait for the automatic 80% trigger. Updates ALL project documentation (incl. architecture docs and learnings from mistakes), then ends the turn with a numbered next-steps list and asks the user to run /compact. Use when the user says "cycle checkpoint", "doc checkpoint", "mach einen doku-checkpoint", "sichere den session-stand", "persist the session knowledge now", "checkpoint before compact", or wants to compact soon and lose nothing.
---

# Manual Documentation Checkpoint

The same checkpoint the context-cycle plugin triggers automatically at 80% of the context window — run on demand. `<skill>` below stands for this skill's base directory (announced when the skill loads).

## Steps

### 1. Report context status

```bash
python3 "<skill>/../cycle-context/hooks/pre_compact_docs_reminder.py" --status
```

Mention the result (e.g. "~250k of 1000k tokens, 25%") in your final message so the user can judge whether compacting is worth it yet.

### 2. Do the checkpoint

1. Bring **ALL** project documentation up to date — README, CLAUDE.md, docs/, skill files, knowledge-base notes, **and architecture documentation** (system design, component relationships, data flows, key decisions with their rationale).
2. Persist **learnings from mistakes** made in this session: what went wrong, the root cause, and the rule to prevent repeating it — written into the project's docs/knowledge base so future sessions do not repeat these errors.
3. Finish or checkpoint any half-done edits.

### 3. Suppress the duplicate automatic reminder

```bash
python3 "<skill>/../cycle-context/hooks/pre_compact_docs_reminder.py" --mark
```

This arms the once-per-cycle marker so the automatic 80% reminder stays silent until after the next compaction (the docs are fresh now — a second checkpoint order would be redundant).

### 4. Stop and hand over

Do NOT continue with the task. END your turn with a final message containing:

- a short confirmation of the documentation checkpoint (what was updated),
- a condensed summary of the RELEVANT findings from recent tool results that are not yet reflected in docs or earlier final answers — key facts only (tool results do not survive compaction),
- a precise, **numbered list of the NEXT STEPS** you had planned, concrete enough that you could resume from this list alone — the post-compact context restore keeps user messages and final answers, so exactly this message will land back in your context after compaction,
- the current context status from step 1,
- the explicit request that the user should now run `/compact`.

You cannot trigger `/compact` yourself — do not try. Wait for the user.

**Exception:** if the user explicitly asked for the checkpoint *without* compacting ("ohne compact", "just update the docs"), skip step 3's marker, omit the /compact request, and simply confirm the documentation update — then continue as the user directs.
