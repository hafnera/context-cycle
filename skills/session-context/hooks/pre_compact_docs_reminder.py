#!/usr/bin/env python3
"""PostToolUse hook: tell the agent to update all docs BEFORE auto-compaction.

There is no hook event for "context reached N tokens", and PreCompact fires
when compaction is already underway (its output is not fed to the model, and
there is no token headroom left to do work). So this hook approximates
"shortly before compact": after every tool call it reads the current context
size from the session transcript; once it crosses REMIND_FRACTION of the
effective auto-compact window, it injects a one-time instruction to bring all
project documentation up to date now, so nothing undocumented is lost in the
upcoming compaction.

Registered as a PostToolUse hook (no matcher = all tools) via this plugin's
hooks/hooks.json. A marker file in /tmp makes the reminder fire once per
approach to the threshold; after compaction shrinks the context, the marker
re-arms automatically.

Tuning: REMIND_FRACTION below (or env DOC_REMINDER_FRACTION). The effective
window is autoCompactWindow from ~/.claude/settings.json if set, else 1M for
"[1m]" models, else 200k.
"""

import json
import os
import sys
from pathlib import Path

REMIND_FRACTION = float(os.environ.get("DOC_REMINDER_FRACTION", "0.8"))
# Context dropping below RESET_FRACTION * threshold means compaction happened.
RESET_FRACTION = 0.6

REMINDER = (
    "Context checkpoint: this session is at ~{tokens_k}k of the {window_k}k-token "
    "{basis} ({pct}%) — auto-compaction is approaching. BEFORE continuing with "
    "the current task, do a documentation checkpoint NOW:\n"
    "1. Bring ALL project documentation up to date — README, CLAUDE.md, docs/, "
    "skill files, knowledge-base notes, AND architecture documentation "
    "(system design, component relationships, data flows, key decisions with "
    "their rationale).\n"
    "2. Persist LEARNINGS FROM MISTAKES made in this session: what went wrong, "
    "the root cause, and the rule to prevent repeating it — written into the "
    "project's docs/knowledge base so future sessions do not repeat these "
    "errors.\n"
    "3. Finish or checkpoint any half-done edits.\n"
    "When the checkpoint is complete, do NOT continue with the task. Instead, "
    "END your turn with a final message to the user that contains:\n"
    "- a short confirmation of the documentation checkpoint (what was updated),\n"
    "- a condensed summary of the RELEVANT findings from the result of the "
    "last tool call (the one that triggered this reminder) — key facts only, "
    "not the full output; tool results do not survive compaction, so anything "
    "you still need from it must be captured here,\n"
    "- a precise, numbered list of the NEXT STEPS you had planned, concrete "
    "enough that you could resume from this list alone — the post-compact "
    "context restore keeps user messages and final answers, so exactly this "
    "message will land back in your context after compaction,\n"
    "- the explicit request that the user should now run /compact.\n"
    "You cannot trigger /compact yourself — do not try. Wait for the user."
)


def effective_window():
    """Returns (window_tokens, basis_label). autoCompactWindow wins when set."""
    try:
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
    except (OSError, json.JSONDecodeError):
        settings = {}
    if isinstance(settings.get("autoCompactWindow"), int):
        return settings["autoCompactWindow"], "autoCompactWindow"
    model = settings.get("model") or ""
    window = 1_000_000 if "[1m]" in model else 200_000
    return window, "model context window"


def current_context_tokens(transcript_path):
    """Context size per the newest assistant entry's usage block (tail read)."""
    path = Path(transcript_path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - 262_144))
        tail = f.read().decode("utf-8", errors="replace")
    for line in reversed(tail.splitlines()):
        if '"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # likely the partial first line of the tail window
        usage = ((entry.get("message") or {}).get("usage")) or {}
        if "input_tokens" in usage:
            return (usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("output_tokens", 0))
    return None


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    transcript_path = hook_input.get("transcript_path")
    if not transcript_path or not Path(transcript_path).is_file():
        return
    tokens = current_context_tokens(transcript_path)
    if tokens is None:
        return
    window, basis = effective_window()
    threshold = int(window * REMIND_FRACTION)
    session = hook_input.get("session_id") or Path(transcript_path).stem
    marker = Path("/tmp") / f"claude-doc-reminder-{session}"

    if tokens < threshold:
        if marker.exists() and tokens < threshold * RESET_FRACTION:
            marker.unlink(missing_ok=True)  # compaction happened — re-arm
        return
    if marker.exists():
        return  # already reminded for this approach to the threshold
    marker.write_text(str(tokens))
    message = REMINDER.format(tokens_k=round(tokens / 1000), window_k=window // 1000,
                              basis=basis, pct=round(tokens / window * 100))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                             "additionalContext": message}}))


if __name__ == "__main__":
    main()
