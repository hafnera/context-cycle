"""Structural tests for the context-cycle extractor and hooks (stdlib unittest).

Run:  python3 -m unittest discover -s tests -v

The tests build small synthetic session files that contain every special case
the extractor has to handle (tool calls, progress notes, interjections,
rewinds, duplicate uuids, sidechains, Agent-tool subagents with a resumed
invocation, Workflow runs, background tasks, slash commands, compact
summaries, Codex rollouts, Desktop-cache V8/Snappy blobs) and check the
rendered structure and the invariants that must hold across detail levels.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "cycle-context" / "scripts"
HOOKS = ROOT / "skills" / "cycle-context" / "hooks"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HOOKS))

import extract_session as ex  # noqa: E402
import v8idb  # noqa: E402


# --------------------------------------------------------------------------
# synthetic session builder
# --------------------------------------------------------------------------

def _e(kind, uuid, parent, ts, **extra):
    d = {"type": kind, "uuid": uuid, "parentUuid": parent, "timestamp": ts,
         "sessionId": "sess", "cwd": "/tmp/proj", "version": "2.1"}
    d.update(extra)
    return d


def user(uuid, parent, ts, text, **extra):
    return _e("user", uuid, parent, ts, message={"role": "user", "content": text}, **extra)


def assistant(uuid, parent, ts, blocks, mid="msg", stop="end_turn", **extra):
    return _e("assistant", uuid, parent, ts,
              message={"id": mid, "role": "assistant", "model": "claude-x", "content": blocks,
                       "stop_reason": stop, "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                                                       "cache_creation_input_tokens": 0, "output_tokens": 5}},
              **extra)


def tool_result(uuid, parent, ts, tool_id, text):
    return _e("user", uuid, parent, ts, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": text}]})


T = "2026-01-01T10:%02d:00.000Z"


def build_claude_session(tmp):
    """One project dir with a session that exercises the special cases.
    Returns the jsonl path."""
    proj = tmp / ".claude" / "projects" / "-tmp-proj"
    proj.mkdir(parents=True)
    sid = "11111111-aaaa-bbbb-cccc-000000000001"
    f = proj / f"{sid}.jsonl"
    rows = []
    # turn 1: user -> note+tool -> tool result -> note -> final
    rows.append(user("u1", None, T % 0, "Build the feature please"))
    rows.append(assistant("a1", "u1", T % 1, [{"type": "text", "text": "Looking at the repo first."},
                                             {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}],
                          mid="m1", stop="tool_use"))
    rows.append(tool_result("r1", "a1", T % 2, "toolu_1", "ok"))
    rows.append(assistant("a2", "r1", T % 3, [{"type": "thinking", "thinking": "hidden"},
                                             {"type": "text", "text": "Now writing the code."},
                                             {"type": "tool_use", "id": "toolu_2", "name": "Edit", "input": {}}],
                          mid="m2", stop="tool_use"))
    # user interjection while the agent works (queued_command attachment)
    rows.append({"type": "attachment", "uuid": "q1", "parentUuid": "a2", "timestamp": T % 4,
                 "attachment": {"type": "queued_command", "prompt": "please also add tests"}})
    rows.append(tool_result("r2", "a2", T % 5, "toolu_2", "ok"))
    rows.append(assistant("a3", "r2", T % 6, [{"type": "text", "text": "Done: feature built and tests added."}],
                          mid="m3"))
    # a slash command and a hook-triggered follow-up
    rows.append(user("c1", "a3", T % 7, "<command-name>/model</command-name><command-args>opus</command-args>"))
    rows.append(user("h1", "c1", T % 8, "Stop hook feedback: run /self-improve", isMeta=True))
    rows.append(assistant("a4", "h1", T % 9, [{"type": "text", "text": "Self-improve done."}], mid="m4"))
    # turn 2 with an Agent-tool subagent (sync) that is later resumed (task notification)
    rows.append(user("u2", "a4", T % 10, "Research the API"))
    rows.append(assistant("a5", "u2", T % 11, [{"type": "text", "text": "Delegating to a researcher."},
                                              {"type": "tool_use", "id": "toolu_ag", "name": "Agent",
                                               "input": {"description": "Research API docs",
                                                         "subagent_type": "Explore", "prompt": "..."}}],
                          mid="m5", stop="tool_use"))
    rows.append(tool_result("r3", "a5", T % 12, "toolu_ag", "First short answer from the researcher."))
    rows.append(assistant("a6", "r3", T % 13, [{"type": "text", "text": "Asking a follow-up."},
                                              {"type": "tool_use", "id": "toolu_sm", "name": "SendMessage",
                                               "input": {"to": "agentX", "message": "follow-up"}}],
                          mid="m6", stop="tool_use"))
    rows.append(tool_result("r4", "a6", T % 14, "toolu_sm",
                            '{"success":true,"message":"Agent \\"agentX\\" resumed"}'))
    rows.append(user("n1", "r4", T % 15,
                     "<task-notification><task-id>agentX</task-id><tool-use-id>toolu_sm</tool-use-id>"
                     "<status>completed</status><summary>Agent finished</summary></task-notification>"))
    rows.append(assistant("a7", "n1", T % 16, [{"type": "text", "text": "Research complete, summary follows."}],
                          mid="m7"))
    # rewind fork: two real user messages under the same parent; the later wins
    rows.append(user("u3a", "a7", T % 17, "Deploy it to prod now"))
    rows.append(assistant("a8a", "u3a", T % 18, [{"type": "text", "text": "Deploying to prod (abandoned branch)."}],
                          mid="m8a"))
    rows.append(user("u3b", "a7", T % 19, "Actually, deploy to staging only"))
    rows.append(assistant("a8b", "u3b", T % 20, [{"type": "text", "text": "Deployed to staging."}], mid="m8b"))
    # duplicate uuid (resume re-append) and a sidechain entry
    rows.append(user("u3b", "a7", T % 19, "Actually, deploy to staging only"))
    rows.append(assistant("s1", None, T % 21, [{"type": "text", "text": "sidechain text"}], isSidechain=True,
                          agentId="agentX"))
    # background bash task notification (must not render) and a Workflow run
    rows.append(user("n2", "a8b", T % 22,
                     "<task-notification><task-id>bg1</task-id><tool-use-id>toolu_bash</tool-use-id>"
                     "<summary>Background command \"npm test\" completed</summary></task-notification>"))
    rows.append(user("u4", "a8b", T % 23, "Run the review workflow"))
    wf_dir = proj / sid / "subagents" / "workflows" / "wf_test-000"
    rows.append(assistant("a9", "u4", T % 24, [{"type": "tool_use", "id": "toolu_wf", "name": "Workflow",
                                               "input": {"script": "..."}}], mid="m9", stop="tool_use"))
    rows.append(tool_result("r5", "a9", T % 25, "toolu_wf",
                            f"Workflow launched in background. Task ID: wfx Summary: Review the code "
                            f"Transcript dir: {wf_dir} Script file: x"))
    rows.append(user("n3", "r5", T % 26,
                     "<task-notification><task-id>wfx</task-id><tool-use-id>toolu_wf</tool-use-id>"
                     "<output-file>/nonexistent/wfx.output</output-file><status>completed</status>"
                     "<summary>Dynamic workflow done</summary>\n<result>{\"confirmed\":[{\"title\":\"Bug A\","
                     "\"severity\":\"major\"}],\"rejected\":[]}</result>\n<diagnostics>ignore me</diagnostics>"
                     "<failures>[review:b] failed: limit</failures></task-notification>"))
    rows.append(assistant("a10", "n3", T % 27, [{"type": "text", "text": "Review workflow finished."}], mid="m10"))
    rows.append({"type": "ai-title", "aiTitle": "Synthetic test session", "timestamp": T % 28})
    with open(f, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    # subagent transcript (Agent tool) with two invocation segments
    sub = proj / sid / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-agentX.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "Research API docs", "toolUseId": "toolu_ag"}))
    srows = [
        user("s-u1", None, T % 11, "Research the API docs", isSidechain=True, agentId="agentX"),
        assistant("s-a1", "s-u1", T % 12, [{"type": "text", "text": "First short answer from the researcher."}],
                  mid="sm1", isSidechain=True, agentId="agentX"),
        user("s-u2", "s-a1", T % 14, "The coordinator sent a message while you were working: follow-up",
             isMeta=True, isSidechain=True, agentId="agentX"),
        assistant("s-a2", "s-u2", T % 15, [{"type": "text", "text": "Second, much longer detailed report. " * 20}],
                  mid="sm2", isSidechain=True, agentId="agentX"),
    ]
    with open(sub / "agent-agentX.jsonl", "w") as fh:
        for r in srows:
            fh.write(json.dumps(r) + "\n")
    # workflow agents: one with StructuredOutput, one description-less
    wf_dir.mkdir(parents=True)
    (wf_dir / "agent-w1.meta.json").write_text(json.dumps(
        {"agentType": "workflow-subagent", "description": "review:a", "workflowPhase": "Review"}))
    with open(wf_dir / "agent-w1.jsonl", "w") as fh:
        fh.write(json.dumps(user("w-u", None, T % 25, "Review lens A")) + "\n")
        fh.write(json.dumps(assistant("w-a", "w-u", T % 26, [
            {"type": "text", "text": "Reviewing."},
            {"type": "tool_use", "id": "toolu_so", "name": "StructuredOutput",
             "input": {"findings": [{"title": "Bug A", "severity": "major"}], "notes": "x"}}],
            mid="wm1", stop="tool_use")) + "\n")
    with open(wf_dir / "agent-w2.jsonl", "w") as fh:
        fh.write(json.dumps(user("w2-u", None, T % 25, "You are reviewer B. Check the tests.")) + "\n")
        fh.write(json.dumps(assistant("w2-a", "w2-u", T % 26, [{"type": "text", "text": "Reviewer B report."}],
                                      mid="wm2")) + "\n")
    return f


class ExtractorStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.path = build_claude_session(cls.tmp)
        cls.parsed = ex.parse_claude_session(cls.path)
        cls.md = ex.render_markdown(cls.parsed)

    def roles(self):
        return [t["role"] for t in self.parsed["turns"]]

    def test_user_messages_and_interjection(self):
        users = [t for t in self.parsed["turns"] if t["role"] == "user"]
        self.assertEqual([u["text"] for u in users],
                         ["Build the feature please", "Research the API",
                          "Actually, deploy to staging only", "Run the review workflow"])
        inter = [t for t in self.parsed["turns"] if t["role"] == "user_interjection"]
        self.assertEqual([i["text"] for i in inter], ["please also add tests"])

    def test_rewind_branch_dropped_and_duplicates_removed(self):
        texts = " ".join(t.get("text", "") for t in self.parsed["turns"])
        self.assertNotIn("abandoned branch", texts)
        self.assertNotIn("Deploy it to prod now", texts)
        self.assertEqual(texts.count("Actually, deploy to staging only"), 1)

    def test_noise_is_dropped(self):
        for bad in ("hidden", "sidechain text", "npm test", "<task-notification>", "toolu_1", "ignore me"):
            self.assertNotIn(bad, self.md, bad)

    def test_progress_notes_and_final_answers(self):
        self.assertRegex(self.md, r"🔹 \*\*Agent note \(\d\d:\d\d\):\*\* Looking at the repo first\.")
        self.assertIn("#### ✅ MAIN AGENT — FINAL ANSWER\n\nDone: feature built and tests added.", self.md)
        self.assertEqual(self.md.count("#### ✅ MAIN AGENT — FINAL ANSWER"), self.md.count("## 👤 USER MESSAGE")
                         + 1)  # +1: the hook-triggered continuation "Self-improve done."

    def test_markers(self):
        self.assertIn("*⌘ User ran:* `/model opus`", self.md)
        self.assertIn("*⚙ [hook: Stop hook feedback: run /self-improve]*", self.md)

    def test_subagent_segments_and_names(self):
        subs = [t for t in self.parsed["turns"] if t["role"] == "subagent"]
        names = [s["name"] for s in subs]
        self.assertEqual(names[:2], ["Research API docs (Explore)", "Research API docs (Explore)"])
        # first invocation: the received text IS the report -> shown once, no separate summary
        self.assertEqual(subs[0]["summary"], "")
        self.assertIn("First short answer", subs[0]["report"])
        # second invocation (resumed): the long report of segment 2
        self.assertIn("Second, much longer detailed report", subs[1]["report"] or subs[1]["summary"])
        self.assertNotIn("First short answer", subs[1]["report"] or "")

    def test_workflow_block(self):
        wf = [t for t in self.parsed["turns"] if t["role"] == "subagent" and t["name"].startswith("Workflow:")]
        self.assertEqual(len(wf), 1)
        self.assertIn("- **confirmed:**", wf[0]["summary"])          # JSON rendered as markdown
        self.assertIn("**Failed workflow agents:**", wf[0]["summary"])
        self.assertIn("Workflow agent «review:a» — phase Review", wf[0]["report"])
        self.assertIn("- **findings:**", wf[0]["report"])              # StructuredOutput as report
        self.assertIn("«You are reviewer B. Check the tests.»", wf[0]["report"])  # description-less name

    def test_per_turn_order_user_subagents_main_agent(self):
        blocks = re.split(r"^## 👤 USER MESSAGE", self.md, flags=re.M)[1:]
        for b in blocks:
            i, j = b.find("### 🧭"), b.find("### 🤖 MAIN AGENT")
            if i >= 0 and j >= 0:
                self.assertLess(i, j, "subagent blocks must precede the main-agent section")
        self.assertIn("*(end of subagent «Research API docs (Explore)»)*", self.md)

    def test_embedded_headings_are_demoted(self):
        parsed = ex.parse_claude_session(self.path)
        parsed["turns"].append({"role": "assistant", "text": "## Not a structure heading\n---\nx", "ts": None})
        md = ex.render_markdown(parsed)
        self.assertIn("###### Not a structure heading", md)
        self.assertNotIn("\n## Not a structure heading", md)

    def test_detail_levels_are_monotonic_and_consistent(self):
        sizes, users = [], []
        for flags in ([], ["--no-subagent-reports"], ["--final-only", "--no-subagent-reports"],
                      ["--final-only", "--no-subagents"]):
            r = subprocess.run([sys.executable, str(SCRIPTS / "extract_session.py"), "extract",
                                "--path", str(self.path), *flags], capture_output=True, text=True,
                               env={**os.environ, "HOME": str(self.tmp)})
            self.assertEqual(r.returncode, 0, r.stderr)
            sizes.append(len(r.stdout))
            users.append(r.stdout.count("## 👤 USER MESSAGE"))
            self.assertIn("Imported context:", r.stderr)
        self.assertEqual(sizes, sorted(sizes, reverse=True), sizes)
        self.assertEqual(len(set(users)), 1)
        self.assertNotIn("🔹", subprocess.run([sys.executable, str(SCRIPTS / "extract_session.py"), "extract",
                                              "--path", str(self.path), "--final-only"],
                                             capture_output=True, text=True).stdout)

    def test_list_and_title(self):
        self.assertEqual(self.parsed["title"], "Synthetic test session")
        r = subprocess.run([sys.executable, str(SCRIPTS / "extract_session.py"), "list", "--all-projects",
                            "--agent", "claude"], capture_output=True, text=True,
                           env={**os.environ, "HOME": str(self.tmp)})
        self.assertIn("Synthetic test session", r.stdout)
        self.assertIn("4 msgs", r.stdout)


class CodexTests(unittest.TestCase):
    def test_codex_rollout(self):
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "rollout-x.jsonl"
        rows = [
            {"timestamp": T % 0, "type": "session_meta", "payload": {"id": "019x", "cwd": "/p", "timestamp": T % 0}},
            {"timestamp": T % 1, "type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>\nHello codex"}]}},
            {"timestamp": T % 2, "type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Hi, working."}]}},
            {"timestamp": T % 3, "type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
            {"timestamp": T % 4, "type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "All done."}]}},
        ]
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        p = ex.parse_codex_session(f)
        self.assertEqual(p["user_messages"], 1)
        self.assertEqual([t["text"] for t in p["turns"] if t["role"] == "user"], ["Hello codex"])
        self.assertEqual(p["turns"][-1]["text"], "All done.")
        self.assertEqual([t["text"] for t in p["turns"] if t["role"] == "assistant_progress"], ["Hi, working."])


class JsonMarkdownTests(unittest.TestCase):
    def test_json_to_markdown(self):
        md = ex.json_to_markdown({"summary": "S", "items": [{"title": "A", "severity": "major"}], "empty": []})
        self.assertIn("- **summary:** S", md)
        self.assertIn("1. **A**", md)
        self.assertIn("- **severity:** major", md)
        self.assertIn("- **empty:** *(empty)*", md)

    def test_structured_and_workflow_result(self):
        self.assertEqual(ex.structured_to_markdown("plain"), "plain")
        self.assertIn("- **a:** 1", ex.structured_to_markdown('<result>{"a":1}</result>'))
        body = '<result>{"a":1,"b":"x\n... (truncated 999 chars, full result in /x)</result><failures>[r:a] failed: q</failures>'
        md = ex.workflow_result_to_markdown(body)
        self.assertIn("only available truncated", md)
        self.assertIn("- [r:a] failed: q", md)


class DesktopCacheTests(unittest.TestCase):
    def _v8(self, obj):
        """Tiny V8 ValueSerializer encoder for the subset the reader handles."""
        out = bytearray(b"\xff\x0f")

        def varint(n):
            while True:
                b = n & 0x7f; n >>= 7
                out.append(b | (0x80 if n else 0))
                if not n:
                    return

        def val(o):
            if isinstance(o, str):
                b = o.encode("latin-1"); out.append(ord('"')); varint(len(b)); out.extend(b)
            elif isinstance(o, bool):
                out.append(ord("T") if o else ord("F"))
            elif isinstance(o, int):
                out.append(ord("I")); varint((o << 1) ^ (o >> 63))
            elif o is None:
                out.append(ord("0"))
            elif isinstance(o, dict):
                out.append(ord("o"))
                for k, v in o.items():
                    val(k); val(v)
                out.append(ord("{")); varint(len(o))
            elif isinstance(o, list):
                out.append(ord("A")); varint(len(o))
                for v in o:
                    val(v)
                out.append(ord("$")); varint(0); varint(len(o))
        val(obj)
        return bytes(out)

    def test_v8_roundtrip(self):
        obj = {"conversationUuid": "code:cse_1", "tree": {"kind": "code_session", "messages": [{"type": "user"}],
                                                          "headCut": True}, "n": 42}
        self.assertEqual(v8idb.load(self._v8(obj)), obj)

    def test_snappy_literal_block(self):
        raw = self._v8({"k": "v"})
        # snappy: uncompressed length varint + one literal chunk
        n = len(raw)
        comp = bytes([n]) + bytes([((n - 1) << 2)]) + raw
        self.assertEqual(v8idb.load_idb_blob(b"\xff\x11\x02" + comp), {"k": "v"})


class HookTests(unittest.TestCase):
    def test_pack_chunks_reassembles_exactly(self):
        import on_compact
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(2000))
        chunks = on_compact.pack_chunks(text)
        self.assertTrue(all(len(c) <= on_compact.CHUNK_CHARS for c in chunks))
        self.assertEqual("".join(chunks), text)

    def test_reminder_threshold_and_window_inference(self):
        import pre_compact_docs_reminder as r
        home = Path(tempfile.mkdtemp()); (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
        saved = os.environ.get("HOME"); os.environ["HOME"] = str(home)
        try:
            self.assertEqual(r.effective_window()[0], 200_000)                  # 4.x model: 200k
            window, basis = r.effective_window(measured_tokens=650_000)
            self.assertEqual(window, 1_000_000); self.assertIn("inferred", basis)  # usage proves 1M
            (home / ".claude" / "settings.json").write_text(json.dumps({"model": "claude-fable-5-1"}))
            self.assertEqual(r.effective_window()[0], 1_000_000)                # Claude 5 family: 1M
        finally:
            os.environ["HOME"] = saved
        tmp = Path(tempfile.mkdtemp()); f = tmp / "s.jsonl"
        f.write_text(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 100,
                     "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0, "output_tokens": 0}}}) + "\n"
                     + json.dumps({"type": "assistant", "isSidechain": True, "message": {"usage": {
                         "input_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                         "output_tokens": 0}}}) + "\n")
        self.assertEqual(r.current_context_tokens(str(f)), 1000)  # sidechain usage ignored

    def test_reminder_fires_once_and_rearms(self):
        tmp = Path(tempfile.mkdtemp()); f = tmp / "s.jsonl"
        f.write_text(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 900_000,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}}}) + "\n")
        env = {**os.environ, "HOME": str(tmp), "DOC_REMINDER_FRACTION": "0.8"}
        payload = json.dumps({"session_id": "unit-test-session", "transcript_path": str(f)})
        marker = Path("/tmp") / "claude-doc-reminder-unit-test-session"
        marker.unlink(missing_ok=True)
        r1 = subprocess.run([sys.executable, str(HOOKS / "pre_compact_docs_reminder.py")], input=payload,
                            capture_output=True, text=True, env=env)
        self.assertIn("Context checkpoint", r1.stdout)
        self.assertIn("numbered list of the NEXT STEPS", r1.stdout)
        r2 = subprocess.run([sys.executable, str(HOOKS / "pre_compact_docs_reminder.py")], input=payload,
                            capture_output=True, text=True, env=env)
        self.assertEqual(r2.stdout, "")  # once per cycle
        marker.unlink(missing_ok=True)

    def test_on_compact_ask_mode_only_first_slot_speaks(self):
        path = build_claude_session(Path(tempfile.mkdtemp()))
        payload = json.dumps({"transcript_path": str(path), "session_id": "x"})
        r1 = subprocess.run([sys.executable, str(HOOKS / "on_compact.py"), "--part", "1", "--parts", "40"],
                            input=payload, capture_output=True, text=True)
        r2 = subprocess.run([sys.executable, str(HOOKS / "on_compact.py"), "--part", "2", "--parts", "40"],
                            input=payload, capture_output=True, text=True)
        self.assertIn("AskUserQuestion", r1.stdout)
        self.assertIn("- Full:", r1.stdout)
        self.assertIn("- Minimal:", r1.stdout)
        self.assertEqual(r2.stdout, "")




class ContinuedCloudSessionTests(unittest.TestCase):
    """A local session that continues a claude.ai/code cloud session: the file
    holds only a replay of the cloud main chain (version "1.0", fresh uuids,
    same message/tool ids). The original cloud session must be identified by
    those ids and its history (with subagent reports) used up to the takeover."""

    REPORT = "Long research report about visualization methods. " * 20

    def cloud_entries(self):
        c = "2026-01-01T09:%02d:00.000Z"
        A = "toolu_A"
        return [
            {"type": "user", "uuid": "c1", "parentUuid": None, "timestamp": c % 0,
             "message": {"role": "user", "content": "Build the analyzer"}},
            {"type": "assistant", "uuid": "c2", "parentUuid": "c1", "timestamp": c % 1,
             "message": {"id": "msg_c2", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "tool_use",
                         "content": [{"type": "text", "text": "Launching the research agent."},
                                     {"type": "tool_use", "id": A, "name": "Agent",
                                      "input": {"description": "Research methods", "subagent_type": "general-purpose",
                                                "run_in_background": True, "prompt": "Research"}}]}},
            {"type": "user", "uuid": "c3", "parentUuid": "c2", "timestamp": c % 2,
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": A,
                                                      "content": "Async agent launched successfully."}]}},
            {"type": "user", "uuid": "s1", "parentUuid": None, "timestamp": c % 3, "parent_tool_use_id": A,
             "subagent_type": "general-purpose", "task_description": "Research methods",
             "message": {"role": "user", "content": "Research"}},
            {"type": "assistant", "uuid": "s2", "parentUuid": "s1", "timestamp": c % 4, "parent_tool_use_id": A,
             "message": {"id": "msg_s2", "role": "assistant", "content": [{"type": "text", "text": self.REPORT}]}},
            {"type": "assistant", "uuid": "s3", "parentUuid": "s2", "timestamp": c % 5, "parent_tool_use_id": A,
             "message": {"id": "msg_s3", "role": "assistant",
                         "content": [{"type": "text", "text": "Handed back. Key finding: spectrograms win."}]}},
            {"type": "user", "uuid": "c4", "parentUuid": "c3", "timestamp": c % 6, "isSynthetic": True,
             "message": {"role": "user", "content": "<agent-message from=\"TASK1\">\n[Subagent hand-back]\n"
                                                    + self.REPORT + "</agent-message>"}},
            {"type": "user", "uuid": "c5", "parentUuid": "c4", "timestamp": c % 7,
             "message": {"role": "user", "content": "<task-notification><task-id>TASK1</task-id><tool-use-id>"
                                                    + A + "</tool-use-id><status>completed</status>"
                                                    "<summary>done</summary></task-notification>"}},
            {"type": "assistant", "uuid": "c6", "parentUuid": "c5", "timestamp": c % 8,
             "message": {"id": "msg_c6", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "Final: the analyzer is planned."}]}},
            {"type": "result", "uuid": "c7", "parentUuid": "c6", "timestamp": c % 9},
            # activity in the cloud AFTER the local takeover must not appear
            {"type": "user", "uuid": "c8", "parentUuid": "c7", "timestamp": c % 30,
             "message": {"role": "user", "content": "Something after the fork"}},
            {"type": "assistant", "uuid": "c9", "parentUuid": "c8", "timestamp": c % 31,
             "message": {"id": "msg_c9", "role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "post-fork answer"}]}},
        ]

    def local_file(self, tmp):
        r = "2026-01-01T12:00:00.%03dZ"
        A = "toolu_A"
        def replay(kind, uuid, parent, i, message):
            return {"type": kind, "uuid": uuid, "parentUuid": parent, "timestamp": r % i, "version": "1.0",
                    "userType": "unknown", "isSidechain": False, "cwd": "/tmp/proj", "message": message}
        lines = [
            replay("user", "r1", None, 0, {"role": "user", "content": "Build the analyzer"}),
            replay("assistant", "r2", "r1", 1, {"id": "msg_c2", "role": "assistant", "model": "claude-opus-5-5",
                   "stop_reason": "tool_use", "content": [{"type": "text", "text": "Launching the research agent."},
                   {"type": "tool_use", "id": A, "name": "Agent", "input": {"description": "Research methods"}}]}),
            replay("user", "r3", "r2", 2, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": A,
                   "content": "Async agent launched successfully."}]}),
            replay("user", "r4", "r3", 3, {"role": "user", "content": "<task-notification><task-id>TASK1</task-id>"
                   "<tool-use-id>" + A + "</tool-use-id><status>completed</status><summary>done</summary>"
                   "</task-notification>"}),
            replay("assistant", "r5", "r4", 4, {"id": "msg_c6", "role": "assistant", "model": "claude-opus-5-5",
                   "stop_reason": "end_turn", "content": [{"type": "text", "text": "Final: the analyzer is planned."}]}),
            {"type": "bridge-session", "sessionId": "local-1", "bridgeSessionId": "cse_bridge"},
            user("l1", "r5", "2026-01-01T12:05:00.000Z", "Continue locally: add tests"),
            assistant("l2", "l1", "2026-01-01T12:06:00.000Z", [{"type": "text", "text": "Added the tests."}], mid="msg_l2"),
        ]
        f = tmp / "local-1.jsonl"
        f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        return f

    def test_origin_found_by_ids_and_history_merged(self):
        tmp = Path(tempfile.mkdtemp())
        f = self.local_file(tmp)
        cloud = self.cloud_entries()
        calls = []
        orig = {"id": "cse_orig", "environment_kind": "anthropic_cloud", "title": "Cloud title",
                "created_at": "2026-01-01T08:00:00Z", "updated_at": "2026-01-01T09:40:00Z"}
        decoy = {"id": "cse_decoy", "environment_kind": "anthropic_cloud", "title": "Other",
                 "created_at": "2026-01-01T07:00:00Z", "updated_at": "2026-01-01T11:00:00Z"}
        bridge = {"id": "cse_bridge", "environment_kind": "bridge", "created_at": "2026-01-01T12:00:00Z"}

        def fake_get(url, params=None):
            calls.append((url, params))
            if "cse_decoy" in url:
                return {"data": [{"payload": {"message": {"id": "msg_other"}}}]}
            if "cse_orig" in url:
                return {"data": [{"payload": e} for e in cloud[:100]]}
            return None
        saved = (ex.CACHE_DIR, ex.cloud_token, ex.cloud_sessions, ex.cloud_get, ex.cloud_events, ex.cloud_session_meta)
        ex.CACHE_DIR = tmp / "cache"
        ex.cloud_token = lambda: "tok"
        ex.cloud_sessions = lambda: [bridge, decoy, orig]
        ex.cloud_get = fake_get
        ex.cloud_events = lambda cid: list(cloud) if cid == "cse_orig" else []
        ex.cloud_session_meta = lambda cid: orig
        try:
            parsed = ex.parse_claude_session(f)
            md = ex.render_markdown(parsed)
            # the decoy (closest activity) was checked first by its oldest page, then the origin
            self.assertEqual([u.split("/")[-2] for u, p in calls if p and p.get("cursor") == 101],
                             ["cse_decoy", "cse_orig"])
            self.assertEqual(parsed["cloud_origin"], "cse_orig")
            roles = [t["role"] for t in parsed["turns"]]
            self.assertEqual([t["text"] for t in parsed["turns"] if t["role"] == "user"],
                             ["Build the analyzer", "Continue locally: add tests"])
            subs = [t for t in parsed["turns"] if t["role"] == "subagent"]
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0]["name"], "Research methods (general-purpose)")
            self.assertIn("Long research report", subs[0]["report"])
            self.assertLess(roles.index("subagent"), roles.index("assistant"))  # subagent before final answer
            self.assertNotIn("post-fork", md)           # cloud activity after the takeover is cut
            self.assertIn("Added the tests.", md)      # local tail follows
            self.assertEqual(parsed["title"], "Cloud title")
            self.assertEqual(ex.fmt_ts(parsed["first_ts"]), "2026-01-01 " + ex.fmt_ts(cloud[0] and ex.parse_ts(cloud[0]["timestamp"]), with_date=False))
            self.assertIn("cse_orig", md.split("---")[0])  # Source line names the origin
            self.assertNotIn("⚠", md)
            # cached: a second parse needs no session search and no event fetch
            ex.cloud_sessions = lambda: (_ for _ in ()).throw(AssertionError("network used"))
            ex.cloud_events = lambda cid: (_ for _ in ()).throw(AssertionError("network used"))
            parsed2 = ex.parse_claude_session(f, cloud="cache")
            self.assertEqual(parsed2["cloud_origin"], "cse_orig")
            self.assertEqual(len([t for t in parsed2["turns"] if t["role"] == "subagent"]), 1)
            # without any cloud access the local replay is used and the gap is flagged
            ex.CACHE_DIR = tmp / "empty-cache"
            ex.cloud_token = lambda: None
            parsed3 = ex.parse_claude_session(f)
            md3 = ex.render_markdown(parsed3)
            self.assertTrue(parsed3.get("cloud_origin_missing"))
            self.assertIn("⚠", md3)
            self.assertIn("--cloud-origin", md3)
            self.assertEqual([t["text"] for t in parsed3["turns"] if t["role"] == "user"],
                             ["Build the analyzer", "Continue locally: add tests"])
        finally:
            (ex.CACHE_DIR, ex.cloud_token, ex.cloud_sessions, ex.cloud_get, ex.cloud_events,
             ex.cloud_session_meta) = saved

    def test_replay_detection_ignores_normal_sessions(self):
        tmp = Path(tempfile.mkdtemp())
        f = build_claude_session(tmp)
        self.assertFalse(any(ex.is_replay_entry(e) for e in ex.iter_jsonl(f)))


class ModelDetectionTests(unittest.TestCase):
    def test_model_and_window_come_from_the_running_session(self):
        home = Path(tempfile.mkdtemp())
        proj = home / ".claude" / "projects" / "-tmp-x"; proj.mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(json.dumps({"model": "claude-fable-5-1"}))
        sid = "22222222-aaaa-bbbb-cccc-000000000002"
        f = proj / f"{sid}.jsonl"
        f.write_text(json.dumps({"type": "assistant", "isSidechain": False, "message": {
            "model": "claude-opus-5-5", "usage": {"input_tokens": 10, "cache_read_input_tokens": 300_000,
                                                  "cache_creation_input_tokens": 0, "output_tokens": 1}}}) + "\n"
            + json.dumps({"type": "assistant", "isSidechain": True, "message": {"model": "claude-haiku-4-5",
                          "usage": {"input_tokens": 1}}}) + "\n")
        saved = (ex.CLAUDE_PROJECTS_DIR, os.environ.get("CLAUDE_CODE_SESSION_ID"), os.environ.get("HOME"))
        ex.CLAUDE_PROJECTS_DIR = home / ".claude" / "projects"
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid; os.environ["HOME"] = str(home)
        try:
            model, window = ex.current_model_and_window()
            self.assertEqual(model, "claude-opus-5-5, this session")   # transcript wins over settings
            self.assertEqual(window, 1_000_000)
            self.assertIn("1000k-token", ex.import_summary(4000))
            os.environ["CLAUDE_CODE_SESSION_ID"] = "does-not-exist"
            model, window = ex.current_model_and_window(project="/nowhere/at/all")
            self.assertEqual(model, "claude-fable-5-1, from settings")  # fallback: settings
            self.assertEqual(window, 1_000_000)
            self.assertEqual(ex.model_window("claude-opus-4-8"), 200_000)
            self.assertEqual(ex.model_window("claude-opus-4-8", usage=250_000), 1_000_000)
            import pre_compact_docs_reminder as r
            self.assertEqual(r.transcript_model(str(f)), "claude-opus-5-5")   # sidechain model ignored
            (home / ".claude" / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
            self.assertEqual(r.effective_window(model="claude-opus-5-5")[0], 1_000_000)  # session model wins
            self.assertEqual(r.effective_window()[0], 200_000)
        finally:
            ex.CLAUDE_PROJECTS_DIR = saved[0]
            if saved[1] is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved[1]
            os.environ["HOME"] = saved[2]


if __name__ == "__main__":
    unittest.main()


class ChatTests(unittest.TestCase):
    """Ordinary claude.ai chats from Claude Desktop's cache (blob files and
    inline LevelDB values)."""

    ROOT = "00000000-0000-4000-8000-000000000000"

    def chat_record(self, cid="chat-uuid-1", title="Freiberufler-Frage"):
        def H(u, p, i, text, files=()):
            return {"uuid": u, "parent_message_uuid": p, "index": i, "sender": "human",
                    "created_at": f"2026-02-01T10:{i:02d}:00Z", "content": [{"type": "text", "text": text}],
                    "files": list(files), "attachments": []}

        def A(u, p, i, blocks):
            return {"uuid": u, "parent_message_uuid": p, "index": i, "sender": "assistant",
                    "created_at": f"2026-02-01T10:{i:02d}:00Z", "content": blocks}
        msgs = [
            H("h1", self.ROOT, 0, "Kann ich freiberuflich arbeiten?"),
            A("a1", "h1", 1, [{"type": "thinking", "thinking": "secret reasoning"},
                              {"type": "text", "text": "Ich pruefe das kurz."},
                              {"type": "tool_use", "id": "t1", "name": "web_search", "input": {"query": "x"}},
                              {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "RESULT-NOISE"}]},
                              {"type": "text", "text": "Ja, als Freiberufler geht das."}]),
            H("h2", "a1", 2, "Und die Nachteile?", files=[{"file_name": "vertrag.pdf"}]),
            A("a2", "h2", 3, [{"type": "text", "text": "Alte Antwort (abandoned branch)"}]),
            A("a3", "h2", 4, [{"type": "text", "text": "Die Nachteile sind ..."},
                              {"type": "tool_use", "id": "t2", "name": "artifacts",
                               "input": {"command": "create", "title": "Vergleich", "content": "# Vergleich\nGewerbe vs. frei"}},
                              {"type": "tool_use", "id": "t3", "name": "message_compose_v1",
                               "input": {"kind": "email", "variants": [{"label": "kooperativ", "subject": "Rahmenvertrag",
                                                                         "body": "Hallo Herr K,\n\nvielen Dank."}]}},
                              {"type": "tool_use", "id": "t4", "name": "Gmail:send_message",
                               "input": {"to": "k@example.com", "subject": "Rahmenvertrag", "body": "Gesendeter Text."}},
                              {"type": "text", "text": "Ich habe dir eine Uebersicht erstellt."}]),
        ]
        return {"conversationUuid": cid, "product": "chat", "fetchedAt": 1700000000000,
                "conversationUpdatedAt": 1700000000000, "messageCount": 5,
                "tree": {"uuid": cid, "name": title, "model": "claude-opus-5-5",
                         "created_at": "2026-02-01T10:00:00Z", "updated_at": "2026-02-01T10:04:00Z",
                         "current_leaf_message_uuid": "a3",
                         "parentByChildUuid": {"h1": self.ROOT, "a1": "h1", "h2": "a1", "a2": "h2", "a3": "h2"},
                         "chat_messages": msgs}}

    def test_parse_chat_displayed_branch_only(self):
        parsed = ex.parse_chat_record(self.chat_record())
        md = ex.render_markdown(parsed)
        self.assertEqual([t["role"] for t in parsed["turns"]],
                         ["user", "assistant_progress", "assistant", "user", "assistant_progress", "assistant"])
        for noise in ("abandoned branch", "secret reasoning", "RESULT-NOISE", "web_search"):
            self.assertNotIn(noise, md)
        self.assertIn("*[file attached: vertrag.pdf]*", md)
        self.assertIn("📄 **Artifact «Vergleich»** (create)", md)
        self.assertIn("##### Vergleich", md)                  # artifact content, headings demoted
        self.assertIn("✉ **Draft email — variant «kooperativ»**\nSubject: Rahmenvertrag\n\nHallo Herr K,", md)
        self.assertIn("✉ **Sent email via Gmail**\nTo: k@example.com\nSubject: Rahmenvertrag\n\nGesendeter Text.", md)
        final_md = ex.render_markdown(ex.parse_chat_record(self.chat_record(), all_text=False))
        self.assertIn("Hallo Herr K,", final_md)              # deliverables survive every detail level
        self.assertNotIn("Die Nachteile sind", final_md)
        self.assertEqual(parsed["title"], "Freiberufler-Frage")
        self.assertEqual(parsed["user_messages"], 2)
        self.assertIn("1 messages on edited/regenerated branches omitted", parsed["source"])
        self.assertIn("claude.ai chat (Claude Desktop)", md)
        self.assertRegex(md, r"\*\*Time:\*\* 2026-02-01 \d\d:00 → 2026-02-01 \d\d:04")  # local time zone
        final = ex.parse_chat_record(self.chat_record(), all_text=False)
        self.assertEqual([t["role"] for t in final["turns"]], ["user", "assistant", "user", "assistant"])

    @staticmethod
    def _varint(n):
        out = bytearray()
        while True:
            b = n & 0x7f; n >>= 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def test_leveldb_log_reader_and_chat_discovery(self):
        import leveldb
        enc = DesktopCacheTests._v8
        tmp = Path(tempfile.mkdtemp()); ldb = tmp / "leveldb"; ldb.mkdir(); blobdir = tmp / "blob"; blobdir.mkdir()

        def batch(seq, entries):
            body = seq.to_bytes(8, "little") + len(entries).to_bytes(4, "little")
            for t, k, v in entries:
                body += bytes([t]) + self._varint(len(k)) + k + ((self._varint(len(v)) + v) if t == 1 else b"")
            return body

        def record(data, rtype=1):
            return b"\0\0\0\0" + len(data).to_bytes(2, "little") + bytes([rtype]) + data
        v1 = b"\x09" + enc(None, self.chat_record())                       # version varint + V8 value
        v2 = b"\x09" + enc(None, self.chat_record("chat-uuid-2", "Deleted later"))
        v3 = b"\x09" + enc(None, self.chat_record("chat-uuid-3", "Fragmented record"))
        third = batch(7, [(1, b"k3", v3)])
        log = (record(batch(1, [(1, b"k1", v1), (1, b"k2", v2)]))
               + record(batch(3, [(0, b"k2", b"")]))                              # deletion wins (newer seq)
               + record(third[:100], 2) + record(third[100:], 4))                  # FIRST + LAST fragments
        (ldb / "000003.log").write_bytes(log)
        values = leveldb.latest_values(ldb)
        self.assertEqual(sorted(values), [b"k1", b"k3"])
        self.assertEqual(values[b"k3"], v3)
        saved = (ex.DESKTOP_IDB_BLOB_DIR, ex.DESKTOP_IDB_LEVELDB_DIR)
        ex.DESKTOP_IDB_BLOB_DIR, ex.DESKTOP_IDB_LEVELDB_DIR = blobdir, ldb
        ex._idb_cache["records"] = None
        try:
            chats = ex.desktop_chats()
            self.assertEqual(sorted(ref.id for _, ref in chats), ["chat-uuid-1", "chat-uuid-3"])
            self.assertEqual([a for a, _ in ex.discover("chat", None, True)], ["chat", "chat"])
            self.assertFalse(any(a == "chat" for a, _ in ex.discover("all", None, False)))  # only --all-projects
            self.assertTrue(any(a == "chat" for a, _ in ex.discover("all", None, True)))
            parsed = ex.parse_any("chat", chats[0][1])
            self.assertEqual(parsed["agent"], "chat")
        finally:
            ex.DESKTOP_IDB_BLOB_DIR, ex.DESKTOP_IDB_LEVELDB_DIR = saved
            ex._idb_cache["records"] = None

    def test_leveldb_table_block_entries(self):
        import leveldb
        v = self._varint
        block = (v(0) + v(3) + v(1) + b"abc" + b"X"
                 + v(2) + v(1) + v(2) + b"d" + b"YZ"
                 + (0).to_bytes(4, "little") + (1).to_bytes(4, "little"))   # one restart point
        self.assertEqual(list(leveldb._block_entries(block)), [(b"abc", b"X"), (b"abd", b"YZ")])


class PathTests(unittest.TestCase):
    def test_munge_path_normalizes_decomposed_unicode(self):
        import unicodedata
        nfc = "/tmp/Vertical Consulting - Verträge"
        nfd = unicodedata.normalize("NFD", nfc)
        self.assertNotEqual(nfc, nfd)
        self.assertEqual(ex.munge_path(nfd), ex.munge_path(nfc))
        self.assertTrue(ex.munge_path(nfc).endswith("Vertical-Consulting---Vertr-ge"))


class CreateSessionTests(unittest.TestCase):
    """`create`: an extract becomes a NEW Claude Code session in another project."""

    def test_entries_alternate_and_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        src = build_claude_session(tmp)
        parsed = ex.parse_claude_session(src)
        entries = ex.build_session_entries(parsed, "/tmp/project-b", "new-sid", version="2.1.0", model="claude-x")
        self.assertEqual(entries[0]["type"], "custom-title")
        self.assertTrue(entries[0]["customTitle"].startswith("Imported: "))
        msgs = entries[1:]
        self.assertEqual(msgs[0]["type"], "user")
        self.assertIn("[context-cycle] Condensed history imported", msgs[0]["message"]["content"])
        self.assertTrue(all(a["type"] != b["type"] for a, b in zip(msgs, msgs[1:])))   # roles alternate
        self.assertEqual([m["parentUuid"] for m in msgs], [None] + [m["uuid"] for m in msgs[:-1]])
        self.assertTrue(all(m["sessionId"] == "new-sid" and m["cwd"] == "/tmp/project-b" for m in msgs))
        stamps = [m["timestamp"] for m in msgs]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(b["type"] == "text" for m in msgs if m["type"] == "assistant"
                            for b in m["message"]["content"]))
        text = "\n".join(json.dumps(m["message"], ensure_ascii=False) for m in msgs)
        for u in (t["text"] for t in parsed["turns"] if t["role"] == "user"):
            self.assertIn(u, text)
        for a in (t["text"] for t in parsed["turns"] if t["role"] == "assistant"):
            self.assertIn(a[:40], text)
        self.assertIn("Subagent «", text)                       # subagent results carried over
        self.assertNotIn("tool_use", text)
        # the new file reads back as a normal session
        new = tmp / "new-sid.jsonl"
        new.write_text("".join(json.dumps(e) + "\n" for e in entries))
        back = ex.parse_claude_session(new, cloud="off")
        self.assertEqual(back["title"], entries[0]["customTitle"])
        self.assertGreaterEqual(back["user_messages"], parsed["user_messages"])
        self.assertEqual(sum(1 for t in back["turns"] if t["role"] == "assistant"), len([m for m in msgs if m["type"] == "assistant"]))

    def test_cmd_create_writes_into_target_project(self):
        tmp = Path(tempfile.mkdtemp())
        src = build_claude_session(tmp)
        target = tmp / "project-b"; target.mkdir()
        saved = (ex.CLAUDE_PROJECTS_DIR, os.environ.get("CLAUDE_CODE_SESSION_ID"))
        ex.CLAUDE_PROJECTS_DIR = tmp / ".claude" / "projects"
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        out = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                ex.main.__globals__["sys"].argv = ["x", "create", "--path", str(src), "--into", str(target),
                                                   "--final-only", "--json"]
                ex.main()
        finally:
            ex.CLAUDE_PROJECTS_DIR = saved[0]
            if saved[1]:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved[1]
        info = json.loads(out.getvalue()[out.getvalue().index("{"):])
        path = Path(info["path"])
        self.assertEqual(path.parent, tmp / ".claude" / "projects" / ex.munge_path(target))
        self.assertTrue(path.is_file())
        self.assertIn("claude --resume " + info["session_id"], out.getvalue())
        back = ex.parse_claude_session(path, cloud="off")
        self.assertIn("detail level Final answers only", back["turns"][0]["text"])
