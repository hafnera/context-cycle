#!/usr/bin/env python3
"""SessionStart(compact) hook: re-inject condensed session history after compaction.

Claude Code compacts the context window when it fills up; details of the original
user requests and final answers can get lost in the summary. Registered as a
SessionStart hook with matcher "compact" (via this plugin's hooks/hooks.json),
this script prints the condensed transcript of the just-compacted session to
stdout, which Claude Code adds to the fresh context.

The hook input (stdin JSON) provides transcript_path, so the session file is
identified exactly — no most-recently-modified heuristics.
"""

import json
import subprocess
import sys
from pathlib import Path

# Optional bounds for the injected context. Both None: the FULL condensed
# transcript is injected. Set e.g. "15" / "2000" if that turns out to be
# too much context after compaction.
LAST_USER_TURNS = None
MAX_CHARS_PER_MESSAGE = None


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

    script = Path(__file__).resolve().parent.parent / "scripts" / "extract_session.py"
    cmd = [sys.executable or "python3", str(script), "extract"]
    if LAST_USER_TURNS:
        cmd += ["--last", LAST_USER_TURNS]
    if MAX_CHARS_PER_MESSAGE:
        cmd += ["--max-chars", MAX_CHARS_PER_MESSAGE]

    transcript_path = hook_input.get("transcript_path")
    if transcript_path and Path(transcript_path).is_file():
        cmd += ["--path", transcript_path]
    else:
        cmd += ["--current", "--project", hook_input.get("cwd") or str(Path.cwd())]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0 or not result.stdout.strip():
        # Never block the session over a failed context refresh.
        sys.exit(0)

    print("The context was just compacted. Below is the condensed transcript of "
          "this session (user messages and final answers) recovered from the "
          "session file on disk, so specifics of the original requests are not "
          "lost. It supplements the compact summary above; the summary wins on "
          "current state, this transcript wins on original wording.\n")
    print(result.stdout)
    size_line = (result.stderr or "").strip().splitlines()
    if size_line and size_line[-1].startswith("Imported context:"):
        print(f"\n({size_line[-1]})")
    print("\nIMPORTANT: Before continuing, re-read ALL documentation of this "
          "project — README, CLAUDE.md, docs/, skill files and any "
          "knowledge-base notes — to rebuild a complete understanding of the "
          "project. Then continue the task using the recovered context above. "
          "Also tell the user how much context was just re-injected (the "
          "\"Imported context\" line above).")


if __name__ == "__main__":
    main()
