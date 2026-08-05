#!/usr/bin/env python3
"""SessionStart(compact) hook: re-inject condensed session history after compaction.

Claude Code compacts the context window when it fills up; details of the original
user requests and final answers can get lost in the summary. Registered as a
SessionStart hook with matcher "compact" (via this plugin's hooks/hooks.json),
this script prints the condensed transcript of the just-compacted session to
stdout, which Claude Code adds to the fresh context.

Chunked injection: Claude Code silently replaces any single hook output larger
than ~10-12k characters with a 2KB preview plus a file path the agent would
have to Read itself (usually incompletely). The limit is per hook command, so
hooks.json registers this script N times as `--part i --parts N`; every part
extracts the same transcript deterministically and prints only its own
<= ~9k-char slice, labeled "part i/M". Parts may arrive out of order (hooks
run in parallel), hence the labels plus a small stagger sleep. Only if the
transcript exceeds all N slots does the tail go to a file — with an explicit
instruction to read it COMPLETELY.

The hook input (stdin JSON) provides transcript_path, so the session file is
identified exactly — no most-recently-modified heuristics. Run without
arguments to print the full text in one piece (manual/debug use).
"""

import json
import subprocess
import sys
import time
from pathlib import Path

# Optional bounds for the injected context. Both None: the FULL condensed
# transcript is injected. Set e.g. "15" / "2000" if that turns out to be
# too much context after compaction.
LAST_USER_TURNS = None
MAX_CHARS_PER_MESSAGE = None

# Keep each injected part safely below the empirically measured persist
# threshold (~10-12k chars incl. our part header).
CHUNK_CHARS = 9_000
# Stagger printing so parts tend to arrive in order (they run in parallel).
STAGGER_SECONDS = 0.15


def build_full_text(hook_input):
    """The complete injection text: preamble + condensed transcript + orders."""
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

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or not result.stdout.strip():
        return None

    parts = ["The context was just compacted. Below is the condensed transcript of "
             "this session (user messages and final answers) recovered from the "
             "session file on disk, so specifics of the original requests are not "
             "lost. It supplements the compact summary; the summary wins on "
             "current state, this transcript wins on original wording.\n"]
    parts.append(result.stdout)
    size_lines = (result.stderr or "").strip().splitlines()
    if size_lines and size_lines[-1].startswith("Imported context:"):
        parts.append(f"\n({size_lines[-1]})")
    parts.append("\nIMPORTANT: Before continuing, re-read ALL documentation of this "
                 "project — README, CLAUDE.md, docs/, skill files and any "
                 "knowledge-base notes — to rebuild a complete understanding of the "
                 "project. Then continue the task using the recovered context above. "
                 "Also tell the user how much context was just re-injected (the "
                 "\"Imported context\" line above).")
    return "\n".join(parts)


def pack_chunks(text):
    """Split at line boundaries into chunks of <= CHUNK_CHARS."""
    chunks, current, current_len = [], [], 0
    for line in text.splitlines(keepends=True):
        # A single overlong line is hard-split.
        while len(line) > CHUNK_CHARS:
            if current:
                chunks.append("".join(current))
                current, current_len = [], 0
            chunks.append(line[:CHUNK_CHARS])
            line = line[CHUNK_CHARS:]
        if current_len + len(line) > CHUNK_CHARS and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def emit(slot, chunk_idx, total_chunks, body):
    header = (f"[Recovered session context — part {chunk_idx}/{total_chunks}"
              + (" — parts may arrive out of order; reassemble by part number]"
                 if total_chunks > 1 else "]"))
    time.sleep(STAGGER_SECONDS * (slot - 1))
    sys.stdout.write(header + "\n" + body)


def main():
    part = parts = None
    args = sys.argv[1:]
    if "--part" in args and "--parts" in args:
        part = int(args[args.index("--part") + 1])
        parts = int(args[args.index("--parts") + 1])

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

    full = build_full_text(hook_input)
    if full is None:
        sys.exit(0)  # never block the session over a failed context refresh

    if part is None:  # manual/debug: everything in one piece
        print(full)
        return

    chunks = pack_chunks(full)
    total = len(chunks)

    if total <= parts:
        if part <= total:
            emit(part, part, total, chunks[part - 1])
        return

    # Overflow: more chunks than hook slots. The MOST RECENT content always
    # gets injected directly (chronological, newest last): slot 1 becomes an
    # overflow notice, slots 2..N carry the last N-1 chunks in order; the
    # oldest chunks go to a file with a read-completely instruction.
    kept = parts - 1
    if part == 1:
        session = hook_input.get("session_id") or "unknown"
        head_file = Path("/tmp") / f"claude-session-restore-{session}.md"
        head = "".join(chunks[:total - kept])
        head_file.write_text(head, encoding="utf-8")
        print(f"[Recovered session context — OVERFLOW NOTICE]\n"
              f"The context was just compacted and the condensed transcript of "
              f"this session (user messages + final answers) was recovered from "
              f"disk, but it is too large for full direct injection "
              f"({len(full):,} chars = {total} parts, {parts} slots). The MOST "
              f"RECENT portion follows below as parts {total - kept + 1}..{total} "
              f"(chronological, newest last). The OLDEST portion (parts "
              f"1..{total - kept}, {len(head):,} chars — including the session's "
              f"original request) was saved to: {head_file}\n"
              f"MANDATORY: Read that file COMPLETELY as well (multiple Read "
              f"calls if needed — do not skip or sample lines), so you know the "
              f"full history before continuing.")
        return
    chunk_idx = total - parts + part  # slot 2 -> oldest kept … slot N -> newest
    emit(part, chunk_idx, total, chunks[chunk_idx - 1])


if __name__ == "__main__":
    main()
