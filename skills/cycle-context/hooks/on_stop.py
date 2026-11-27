#!/usr/bin/env python3
"""Stop hook: in a project that runs the hand-over cycle, make sure the
checkpoint really rewrote the hand-over file before the turn ends.

The PostToolUse reminder (pre_compact_docs_reminder.py) orders the hand-over
at 80% context and records the time in the session's marker file. When the
agent ends its turn afterwards — the moment it asks the user for /clear or
/compact — this hook compares the hand-over file against that time (mtime,
or a stamp line at least that new). If the file was not rewritten, the turn
continues with a feedback message, at most twice per cycle. Without
.claude/context-cycle.json, or outside a checkpoint, the hook exits silently.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cycle_config  # noqa: E402

MAX_NAGS = 2
FEEDBACK = (
    "Hand-over check (context-cycle): the hand-over file {handover} was NOT rewritten since the "
    "context checkpoint was ordered ({ordered}). Before ending your turn, write it completely "
    "following its template (running task and exact state, agreements with other sessions, what "
    "lies with the user, numbered next steps, fresh pitfalls), update its stamp line and commit "
    "it together with the work. Then end your turn with the checkpoint message and the request "
    "to run {command}."
)


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    config, handover = cycle_config.active(hook_input)
    if not config:
        return
    session = hook_input.get("session_id") or Path(hook_input.get("transcript_path") or "x").stem
    marker = Path("/tmp") / f"claude-doc-reminder-{session}"
    try:
        state = json.loads(marker.read_text())
    except (OSError, ValueError):
        return  # no checkpoint ordered in this cycle (or the plain marker of the classic cycle)
    if not isinstance(state, dict) or not state.get("ts"):
        return
    if time.time() - state["ts"] > 86_400:
        return  # a stale order; never nag a day later
    if cycle_config.rewritten_since(config, handover, state["ts"]):
        return
    if state.get("nags", 0) >= MAX_NAGS:
        return
    state["nags"] = state.get("nags", 0) + 1
    try:
        marker.write_text(json.dumps(state))
    except OSError:
        pass
    command = "/clear" if state.get("cycle", config["cycle"]) == "clear" else "/compact"
    ordered = time.strftime("%Y-%m-%d %H:%M", time.localtime(state["ts"]))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": FEEDBACK.format(
        handover=handover, ordered=ordered, command=command)}}))


if __name__ == "__main__":
    main()
