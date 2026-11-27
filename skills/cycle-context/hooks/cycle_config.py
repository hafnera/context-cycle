#!/usr/bin/env python3
"""Per-project hand-over cycle configuration for the context-cycle hooks.

A project switches the hand-over cycle on with `.claude/context-cycle.json`
in the working directory (the worktree the session runs in):

    {
      "cycle": "clear",                       # or "compact" (default)
      "handover": "docs/roles/{dir}.md",      # path relative to cwd; {dir} = cwd
                                              # basename, {title} = session title slug
      "sessions": {"KAI - DMS": "../Kai-dms/docs/roles/dms.md"},
                                              # map by exact session title (several
                                              # sessions often share one cwd); wins
      "handovers": {"kai-portale": "docs/roles/portale.md", "*": "docs/roles/{dir}.md"},
                                              # map by cwd basename or session title;
                                              # "*" is the fallback; wins over "handover"
      "load": "plugin",                       # "plugin": the SessionStart hook injects the
                                              # hand-over file itself; "project": the
                                              # project's own hook loads it, the plugin
                                              # only reports path, stamp and warnings
      "stamp": "^Stand: (\\\\d{2}\\\\.\\\\d{2}\\\\.\\\\d{4} \\\\d{2}:\\\\d{2})"
                                              # optional regex with one group holding the
                                              # hand-over's stamp (date[ time]); without a
                                              # match the file's mtime counts
    }

Without this file every hook of the plugin behaves exactly as before; the
functions here then return None and the hooks exit silently.

Also here: the registry that lets the SessionStart hook after `/clear` find
the predecessor session — `/clear` keeps the Claude Code process, so the
PostToolUse hook records {session, transcript} under the process id of the
Claude Code process that spawned it, and the SessionStart hook reads the
entry of its own.
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

CONFIG_NAME = ".claude/context-cycle.json"
DEFAULT_STAMP = r"^Stand: (\d{2}\.\d{2}\.\d{4}(?: \d{2}:\d{2})?)"
STAMP_FORMATS = ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M")
REGISTRY_DIR = Path(os.environ.get("CONTEXT_CYCLE_REGISTRY") or (Path.home() / ".cache" / "context-cycle" / "pids"))
TITLE_CACHE_DIR = REGISTRY_DIR.parent / "titles"
MAX_INJECT_CHARS = 9_500  # Claude Code replaces hook output above ~10k chars with a preview


# ---------------------------------------------------------------- config

def load_config(cwd):
    """The project's hand-over configuration, or None when the cycle is off."""
    if not cwd:
        return None
    path = Path(cwd) / CONFIG_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cycle = str(data.get("cycle") or "compact").lower()
    data["cycle"] = "clear" if cycle == "clear" else "compact"
    data["load"] = "project" if str(data.get("load") or "plugin").lower() == "project" else "plugin"
    data["_cwd"] = str(cwd)
    return data


def slug(text):
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "session"


def session_title(hook_input, transcript_path=None):
    """The session's custom title: from the hook input when present (SessionStart,
    UserPromptSubmit), else the last custom-title entry of the transcript. The
    transcript scan is incremental (PostToolUse and Stop fire often, transcripts
    reach hundreds of MB): a cache per transcript keeps the title and the offset
    already scanned, so each call reads only what was appended since."""
    title = hook_input.get("session_title") if hook_input else None
    if title:
        return str(title)
    path = transcript_path or (hook_input or {}).get("transcript_path")
    if not path or not Path(path).is_file():
        return None
    size = Path(path).stat().st_size
    cache_file = TITLE_CACHE_DIR / (Path(path).stem + ".json")
    cached = {"title": None, "offset": 0}
    try:
        data = json.loads(cache_file.read_text())
        if isinstance(data, dict) and data.get("offset", 0) <= size:
            cached = data
    except (OSError, ValueError):
        pass
    if cached["offset"] >= size:
        return cached["title"]
    last = cached["title"]
    try:
        with open(path, "rb") as f:
            if cached["offset"]:
                f.seek(cached["offset"] - 1)
                if f.read(1) != b"\n":
                    f.readline()  # the offset sat inside a line (file was mid-write): skip its rest
            else:
                f.seek(0)
            for raw in f:
                if b'"customTitle"' not in raw:
                    continue
                try:
                    entry = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if entry.get("customTitle"):
                    last = str(entry["customTitle"])
    except OSError:
        return last
    try:
        TITLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"title": last, "offset": size}))
    except OSError:
        pass
    return last


