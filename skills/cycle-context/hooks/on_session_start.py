#!/usr/bin/env python3
"""SessionStart hook (startup, resume, clear, fork) for projects that run the
hand-over cycle (.claude/context-cycle.json, see cycle_config.py).

- Reports the hand-over file: path, stamp, last change, warnings (missing
  file, forgotten stamp line; with load "plugin" also "older than the newest
  commit"). With load "plugin" the file's content is injected as well (up to
  ~9.5k chars, else a preview plus the order to read it completely); with
  load "project" the project's own hook loads it.
- After /clear (source "clear") it OFFERS the predecessor session's history:
  title, id and size of the transcript — never imports it. The user chooses;
  then the cycle-context skill asks the detail level as usual.

Source "compact" is handled by on_compact.py; this hook exits for it. Without
the config file the hook prints nothing.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cycle_config  # noqa: E402


def transcript_summary(entry):
    path = Path(entry["transcript_path"])
    size = path.stat().st_size
    title = cycle_config.session_title({}, str(path)) or "(no custom title)"
    mb = size / 1_000_000
    return f"«{title}» (id {entry['session_id']}, transcript {mb:.1f} MB)"


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    source = hook_input.get("source") or ""
    if source == "compact":
        return
    config, handover = cycle_config.active(hook_input)
    if not config:
        return
    status = cycle_config.handover_status(config, handover)
    lines = [f"context-cycle hand-over cycle ({config['cycle']}; session start: {source or 'unknown'})."]
    lines.append(cycle_config.describe(status))
    if status["exists"] and config.get("load") == "plugin":
        text = Path(handover).read_text(encoding="utf-8", errors="replace")
        if len(text) <= cycle_config.MAX_INJECT_CHARS:
            lines.append("The hand-over (read it completely, it is your primary source of the current state):")
            lines.append("")
            lines.append(text.rstrip())
        else:
            lines.append(f"The hand-over is {len(text):,} chars, too long to inject: Read the file "
                         f"COMPLETELY (multiple Read calls) before doing anything else.")
    elif status["exists"]:
        lines.append("The project's own hook loads the hand-over; if it did not arrive, Read the file "
                     "completely before doing anything else.")
    if status["warnings"]:
        lines.append("Report the warnings above to the user first, before continuing.")
    if source == "clear":
        pred = cycle_config.predecessor(hook_input)
        if pred:
            lines.append("")
            lines.append("Previous session of this process (restarted with /clear): "
                         + transcript_summary(pred) + ". Its history is on disk as an emergency reserve "
                         "only: do NOT import it unasked. If the user asks for it, run the cycle-context "
                         "skill on that session (detail-level question as usual): "
                         f"extract --path \"{pred['transcript_path']}\".")
        else:
            lines.append("")
            lines.append("Previous session: not identified (no registry entry for this process); the "
                         "cycle-context skill's `list` finds it if the user asks for the history.")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
