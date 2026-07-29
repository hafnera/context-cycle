---
name: session-context
description: Import a previous local agent session (Claude Code or Codex CLI) into the current conversation as condensed context — only the user's messages and each turn's final assistant answer, without tool calls, code edits, intermediate steps or thinking. Use when the user wants to continue from, reference, or "load" an earlier session, e.g. "hol den Kontext aus der letzten Session", "import the session where we built X", "what did we discuss yesterday in project Y", "füge die Session von gestern als Kontext hinzu".
---

# Session Context Import

Pull a previous local agent session into the current conversation as **condensed context**: only user messages and each turn's final assistant answer. A multi-MB session file typically collapses to a few dozen KB.

Supported sources (parsed from local disk, nothing leaves the machine):

- **Claude Code**: `~/.claude/projects/<project>/<session-id>.jsonl`
- **Codex CLI**: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`

All commands use the bundled script (stdlib-only Python 3). `<skill>` below stands for this skill's base directory (announced when the skill loads):

```bash
python3 "<skill>/scripts/extract_session.py" <command> [options]
```

## Workflow

### 1. Find the session

```bash
python3 "<skill>/scripts/extract_session.py" list
```

Defaults: sessions of **both agents** for the **current project directory**, newest first, max 15. Useful options:

- `--all-projects` — the user references another project or "some session last week"
- `--agent claude|codex` — the user names the tool ("die Codex Session", "Claude Code session")
- `--grep "keyword"` — the user remembers a topic, not a date ("the session about the sankey widget")
- `--project /path/to/dir` — sessions of a specific other project
- `-n 30` — show more
- `--json` — machine-readable (fields include `session_id` and `path`)

Interpreting the list:

- Sessions are grouped by **project entity** (`📁` header per repo/directory, ordered by most recent activity). Each row shows `[agent] id  last-activity  #user-msgs  ~tokens  title`, where `~tokens` is the estimated context cost of the **condensed** extract (chars/4) — mention it to the user when relevant, but see the full-extract rule below.
- A row marked `*ACTIVE*` is almost certainly **this currently running session** (or another session running in parallel).
- Match the user's description (topic, date, project). If exactly one session fits, proceed without asking. If several plausibly fit, show the user the shortlist (title, date, token estimate) and ask which one.

### 2. Extract and ingest

**Full-extract rule: the default is ALWAYS the complete extract — never apply `--last` or `--max-chars` on your own judgment.** Those options exist solely for when the user has explicitly asked for a limited import. If a full extract seems too large to ingest, do not silently limit it: tell the user the size (from `-o` output or the list's token estimate) and ask how to proceed. If a limit was used (on the user's instruction), state that clearly in your confirmation so it never happens unnoticed.

For a normal-sized session (≲ 30 user messages), print straight to stdout — the output lands directly in your context:

```bash
python3 "<skill>/scripts/extract_session.py" extract <id-prefix> [--all-projects]
```

For big sessions, write to a file first (the command reports the size), then Read the file — completely, in multiple chunks if necessary:

```bash
python3 "<skill>/scripts/extract_session.py" extract <id-prefix> --all-projects -o /tmp/session_ctx.md
```

Extract options:

- `--last N` — keep only the last N user messages + their answers (**only when the user asked for it**)
- `--max-chars N` — truncate each message to N chars (**only when the user asked for it**)
- `--all-text` — keep *all* assistant text of a turn (intermediate status notes too), not just the final answer
- `--current` — the currently running session of this project (see below)
- `--path FILE` — extract a specific `.jsonl` directly (bypasses discovery)
- `--json` — structured output instead of markdown
- Multiple id prefixes are allowed in one call.

### Special case: re-orient in the CURRENT session

When the user asks you to use this skill **on the current session itself** ("damit du wieder weißt, worum es in dieser Session geht") — typically after your context was auto-compacted — skip the list step and run:

```bash
python3 "<skill>/scripts/extract_session.py" extract --current
```

This resolves to the most recently written session file of the current project, which is the running session (its jsonl retains the full history even after compaction). Read the output, then give the user a short recap of the session so far: original goal, key decisions, current state, open points. If two sessions of this project run in parallel, verify the extract matches this conversation and fall back to an explicit id if not. The full-extract rule applies here too: no `--last`/`--max-chars` unless the user asked.

### 3. Confirm

After ingesting, tell the user in 2–4 sentences what context was imported (session title, time range, number of turns, main topics) so they can verify it's the right one — and explicitly whether it was the **full** extract (default) or limited on their instruction.

**Always report the import size.** Every extract prints an `Imported context: ~X.Xk tokens ≈ Y% of the …-token context window (model: …)` line on stderr — relay exactly these numbers (tokens in k, percentage of the current model's context window) to the user in your confirmation. If the line is missing, compute chars/4 yourself and say the window was assumed.

Then continue with the user's actual task, using the imported context.

## Notes

- The transcript deliberately drops tool calls, tool results, code edits, thinking and hook noise. Slash commands appear as one-line `⌘` markers, hook-triggered follow-ups as `⚙` markers. If the user needs implementation details that were only in tool output, say so — the condensed import doesn't contain them (the raw `.jsonl` path is shown by `list --json`).
- Sessions that were auto-compacted may start with a "Carried-over summary" section — that is Claude's own summary of history not present in the file.
- `list` skips sessions without any real user message (use `--include-empty` to see them).