def handover_path(config, hook_input=None, title=None):
    """Absolute path of the hand-over file for this cwd/session, or None."""
    if not config:
        return None
    cwd = Path(config["_cwd"])
    dir_name = cwd.name
    if title is None:
        title = session_title(hook_input or {})
    template = None
    sessions = config.get("sessions")   # by exact session title: several sessions share one cwd
    if isinstance(sessions, dict) and title and sessions.get(title):
        template = str(sessions[title])
    handovers = config.get("handovers")
    if template is None and isinstance(handovers, dict):
        for key in (dir_name, title, "*"):
            if key and key in handovers and handovers[key]:
                template = str(handovers[key])
                break
    if template is None and config.get("handover"):
        template = str(config["handover"])
    if not template:
        return None
    rel = template.replace("{dir}", dir_name).replace("{title}", slug(title))
    return (cwd / rel).resolve() if not Path(rel).is_absolute() else Path(rel)


# ---------------------------------------------------------------- hand-over state

def stamp_time(config, path):
    """The datetime of the hand-over's stamp line, or None."""
    pattern = config.get("stamp") or DEFAULT_STAMP
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        m = re.search(pattern, text, re.M)
    except re.error:
        m = re.search(DEFAULT_STAMP, text, re.M)
    if not m:
        return None
    value = (m.group(1) if m.groups() else m.group(0)).strip()
    for fmt in STAMP_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def active(hook_input, title=None):
    """(config, hand-over path) when the cycle is on for this cwd/session, else
    (None, None): a config without a matching hand-over entry for the working
    directory leaves the plugin's behaviour exactly as without the file."""
    config = load_config((hook_input or {}).get("cwd"))
    if not config:
        return None, None
    path = handover_path(config, hook_input, title)
    if not path:
        return None, None
    return config, path


def handover_status(config, path):
    """Facts about the hand-over file: exists, mtime, stamp, git staleness.
    The git comparison is skipped with load "project": there the project's own
    hook judges staleness (in a role worktree HEAD is newer after every merge
    from main, so a plain HEAD comparison would warn constantly)."""
    status = {"path": str(path), "exists": False, "mtime": None, "stamp": None,
              "head_commit": None, "head_time": None, "file_commit": None, "file_time": None,
              "uncommitted": False, "warnings": []}
    if not path or not Path(path).is_file():
        status["warnings"].append(f"hand-over file missing: {path}")
        return status
    status["exists"] = True
    status["mtime"] = datetime.fromtimestamp(Path(path).stat().st_mtime)
    status["stamp"] = stamp_time(config, path)
    if status["stamp"] is None:
        status["warnings"].append("no stamp line found in the hand-over (its mtime counts instead)")
    elif (status["mtime"] - status["stamp"]).total_seconds() > 86_400:
        status["warnings"].append(
            f"the hand-over's stamp line ({status['stamp']:%Y-%m-%d %H:%M}) is more than a day older than "
            f"the file's last change ({status['mtime']:%Y-%m-%d %H:%M}): the stamp was not updated")
    cwd = config["_cwd"]
    if config.get("load") == "project":
        return status

    def git(*args):
        try:
            r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    head = git("log", "-1", "--format=%h %ct")
    if head:
        h_hash, h_ts = head.split()
        status["head_commit"], status["head_time"] = h_hash, datetime.fromtimestamp(int(h_ts))
        rel = os.path.relpath(str(path), cwd)
        last = git("log", "-1", "--format=%h %ct", "--", rel)
        if last:
            f_hash, f_ts = last.split()
            status["file_commit"], status["file_time"] = f_hash, datetime.fromtimestamp(int(f_ts))
            if int(h_ts) > int(f_ts):
                status["warnings"].append(
                    f"the hand-over is older than the newest commit of this working directory: last "
                    f"committed in {f_hash} ({status['file_time']:%Y-%m-%d %H:%M}), HEAD is {h_hash} "
                    f"({status['head_time']:%Y-%m-%d %H:%M})")
        else:
            status["warnings"].append("the hand-over has never been committed")
        porcelain = git("status", "--porcelain", "--", rel)
        if porcelain:
            status["uncommitted"] = True
            status["warnings"].append("the hand-over has uncommitted changes")
    return status


