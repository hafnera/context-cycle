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
        window, basis = r.effective_window(measured_tokens=650_000)
        self.assertEqual(window, 1_000_000)
        self.assertIn("inferred", basis)
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


if __name__ == "__main__":
    unittest.main()
