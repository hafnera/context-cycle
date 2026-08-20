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
import re
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


def newest_session_of_cwd():
    """The most recently written session file of the current project (CLI use)."""
    munged = re.sub(r"[^A-Za-z0-9]", "-", str(Path.cwd().resolve()))
    project_dir = Path.home() / ".claude" / "projects" / munged
    files = sorted(project_dir.glob("*.jsonl"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


def cli_mode(mode):
    """--status: print current context usage. --mark: arm the once-marker so
    the automatic 80% reminder stays silent for the current cycle (used after
    a manual documentation checkpoint via the doc-checkpoint skill)."""
    transcript = newest_session_of_cwd()
    if transcript is None:
        print("No session file found for this project.")
        return
    tokens = current_context_tokens(str(transcript))
    window, basis = effective_window()
    if tokens is None:
        print("Could not read context usage from the session file.")
        return
    if mode == "--status":
        print(f"Context status: ~{tokens/1000:.0f}k of {window//1000}k tokens "
              f"({tokens/window*100:.0f}%) — basis: {basis}, "
              f"checkpoint threshold: {int(REMIND_FRACTION*100)}%")
    else:
        marker = Path("/tmp") / f"claude-doc-reminder-{transcript.stem}"
        marker.write_text(str(tokens))
        print(f"Marked: automatic checkpoint reminder suppressed for session "
              f"{transcript.stem[:8]} until after the next compaction.")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--status", "--mark"):
        cli_mode(sys.argv[1])
        return
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
