---
name: cycle-context
description: Import a previous agent session (Claude Code CLI, Claude Desktop local coding sessions and cached claude.ai/code remote sessions on macOS, or Codex CLI) into the current conversation as condensed context — only the user's messages and each turn's final assistant answer, without tool calls, code edits, intermediate steps or thinking. Use when the user wants to continue from, reference, or "load" an earlier session, e.g. "hol den Kontext aus der letzten Session", "import the session where we built X", "what did we discuss yesterday in project Y", "füge die Session von gestern als Kontext hinzu".
---

# Session Context Import

Pull a previous agent session into the current conversation as **condensed context**: the user's messages, the main agent's progress notes (its short narration between tool calls) and final answer per turn, plus every subagent's result — the summary the main agent received *and* the subagent's full report, labeled with the subagent's real name (its task description). Tool calls, tool results and thinking are dropped. A multi-MB session file typically collapses to a few dozen KB.

Supported sources (parsed from local disk, nothing leaves the machine):

- **Claude Code**: `~/.claude/projects/<project>/<session-id>.jsonl`
- **Codex CLI**: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- **Claude Desktop (macOS) local coding sessions**: metadata in `~/Library/Application Support/Claude/claude-code-sessions/**/local_*.json` (title, model, cwd), transcript is the matching `<cliSessionId>.jsonl` under `~/.claude/projects`. Listed as `[desktop]`.
- **claude.ai/code REMOTE (cloud) sessions**: fetched **complete** from the Anthropic API (`/v1/code/sessions/<id>/events`, paginated back to sequence 1) using the logged-in Claude Code CLI's OAuth token (macOS Keychain / `~/.claude/.credentials.json`). Listed as `[remote]` with `--all-projects` or `--agent remote` (metadata-only rows: title, repo, status, session context size). Accepts the claude.ai id (`session_…`) or API id (`cse_…`). Offline or logged out, Claude Desktop's IndexedDB cache is the fallback — but that cache is tail-only for long sessions (`headCut`, flagged in the extract header). The extract's `Source:` line tells which one was used; report it to the user.

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
- `--agent claude|desktop|remote|codex` — the user names the tool ("die Codex Session", "die Desktop-Session", "die claude.ai/code Session", "Claude Code session")
- `--grep "keyword"` — the user remembers a topic, not a date ("the session about the sankey widget")
- `--project /path/to/dir` — sessions of a specific other project
- `-n 30` — show more
- `--json` — machine-readable (fields include `session_id` and `path`)

Interpreting the list:

- Sessions are grouped by **project entity** (`📁` header per repo/directory, ordered by most recent activity). Each row shows `[agent] id  last-activity  #user-msgs  ~tokens  title`, where `~tokens` is the estimated context cost of the **condensed** extract (chars/4) — mention it to the user when relevant, but see the full-extract rule below.
- A row marked `*ACTIVE*` is almost certainly **this currently running session** (or another session running in parallel).
- Match the user's description (topic, date, project). If exactly one session fits, proceed without asking. If several plausibly fit, show the user the shortlist (title, date, token estimate) and ask which one.

### 2. Ask for the detail level (AskUserQuestion)

Before **every** extract — a normal import, the current-session re-orientation, and the restore after a compaction — ask the user with **AskUserQuestion** (single select) which parts of the context to import. Use exactly these labels and descriptions (add the token estimate to each description when you have one — the post-compact instruction and `list` provide estimates):

1. **Full (Recommended)** — description: *"Everything: your messages, the agent's notes between tool calls, final answers, and every subagent's summary AND full report. Most complete context."* → flags: *(none)*
2. **Without subagent full reports** — description: *"Like Full, but subagent blocks keep only the short summary the main agent received; the long full reports are left out."* → `--no-subagent-reports`
3. **Final answers only** — description: *"Your messages and the agent's final answer per turn, plus the short subagent summaries (no full reports). The agent's intermediate notes between tool calls are left out."* → `--final-only --no-subagent-reports`
4. **Minimal** — description: *"Only your messages and the agent's final answers. No agent notes, no subagent blocks at all. Smallest context."* → `--final-only --no-subagents`

Never pick a reduced level on your own. If the question cannot be asked (non-interactive session), import **Full** and say so explicitly.

### 3. Extract and ingest

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
- `--final-only` / `--no-subagent-reports` / `--no-subagents` — the detail levels from step 2 (**only as chosen by the user**)
- `--current` — the currently running session of this project (see below)
- `--path FILE` — extract a specific `.jsonl` directly (bypasses discovery)
- `--json` — structured output instead of markdown
- Multiple id prefixes are allowed in one call.

### Special case: re-orient in the CURRENT session

(Ask the detail-level question from step 2 first — this is exactly the moment where full subagent reports may or may not be wanted.) After a compaction, the plugin's hook injects a short instruction with the token estimates of all four levels: ask the question with those numbers, then run the extract with the chosen flags, write it with `-o`, and Read the file **completely** (multiple Read calls for big files) — nothing may be skipped.

When the user asks you to use this skill **on the current session itself** ("damit du wieder weißt, worum es in dieser Session geht") — typically after your context was auto-compacted — skip the list step, ask the detail-level question, and run (adding the chosen flags):

```bash
python3 "<skill>/scripts/extract_session.py" extract --current
```

This resolves to the most recently written session file of the current project, which is the running session (its jsonl retains the full history even after compaction). Read the output, then give the user a short recap of the session so far: original goal, key decisions, current state, open points. If two sessions of this project run in parallel, verify the extract matches this conversation and fall back to an explicit id if not. The full-extract rule applies here too: no `--last`/`--max-chars` unless the user asked.

### 4. Confirm

After ingesting, tell the user in 2–4 sentences what context was imported (session title, time range, number of turns, main topics) so they can verify it's the right one — and explicitly whether it was the **full** extract (default) or limited on their instruction.

**Always report the import size.** Every extract prints an `Imported context: ~X.Xk tokens ≈ Y% of the …-token context window (model: …)` line on stderr — relay exactly these numbers (tokens in k, percentage of the current model's context window) to the user in your confirmation. If the line is missing, compute chars/4 yourself and say the window was assumed.

Then continue with the user's actual task, using the imported context.

## Notes

- The transcript deliberately drops tool calls, tool results, code edits, thinking and hook noise. Slash commands appear as one-line `⌘` markers, hook-triggered follow-ups as `⚙` markers. If the user needs implementation details that were only in tool output, say so — the condensed import doesn't contain them (the raw `.jsonl` path is shown by `list --json`).
- Sessions that were auto-compacted may start with a "Carried-over summary" section — that is Claude's own summary of history not present in the file.
- `list` skips sessions without any real user message (use `--include-empty` to see them).