def rewritten_since(config, path, since_ts):
    """True when the hand-over was written after the unix time `since_ts`
    (file mtime, or a stamp line at least that new)."""
    if not path or not Path(path).is_file():
        return False
    if Path(path).stat().st_mtime > since_ts:
        return True
    stamp = stamp_time(config, path)
    return bool(stamp and stamp.timestamp() >= since_ts - 90)


def describe(status, max_chars=0):
    """Short status lines for an injection."""
    lines = [f"Hand-over file: {status['path']}"]
    if status["exists"]:
        parts = []
        if status["stamp"]:
            parts.append(f"stamp {status['stamp']:%Y-%m-%d %H:%M}")
        if status["mtime"]:
            parts.append(f"last change {status['mtime']:%Y-%m-%d %H:%M}")
        if status["file_commit"]:
            parts.append(f"committed in {status['file_commit']}")
        lines.append("  " + ", ".join(parts))
    for w in status["warnings"]:
        lines.append(f"  WARNING: {w}")
    return "\n".join(lines)


# ---------------------------------------------------------------- process registry

def _proc(pid, field):
    try:
        r = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


SHELLS = {"sh", "bash", "zsh", "dash", "fish", "python", "python3", "python3.13", "python3.12", "python3.11"}


def claude_pid():
    """The pid of the Claude Code process that spawned this hook: the first
    ancestor that is neither a shell nor the Python interpreter."""
    if os.environ.get("CONTEXT_CYCLE_CLAUDE_PID", "").isdigit():
        return int(os.environ["CONTEXT_CYCLE_CLAUDE_PID"])  # tests
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            return None
        comm = os.path.basename(_proc(pid, "comm")).lower()
        if comm and comm not in SHELLS and not comm.startswith("python"):
            return pid
        parent = _proc(pid, "ppid")
        if not parent.isdigit():
            return None
        pid = int(parent)
    return None


def record_session(hook_input):
    """PostToolUse: remember this session under its Claude Code process id."""
    pid = claude_pid()
    if not pid or not hook_input.get("session_id"):
        return
    try:
        REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        path = REGISTRY_DIR / f"{pid}.json"
        current = None
        try:
            current = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        if current and current.get("session_id") == hook_input["session_id"] and time.time() - current.get("ts", 0) < 300:
            return  # fresh enough; avoid a write per tool call
        path.write_text(json.dumps({"session_id": hook_input["session_id"],
                                    "transcript_path": hook_input.get("transcript_path"),
                                    "cwd": hook_input.get("cwd"), "ts": time.time()}))
        # housekeeping: drop entries older than a week
        now = time.time()
        for old in REGISTRY_DIR.glob("*.json"):
            try:
                if now - old.stat().st_mtime > 7 * 86_400:
                    old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def predecessor(hook_input):
    """SessionStart after /clear: the session this process ran before, or None."""
    pid = claude_pid()
    entry = None
    if pid:
        try:
            entry = json.loads((REGISTRY_DIR / f"{pid}.json").read_text())
        except (OSError, ValueError):
            entry = None
    if entry and entry.get("session_id") != hook_input.get("session_id") \
            and entry.get("transcript_path") and Path(entry["transcript_path"]).is_file():
        return entry
    # fallback: the newest transcript of this project with another id
    cwd = hook_input.get("cwd")
    if not cwd:
        return None
    munged = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    project_dir = Path.home() / ".claude" / "projects" / munged
    files = sorted(project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files:
        if f.stem != hook_input.get("session_id") and time.time() - f.stat().st_mtime < 6 * 3600:
            return {"session_id": f.stem, "transcript_path": str(f), "cwd": cwd, "ts": f.stat().st_mtime}
    return None
