# Changelog

All notable changes of the context-cycle plugin. Versions are the `version` in `.claude-plugin/plugin.json`; the plugin updater only offers an update when it changes.

## 1.20.0 — 2026-11-24
- The agent's changes to its list of open questions for the owner (offene-fragen mod: `frage_setzen`, `frage_entfernen`, `fragen_uebernehmen`) appear as short agent notes at the place of the call — `**Agent note (16:05) · new question F14:** <frage> — <kurz>`, `… · question F17 changed: …` (only the fields set), `… · question F3 closed: <grund>`, `… · questions taken over: …` — never the long fields. Rejected calls and `fragen_lesen` are left out. Kept at every level except Minimal (new flag `--no-question-notes`), not counted by `--notes-last`; `create` writes them back as the same tool calls. Requested by the owner via the Mods session: since questions go only through the mod's tool, the extract held every answer but none of the questions.

## 1.19.3 — 2026-11-24
- Post-compact instruction and skill: the detail-level question is asked even if a project rule, a memory note or a user message that arrived with the compaction seems to prescribe a level or to allow skipping it (an agent had skipped the question claiming an owner rule that did not exist). Owner's decision (F2).

## 1.19.2 — 2026-11-17
- Skill: token estimates from a post-compact instruction are only valid for the question asked right after that compaction; when the skill is invoked later (slash command, reopened session, plugin update) the agent computes fresh sizes (`-o /dev/null`) instead of copying stale numbers from the context.

## 1.19.1 — 2026-11-17
- An incoming message from another Claude session no longer ends the agent's turn (reverts that part of 1.18.2): the agent's text before it is an intermediate note, the turn's final answer is the last text before the next user message. In a session that peers wake constantly, "Final answers only" had grown from ~96k to ~234k tokens (259 instead of 58 answers); the messages themselves stay extracted as intermediate messages. Owner's decision (F1).

## 1.19.0 — 2026-11-17
- Extract markers are plain text instead of emojis (`## USER MESSAGE`, `### MAIN AGENT`, `**Agent note (time):**`, `**USER INTERJECTION …**`, `**MESSAGE FROM/TO ANOTHER CLAUDE SESSION …**`, `*User ran:*`, `WARNING:`): each emoji cost 4–5 tokens (0.85% of a large extract).

## 1.18.2 — 2026-11-17
- Messages exchanged with other Claude sessions (cross-session messaging) are extracted: incoming `cross-session-message` meta entries as 📨, outgoing `SendMessage` calls as 📤 (named via the sender's address). They count as intermediate messages: Final answers only drops them, `--notes-last N` keeps the newest together with the agent's notes. An incoming message that arrives while the agent is idle closes its previous answer, so answers that were followed by peer traffic are no longer misread as notes. `create` writes them natively.

## 1.18.1 — 2026-11-17
- The question card keeps its four levels; choosing **Final answers only** triggers a follow-up question: keep the agent's most recent notes — 0 / 50 / 100 / All / any number (`--notes-last N`), each with the total token estimate. Hook, skill, Mac app and docs updated.

## 1.18.0 — 2026-11-17
- New flag `--notes-last N` (keep only the N most recent agent notes; with `--no-subagent-reports` the level "Final answers + last N notes").

## 1.17.1 — 2026-11-03
- Desktop chats: published artifacts (Claude Docs documents) appear as `📄 Artifact «name»: link` markers; the content lives in the Claude Docs service and is fetched through its connector on request (skill instruction added).

## 1.17.0 — 2026-11-03
- `create` writes native session structures: interjections as `queued_command` attachments, notes as assistant messages with `stop_reason: tool_use`, answers with `end_turn`, every subagent result as an Agent `tool_use` + `tool_result` + the subagent's own transcript file (`subagents/agent-<id>.jsonl` + `.meta.json`), compact summaries as `isCompactSummary` user entries, slash commands and user interruptions as the entries Claude Code writes for them. An imported session therefore behaves exactly like a native one in the compaction cycle and at every detail level (round trip verified on a 32-message session at all four levels).
- Parser: a finished (`end_turn`) answer directly followed by another assistant message closes the turn; native sessions extract byte-identically.

## 1.16.x — 2026-10-09 … 2026-11-03
- 1.16.3/1.16.2: `create` reports the target project's baseline context (system prompt, tools, CLAUDE.md, memory, measured from the first request of the project's other sessions) next to the import size; the Mac app shows it.
- 1.16.1: Claude Desktop 4.42 cache format: chats are `hub_transcript` records; titles, model and timestamps come from the app's persisted query cache (`chat_conversation_list`), which also holds further transcripts. Tool names with spaces ("Gmail: Send message") count as deliverables.
- 1.16.0: token sizes are **measured** with the token-counting API (CLI login) and marked `(measured)`; offline estimate calibrated to ~2 characters per token for Claude 5 models (chars/4 undercounted 2×). `CONTEXT_CYCLE_NO_API=1` disables the API call.

## 1.15.x — 2026-10-09
- 1.15.1: project paths are NFC-normalized before munging (macOS dialogs return decomposed Unicode; "ä" became "a-" and the project directory was not found).
- 1.15.0: `create <session> --into DIR` — a new Claude Code session in another project from a condensed extract; resume with `claude --resume <id>`.
- Mac app (`app/`): native SwiftUI front end — list a project's sessions, Desktop chats or cloud sessions; copy one as a new session into another project (detail level with per-level token estimates) or save its extract as Markdown. `app/build.sh --install`.

## 1.14.x — 2026-09-30
- 1.14.2/1.14.1: composed/sent messages (`message_compose_v1`, Gmail-style send/draft) and created artifacts stay inside the answer with their content at every detail level.
- 1.14.0: ordinary claude.ai **chats** from Claude Desktop's cache as a source (`--agent chat`): blob files and inline LevelDB values (`leveldb.py`), displayed branch only, notes/answers/markers.

## 1.13.0 — 2026-09-25
- Locally continued cloud sessions: the original cloud session is identified by the message/tool ids shared with the local replay and its complete history (incl. subagent reports) is used up to the takeover (`--cloud-origin`, `--no-cloud`, cache under `~/.cache/context-cycle/`).
- The size line and the checkpoint hook take the model from the running session's transcript (`CLAUDE_CODE_SESSION_ID`), not from settings.

## 1.12.x — 2026-09-25
- Test suite (synthetic sessions, structural invariants), documentation rewrite, Codex progress notes, subagent fixes; Claude 5 family models default to a 1M window.
