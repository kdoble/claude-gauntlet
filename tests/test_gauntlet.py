"""Tests for gauntlet. Stdlib unittest only.

The load-bearing tests are the ones proving a guard is NOT vacuous:
- test_leak_guard_fails_on_bad_artifact: the leak checker RAISES on a planted secret.
- test_check_fires_on_known_bad: a waste check FIRES on a fixture built to trip it, and
  stays silent on a clean one.
- test_average_floor: no average is claimed below N=3 runs.
A checker never seen to fail is not a checker; these make failure observable.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gauntlet as g  # noqa: E402

PRICING = g.load_json(os.path.join(os.path.dirname(__file__), "..", "pricing.json"))


def _usage_line(mid, model, inp=0, ccr=0, cc5=0, out=0, ts="2026-07-01T00:00:00Z",
                tool=None, tool_input=None, tool_result=None, side=False):
    """One assistant JSONL line with a usage block; optionally a tool_use / tool_result."""
    content = []
    if tool:
        content.append({"type": "tool_use", "name": tool, "input": tool_input or {}})
    msg = {"id": mid, "model": model, "usage": {
        "input_tokens": inp, "cache_read_input_tokens": ccr,
        "cache_creation_input_tokens": cc5, "output_tokens": out}, "content": content}
    o = {"type": "assistant", "timestamp": ts, "isSidechain": side, "message": msg}
    return json.dumps(o)


def _invoke_line(skill="underwrite", ts="2026-07-01T00:00:00Z"):
    body = f"<command-message>{skill}</command-message>\n<command-name>/{skill}</command-name>\n<command-args>go</command-args>"
    return json.dumps({"type": "user", "timestamp": ts,
                       "message": {"role": "user", "content": body}})


def _result_line(tid, payload):
    return json.dumps({"type": "user", "message": {"role": "user", "content":
                      [{"type": "tool_result", "tool_use_id": tid, "content": payload}]}})


def _write_session(dirpath, session, lines):
    proj = os.path.join(dirpath, "proj-a")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, session + ".jsonl"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _slurp(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class TraceMath(unittest.TestCase):
    def test_dedupe_on_message_id(self):
        # two content blocks share one id -> ONE step, not two
        lines = [_invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=10),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=10, tool="Bash")]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0].steps), 1)
        self.assertIn("Bash", runs[0].steps[0].tools)

    def test_run_totals_exact(self):
        lines = [_invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=10, ccr=1000, cc5=200, out=50),
                 _usage_line("m2", "claude-opus-4-8", inp=10, ccr=2000, out=30)]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
        t = g.run_totals(runs[0], PRICING)
        self.assertEqual(t["steps"], 2)
        self.assertEqual(t["ccr"], 3000)
        self.assertEqual(t["out"], 80)
        self.assertEqual(t["tokens"], 10 + 1000 + 200 + 50 + 10 + 2000 + 30)
        # overhead = first step context (inp+cc5+cc1+ccr)
        self.assertEqual(t["overhead"], 10 + 200 + 1000)

    def test_synthetic_model_skipped(self):
        lines = [_invoke_line(),
                 _usage_line("m1", "<synthetic>", inp=5, out=5),
                 _usage_line("m2", "claude-opus-4-8", inp=5, ccr=100, out=5)]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
        self.assertEqual(len(runs[0].steps), 1)
        self.assertEqual(runs[0].synthetic_events, 1)


class Attribution(unittest.TestCase):
    def test_one_run_per_session_despite_requotes(self):
        # invocation, then the same command re-quoted later must NOT make a 2nd run
        lines = [_invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5),
                 _invoke_line(),  # a re-quote / re-echo
                 _usage_line("m2", "claude-opus-4-8", inp=5, ccr=200, out=5)]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0].steps), 2)

    def test_different_skill_is_not_matched(self):
        # a /spock session that merely mentions underwrite in args must not be a run
        body = ("<command-message>spock</command-message>\n<command-name>/spock</command-name>"
                "\n<command-args>fix the underwrite thing</command-args>")
        lines = [json.dumps({"type": "user", "message": {"role": "user", "content": body}}),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5)]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
        self.assertEqual(len(runs), 0)


class LeakGuard(unittest.TestCase):
    def test_leak_guard_fails_on_bad_artifact(self):
        # THE negative test: the guard must RAISE when a forbidden string is present.
        bad = "<html>... C:/Users/testuser/secret_deal ...</html>"
        with self.assertRaises(AssertionError):
            g.assert_no_leak(bad, ["C:/Users/testuser/secret_deal"])

    def test_leak_guard_passes_on_clean_artifact(self):
        g.assert_no_leak("<html>clean</html>", ["C:/Users/testuser/secret_deal"])  # no raise

    def test_shared_does_not_leak_basename_via_reread_finding(self):
        # Regression for the security review's Finding 1: a file Read TWICE would surface
        # its basename in the re_read finding text. In shared mode, production ordering is
        # diagnose(shared=True) BEFORE redact, then render. The basename must NOT ship, and
        # the guard (which now includes basenames) must not raise on a clean shared report.
        lines = [_invoke_line()]
        for i in range(2):
            lines.append(_usage_line(f"m{i}", "claude-opus-4-8", inp=5, ccr=100, out=5,
                         tool="Read", tool_input={"file_path": "C:/deals/ACME_T12.xlsx"}))
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
            m = g.measure(runs, PRICING)
            # internal: the re_read finding SHOULD name the file (useful detail)
            internal = g.diagnose(runs, m, PRICING, {}, shared=False)
            rr = next(f for f in internal if f["key"] == "re_read")
            self.assertTrue(rr["fired"])
            self.assertIn("ACME_T12.xlsx", rr["msg"])
            # shared: production ordering. diagnose(shared=True) first, then redact, then render.
            shared_findings = g.diagnose(runs, m, PRICING, {}, shared=True)
            basenames = {t for r in runs for s in r.steps for t in s.read_targets}
            forbidden = list({r.project for r in runs}) + list(basenames)
            g.redact_for_share(m, runs)
            shared_html = g.render("underwrite", m, runs, shared_findings,
                                   g.recommendations(shared_findings, m), PRICING, shared=True)
            self.assertNotIn("ACME_T12.xlsx", shared_html)
            g.assert_no_leak(shared_html, forbidden)  # must not raise

    def test_shared_report_redacts_and_guards(self):
        # a Read of a sensitive file name: internal shows it, shared must not, and the
        # shared render must survive the guard against the real project dir name.
        lines = [_invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5,
                             tool="Read", tool_input={"file_path": "C:/deals/SECRET.xlsx"})]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
            # internal report DOES show the basename
            m = g.measure(runs, PRICING)
            internal = g.render("underwrite", m, runs, g.diagnose(runs, m, PRICING, {}),
                                [], PRICING, shared=False)
            self.assertIn("SECRET.xlsx", internal)
            # shared: redact then render, and the guard must pass (no project dir, no basename)
            g.redact_for_share(m, runs)
            shared = g.render("underwrite", m, runs, g.diagnose(runs, m, PRICING, {}),
                              [], PRICING, shared=True)
            self.assertNotIn("SECRET.xlsx", shared)
            g.assert_no_leak(shared, ["proj-a", "SECRET.xlsx"])


class ChecksFire(unittest.TestCase):
    def _run_from(self, lines):
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            return g.find_runs("underwrite", d)

    def test_reread_check_fires_and_is_silent_when_clean(self):
        # bad: same file read twice -> re_read fires. clean: read once -> silent.
        bad = [_invoke_line()]
        for i in range(3):
            bad.append(_usage_line(f"m{i}", "claude-opus-4-8", inp=5, ccr=100, out=5,
                       tool="Read", tool_input={"file_path": "C:/x/config.py"}))
        runs = self._run_from(bad)
        m = g.measure(runs, PRICING)
        rr = next(f for f in g.diagnose(runs, m, PRICING, {}) if f["key"] == "re_read")
        self.assertTrue(rr["fired"], "re_read should fire on a repeated read")

        clean = [_invoke_line(),
                 _usage_line("m0", "claude-opus-4-8", inp=5, ccr=100, out=5,
                             tool="Read", tool_input={"file_path": "C:/x/a.py"}),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5,
                             tool="Read", tool_input={"file_path": "C:/x/b.py"})]
        runs = self._run_from(clean)
        m = g.measure(runs, PRICING)
        rr = next(f for f in g.diagnose(runs, m, PRICING, {}) if f["key"] == "re_read")
        self.assertFalse(rr["fired"], "re_read must stay silent when each file read once")

    def test_heavy_recon_check_fires(self):
        # a >20k-token tool_result landing in main context (no subagent) must fire.
        big = "x" * (25_000 * 4)  # ~25k tokens by the 4-bytes/token estimate
        lines = [_invoke_line(),
                 _usage_line("m0", "claude-opus-4-8", inp=5, ccr=100, out=5, tool="Bash"),
                 _result_line("t0", big),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=200, out=5)]
        runs = self._run_from(lines)
        m = g.measure(runs, PRICING)
        hr = next(f for f in g.diagnose(runs, m, PRICING, {}) if f["key"] == "heavy_recon")
        self.assertTrue(hr["fired"], "heavy_recon should fire on an oversized tool_result")

    def test_heavy_recon_ignores_image_blocks(self):
        # THE bug this fixes: a huge base64 image blob must NOT read as heavy recon. A
        # rendered page tokenizes by pixel dimensions (~1.5k tok), not its file size. The
        # old bytes/4 estimate turned a 440KB PNG into a phantom ~110k-token flag.
        import base64
        import struct
        # a valid PNG header claiming 1275x1650 (a letter page), then a huge junk tail
        hdr = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
               + struct.pack(">II", 1275, 1650) + b"\x08\x06\x00\x00\x00")
        blob = base64.b64encode(hdr).decode() + "A" * 600_000  # ~600k base64 chars
        img = [{"type": "image", "source": {"type": "base64",
                "media_type": "image/png", "data": blob}}]
        lines = [_invoke_line(),
                 _usage_line("m0", "claude-opus-4-8", inp=5, ccr=100, out=5, tool="Read",
                             tool_input={"file_path": "C:/deals/tax_p0.png"}),
                 _result_line("t0", img),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=200, out=5)]
        runs = self._run_from(lines)
        # the image step must cost ~1.5k tok, NOT ~150k (600k base64 / 4)
        step0 = runs[0].steps[0]
        self.assertLess(step0.injected_tokens, 4000,
                        "an image must cost image-tokens, not its base64 byte length")
        m = g.measure(runs, PRICING)
        hr = next(f for f in g.diagnose(runs, m, PRICING, {}) if f["key"] == "heavy_recon")
        self.assertFalse(hr["fired"], "heavy_recon must NOT fire on an image read")

    def test_image_tokens_matches_anthropic_rule(self):
        # a letter page (~1275x1650) lands near the observed 1.2k-3.6k real cost, capped
        t = g.image_tokens(1275, 1650)
        self.assertTrue(500 < t < 3000, f"letter-page image tokens out of range: {t}")
        # a giant image is downscaled, so tokens stay bounded, never proportional to pixels
        self.assertLess(g.image_tokens(8000, 8000), 2500)
        self.assertEqual(g.image_tokens(0, 0), g.IMAGE_TOKENS_FALLBACK)


class AverageFloor(unittest.TestCase):
    def test_average_floor(self):
        # 2 runs -> below floor -> enough_for_average False; 3 -> True
        def sess(name):
            return [_invoke_line(), _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5)]
        with tempfile.TemporaryDirectory() as d:
            for s in ("s1", "s2"):
                _write_session(d, s, sess(s))
            runs = g.find_runs("underwrite", d)
            self.assertEqual(len(runs), 2)
            self.assertFalse(g.measure(runs, PRICING)["enough_for_average"])
            _write_session(d, "s3", sess("s3"))
            runs = g.find_runs("underwrite", d)
            self.assertTrue(g.measure(runs, PRICING)["enough_for_average"])

    def test_empty_reports_honestly(self):
        with tempfile.TemporaryDirectory() as d:
            runs = g.find_runs("nothinghere", d)
        self.assertEqual(runs, [])
        m = g.measure(runs, PRICING)
        html = g.render("nothinghere", m, runs, [], [], PRICING)
        self.assertIn("No historical run", html)


class FabricationGuards(unittest.TestCase):
    """Regressions for the tool-written-user-turn fabrication class (2026-07-18).
    A harvest tool wrote type:user lines whose STRING content was a JSON report
    quoting command tags as example data; the unanchored matcher read them as real
    invocations and fabricated up to 100% of a skill's runs."""

    def test_json_blob_user_line_does_not_fabricate(self):
        blob = json.dumps({"asks": [
            "<command-message>underwrite</command-message>\n"
            "<command-name>/underwrite</command-name>"]})
        lines = [json.dumps({"type": "user", "timestamp": "2026-07-01T00:00:00Z",
                             "message": {"role": "user", "content": blob}}),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5)]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            self.assertEqual(g.find_runs("underwrite", d), [],
                             "a JSON blob quoting the tag must never become a run")

    def test_inline_quoted_tag_does_not_fabricate(self):
        # tag embedded mid-line (quotes/commas around it) must not match the
        # anchored pattern; only a tag alone on its own line is an invocation
        body = 'see the example "<command-name>/underwrite</command-name>", which shows'
        lines = [json.dumps({"type": "user", "message": {"role": "user", "content": body}}),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5)]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            self.assertEqual(g.find_runs("underwrite", d), [])

    def test_malformed_usage_line_skipped_not_fatal(self):
        # one corrupt usage field must cost one line, never the whole scan
        bad = json.dumps({"type": "assistant", "message": {
            "id": "mB", "model": "claude-opus-4-8",
            "usage": {"input_tokens": "N/A", "output_tokens": 5}, "content": []}})
        lines = [_invoke_line(), bad,
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5)]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
        self.assertEqual(len(runs), 1)
        # the corrupt line coerces to 0s or is skipped; the good step survives
        self.assertGreaterEqual(len(runs[0].steps), 1)


class AgentRuns(unittest.TestCase):
    def test_agent_spawn_traced_from_subagent_file(self):
        # parent session spawns subagent_type=researcher; the agent's own transcript
        # holds its steps; find_agent_runs must trace THAT file as one run.
        parent = [
            json.dumps({"type": "assistant", "message": {
                "id": "p1", "model": "claude-opus-4-8",
                "usage": {"input_tokens": 5, "output_tokens": 5},
                "content": [{"type": "tool_use", "id": "tu1", "name": "Agent",
                             "input": {"subagent_type": "researcher", "prompt": "go"}}]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1",
                 "content": "done. agentId: abc123"}]}}),
        ]
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, "proj-a")
            os.makedirs(os.path.join(proj, "s1", "subagents"))
            with open(os.path.join(proj, "s1.jsonl"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(parent) + "\n")
            agent_lines = [_usage_line("a1", "claude-sonnet-5", inp=5, ccr=50, out=9),
                           _usage_line("a2", "claude-sonnet-5", inp=5, ccr=90, out=7)]
            with open(os.path.join(proj, "s1", "subagents", "agent-abc123.jsonl"),
                      "w", encoding="utf-8") as fh:
                fh.write("\n".join(agent_lines) + "\n")
            runs = g.find_agent_runs("researcher", d)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0].steps), 2)
        self.assertFalse(runs[0].mid_session)


class SharedToolNames(unittest.TestCase):
    def test_shared_aliases_custom_tool_names(self):
        # a custom MCP tool name can identify a client; --shared must alias it and
        # the guard must cover the original name (raises if it ever ships).
        lines = [_invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5,
                             tool="mcp__acmeclient__query")]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
            m = g.measure(runs, PRICING)
            findings = g.diagnose(runs, m, PRICING, {}, shared=True)
            hidden = g.redact_for_share(m, runs)
            self.assertIn("mcp__acmeclient__query", hidden)
            self.assertEqual(runs[0].steps[0].tools, ["tool-1"])
            html = g.render("underwrite", m, runs, findings,
                            g.recommendations(findings, m), PRICING, shared=True)
            self.assertNotIn("acmeclient", html)
            g.assert_no_leak(html, list(hidden))  # must not raise

    def test_builtin_tool_names_kept_in_shared(self):
        lines = [_invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5, tool="Bash")]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
            g.redact_for_share(g.measure(runs, PRICING), runs)
            self.assertEqual(runs[0].steps[0].tools, ["Bash"])


class SharedGuardRobustness(unittest.TestCase):
    def test_common_extensionless_basename_does_not_crash_shared(self):
        # Regression (exec review H1): a run that Read a file named 'run' used to crash
        # --shared with a false "content leak" because 'run' collides with the report's
        # own words ("run reconstructed..."). A bare lowercase word is not identifying
        # and must not enter the guard; --shared must produce a report, not raise.
        lines = [_invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5,
                             tool="Read", tool_input={"file_path": "/home/dev/proj/run"})]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
            m = g.measure(runs, PRICING)
            findings = g.diagnose(runs, m, PRICING, {}, shared=True)
            basenames = {t for r in runs for s in r.steps for t in s.read_targets
                         if g._identifying(t)}
            self.assertEqual(basenames, set(), "'run' must not be an identifying basename")
            hidden = g.redact_for_share(m, runs)
            forbidden = list({r.project for r in runs}) + list(basenames) + list(hidden)
            html = g.render("underwrite", m, runs, findings,
                            g.recommendations(findings, m), PRICING, shared=True)
            g.assert_no_leak(html, forbidden)  # must not raise

    def test_identifying_helper(self):
        for ident in ("ACME_T12.xlsx", "deed_p0.png", "ACMECORP", "123-main.jpg",
                      "verylongprojectname"):
            self.assertTrue(g._identifying(ident), ident)
        for bare in ("run", "main", "build", "cost", "node", "done", "file", ""):
            self.assertFalse(g._identifying(bare), bare)

    def test_shared_guard_holds_real_project_name(self):
        # Regression (exec review M2): the forbidden set must hold the REAL project name,
        # captured before redact aliases it, or the guard protects nothing.
        lines = [_invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5)]
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, "acmecorp-deal")
            os.makedirs(proj)
            with open(os.path.join(proj, "s1.jsonl"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            runs = g.find_runs("underwrite", d)
            m = g.measure(runs, PRICING)
            projects = {r.project for r in runs}          # BEFORE redact
            self.assertIn("acmecorp-deal", projects)
            g.redact_for_share(m, runs)
            self.assertNotIn("acmecorp-deal", {r.project for r in runs})  # aliased now
            # the guard, fed the pre-redact names, would catch a real-name leak
            with self.assertRaises(AssertionError):
                g.assert_no_leak("<html>acmecorp-deal</html>", list(projects))


class ParseErrorCounting(unittest.TestCase):
    def test_corrupt_json_line_is_counted(self):
        # exec review L3: a corrupt JSON line must increment parse_errors so the report's
        # "N malformed line(s) skipped" caveat is honest.
        lines = [_invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5),
                 '{"type":"assistant","message":{"id":"m2","mod',  # truncated
                 _usage_line("m3", "claude-opus-4-8", inp=5, ccr=200, out=5)]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
        self.assertEqual(runs[0].parse_errors, 1)
        self.assertEqual(len(runs[0].steps), 2)


class EscapeAtSink(unittest.TestCase):
    def test_hostile_basename_never_ships_raw(self):
        # defense in depth: even if a future finding forgets its own esc(), the
        # render sink escapes. A hostile filename must land inert in the HTML.
        hostile = "<img src=x onerror=alert(1)>.xlsx"
        lines = [_invoke_line()]
        for i in range(2):
            lines.append(_usage_line(f"m{i}", "claude-opus-4-8", inp=5, ccr=100, out=5,
                         tool="Read", tool_input={"file_path": "C:/x/" + hostile}))
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
            m = g.measure(runs, PRICING)
            findings = g.diagnose(runs, m, PRICING, {})
            html = g.render("underwrite", m, runs, findings,
                            g.recommendations(findings, m), PRICING)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)


class HonestAverages(unittest.TestCase):
    def test_min_runs_floor_read_from_checklist(self):
        # the config key must be LIVE: floor 2 in checklist -> 2 runs is enough
        def sess():
            return [_invoke_line(), _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5)]
        with tempfile.TemporaryDirectory() as d:
            for s in ("s1", "s2"):
                _write_session(d, s, sess())
            runs = g.find_runs("underwrite", d)
            self.assertFalse(g.measure(runs, PRICING)["enough_for_average"])
            self.assertTrue(g.measure(runs, PRICING,
                                      {"min_runs_for_average": 2})["enough_for_average"])

    def test_no_median_language_below_floor(self):
        # a single run: overhead/verbosity/shape must not speak "median"
        lines = [_invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=70_000, ccr=0, out=5)]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
        m = g.measure(runs, PRICING)
        self.assertFalse(m["enough_for_average"])
        for f in g.diagnose(runs, m, PRICING, {}):
            if f["key"] in ("overhead", "verbosity", "shape"):
                self.assertNotIn("median", f["msg"].lower(),
                                 f"{f['key']} claims a median off n=1: {f['msg']}")

    def test_overhead_qualifier_keys_off_fresh_count(self):
        # 3 runs total but only 1 fresh: the overhead check may not say "median"
        def fresh_sess():
            return [_invoke_line(), _usage_line("m1", "claude-opus-4-8", inp=70_000, out=5)]
        def mid_sess():
            return [_usage_line("m0", "claude-opus-4-8", inp=5, ccr=300_000, out=5),
                    _invoke_line(),
                    _usage_line("m1", "claude-opus-4-8", inp=5, ccr=300_000, out=5)]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", fresh_sess())
            _write_session(d, "s2", mid_sess())
            _write_session(d, "s3", mid_sess())
            runs = g.find_runs("underwrite", d)
        m = g.measure(runs, PRICING)
        self.assertTrue(m["enough_for_average"])
        self.assertEqual(m["overhead_fresh_n"], 1)
        ov = next(f for f in g.diagnose(runs, m, PRICING, {}) if f["key"] == "overhead")
        self.assertNotIn("median", ov["msg"].lower())
        self.assertIn("n=1", ov["msg"])

    def test_mid_session_runs_excluded_from_overhead(self):
        # invocation AFTER billed work: step-0 context is the session's history,
        # not the skill's floor. With no fresh run, overhead must say so.
        lines = [_usage_line("m0", "claude-opus-4-8", inp=5, ccr=400_000, out=5),
                 _invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=400_000, out=5)]
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
        self.assertTrue(runs[0].mid_session)
        m = g.measure(runs, PRICING)
        self.assertEqual(m["overhead_fresh_n"], 0)
        ov = next(f for f in g.diagnose(runs, m, PRICING, {}) if f["key"] == "overhead")
        self.assertFalse(ov["fired"])
        self.assertIn("cannot isolate", ov["msg"])


class SavingRendering(unittest.TestCase):
    def test_saving_amt_never_splits_a_word(self):
        # the heavy_recon phrasing must render "~45,231 tokens", never bite "tok"
        # out of a compound word
        s = "~45,231 tokens recoverable across runs by moving that recon to a subagent."
        self.assertEqual(g._saving_amt(s), "~45,231 tokens")
        self.assertNotIn("/", g._saving_amt("~5 tok/all-runs style"))


class Pricing(unittest.TestCase):
    def test_family_token_prices_real_ids(self):
        # a real Claude 3.x id must match the sonnet family, not the Opus fallback
        p, fb = g.price_for("claude-3-5-sonnet-20241022", PRICING)
        self.assertFalse(fb, "a real sonnet id must not hit the fallback rate")
        self.assertIn("onnet", p["label"])

    def test_pricing_models_are_public_families_only(self):
        # A public tool must not ship a non-public model name/price. Assert every entry's
        # match key is on an allowlist of families that appear on a PUBLIC provider rate
        # card, so a future edit that adds an internal codename FAILS here. Provenance,
        # not fiat: each family below is on a public pricing page, verified 2026-07-20 at
        # https://platform.claude.com/docs/en/about-claude/pricing (Claude) and
        # https://openai.com/api/pricing (OpenAI). Add a name ONLY after confirming the
        # same, and cite it here. Limited-availability models (e.g. Mythos) are deliberately
        # NOT shipped: the fallback rate covers them, and a public repo has no need to.
        public = {"opus", "sonnet", "haiku", "fable", "gpt", "codex", "o1",
                  "o3", "gemini", "claude-3", "claude-2"}
        for entry in PRICING["models"]:
            match = entry["match"].lower()
            self.assertTrue(any(p in match or match in p for p in public),
                            f"pricing entry '{match}' is not a known public model family; "
                            f"if it is public, add it to the allowlist")

    def test_no_nonpublic_codename_in_pricing(self):
        raw = json.dumps(PRICING).lower()
        for bad in ("codename", "internal-only", "unreleased", "confidential"):
            self.assertNotIn(bad, raw)


class CliFeatures(unittest.TestCase):
    def test_version_flag(self):
        # argparse action="version" exits 0 after printing.
        with self.assertRaises(SystemExit) as cm:
            g.main(["--version"])
        self.assertEqual(cm.exception.code, 0)

    def test_demo_produces_report_and_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r.html")
            js = os.path.join(d, "r.json")
            rc = g.main(["--demo", "--out", out, "--json", js])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(out))
            blob = _load(js)
            self.assertEqual(blob["schema_version"], g.JSON_SCHEMA_VERSION)
            self.assertEqual(blob["gauntlet_version"], g.__version__)
            self.assertEqual(blob["skill"], "demo-skill")
            self.assertGreaterEqual(blob["n_runs"], 3)
            html = _slurp(out)
            self.assertIn("Median Tokens", html)

    def test_demo_cleans_up_tempdir(self):
        import glob as _glob
        before = set(_glob.glob(os.path.join(tempfile.gettempdir(), "gauntlet-demo-*")))
        with tempfile.TemporaryDirectory() as d:
            g.main(["--demo", "--out", os.path.join(d, "r.html")])
        after = set(_glob.glob(os.path.join(tempfile.gettempdir(), "gauntlet-demo-*")))
        self.assertEqual(before, after, "demo temp dir was not cleaned up")

    def _runs_over_days(self, days):
        lines = [_invoke_line()]
        for i, day in enumerate(days):
            lines.append(_usage_line(f"m{i}", "claude-opus-4-8", inp=5, ccr=50, out=5,
                                     ts=f"{day}T00:00:00Z"))
            lines.append(_invoke_line())  # each invoke starts a fresh run boundary
        return lines

    def test_filter_since_and_last(self):
        # three runs on distinct days; --since and --last narrow them, and disclose.
        with tempfile.TemporaryDirectory() as d:
            for k, day in enumerate(("2026-07-01", "2026-07-10", "2026-07-20")):
                _write_session(d, f"s{k}", [_invoke_line(ts=f"{day}T00:00:00Z"),
                    _usage_line("m1", "claude-opus-4-8", inp=5, ccr=50, out=5,
                                ts=f"{day}T00:00:00Z")])
            runs = g.find_runs("underwrite", d)
            self.assertEqual(len(runs), 3)
            kept, note = g.filter_runs(runs, since="2026-07-10")
            self.assertEqual(len(kept), 2)
            self.assertIn("since 2026-07-10", note)
            kept2, note2 = g.filter_runs(runs, last=1)
            self.assertEqual(len(kept2), 1)
            self.assertEqual(kept2[0].day, "2026-07-20")
            kept3, note3 = g.filter_runs(runs)
            self.assertEqual(len(kept3), 3)
            self.assertIsNone(note3)

    def test_filter_edge_cases_are_safe(self):
        # invalid/boundary filters must never crash; they yield a safe empty-or-full set.
        with tempfile.TemporaryDirectory() as d:
            for k, day in enumerate(("2026-07-01", "2026-07-10", "2026-07-20")):
                _write_session(d, f"s{k}", [_invoke_line(ts=f"{day}T00:00:00Z"),
                    _usage_line("m1", "claude-opus-4-8", inp=5, ccr=50, out=5,
                                ts=f"{day}T00:00:00Z")])
            runs = g.find_runs("underwrite", d)
            self.assertEqual(len(g.filter_runs(runs, last=0)[0]), 0)
            self.assertEqual(len(g.filter_runs(runs, last=-1)[0]), 0)   # max(0,-1) slice
            self.assertEqual(len(g.filter_runs(runs, last=99)[0]), 3)   # beyond length
            self.assertEqual(len(g.filter_runs(runs, since="2027-01-01")[0]), 0)
            self.assertEqual(len(g.filter_runs(runs, since="2020-01-01")[0]), 3)
            self.assertEqual(len(g.filter_runs(runs, since="2026-07-05", last=1)[0]), 1)
        # a dateless run is excluded by --since, never crashes
        dr = g.Run("s", "p", "f")
        self.assertEqual(len(g.filter_runs([dr], since="2026-07-01")[0]), 0)

    def test_tokens_first_is_default(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r.html")
            g.main(["--demo", "--out", out])
            html = _slurp(out)
            # tokens tile carries the accent by default; cost is de-emphasized + labeled
            self.assertRegex(html, r'stat accent"[^>]*>\s*<div class="k">Median Tokens')
            self.assertIn("counterfactual", html)

    def test_dollars_flag_leads_with_cost(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r.html")
            g.main(["--demo", "--dollars", "--out", out])
            html = _slurp(out)
            self.assertRegex(html, r'stat accent"[^>]*>\s*<div class="k">Median Cost')


class FleetAndBaseline(unittest.TestCase):
    def _two_skills(self, d):
        _write_session(d, "s1", [_invoke_line("alpha"),
            _usage_line("m1", "claude-opus-4-8", inp=5, ccr=500000, out=50)])
        _write_session(d, "s2", [_invoke_line("beta"),
            _usage_line("m2", "claude-opus-4-8", inp=5, ccr=50, out=5)])

    def test_audit_all_ranks_worst_first(self):
        with tempfile.TemporaryDirectory() as d:
            self._two_skills(d)
            rows = g.audit_all(d, PRICING, {})
            names = [r["name"] for r in rows]
            self.assertEqual(names, ["alpha", "beta"])  # alpha burns more tokens -> first
            self.assertGreater(rows[0]["tokens"], rows[1]["tokens"])
            self.assertFalse(rows[0]["enough"])  # 1 run each -> below floor, honest

    def test_all_flag_exit_and_output(self):
        with tempfile.TemporaryDirectory() as d:
            self._two_skills(d)
            rc = g.main(["--all", "--claude-dir", d])
            self.assertEqual(rc, 0)

    def test_baseline_diff(self):
        cur_findings = [{"key": "shape", "fired": False},
                        {"key": "overhead", "fired": False}]
        m = {"steps": {"median": 10}, "tokens": {"median": 600_000},
             "cost": {"median": 1.0}, "cache_hit": {"median": 0.9}}
        prior = {"schema_version": g.JSON_SCHEMA_VERSION,
                 "medians": {"steps": 12, "tokens": 900_000, "cost": 1.5, "cache_hit": 0.8},
                 "findings": [{"key": "overhead", "fired": True}], "window": [None, "2026-07-01"]}
        d = g.compare_baseline(prior, m, cur_findings)
        self.assertAlmostEqual(d["metrics"]["tokens"]["pct"], -1 / 3, places=4)
        self.assertIn("overhead", d["cleared"])
        self.assertEqual(d["regressed"], [])

    def test_baseline_schema_mismatch_refuses(self):
        prior = {"schema_version": 999, "medians": {}, "findings": []}
        with self.assertRaises(ValueError):
            g.compare_baseline(prior, {"steps": {"median": 1}, "tokens": {"median": 1},
                                       "cost": {"median": 1}, "cache_hit": {"median": 1}}, [])

    def test_baseline_bad_file_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            rc = g.main(["--demo", "--baseline", os.path.join(d, "nope.json"),
                         "--out", os.path.join(d, "r.html")])
            self.assertEqual(rc, 2)

    def test_baseline_type_garbage_never_tracebacks(self):
        # Regression (review HIGH): a user hand-edits their baseline and fat-fingers a value.
        # Wrong types (quoted number, non-list findings, short/int window) must degrade, not
        # crash. Run each through the real CLI and assert a clean exit, no exception.
        m = {"steps": {"median": 10}, "tokens": {"median": 600_000},
             "cost": {"median": 1.0}, "cache_hit": {"median": 0.9}}
        bad = [
            {"schema_version": 1, "medians": {"tokens": "100"}},
            {"schema_version": 1, "medians": "oops"},
            {"schema_version": 1, "findings": "gotcha"},
            {"schema_version": 1, "findings": 5},
            {"schema_version": 1, "window": 5},
            {"schema_version": 1, "window": ["only"]},
            {"schema_version": 1, "medians": {"tokens": True}},   # bool is not a metric
        ]
        for prior in bad:
            d = g.compare_baseline(prior, m, [])   # must not raise
            self.assertIn("metrics", d)
            self.assertNotIn(True, [v.get("old") for v in d["metrics"].values()])

    def test_shared_baseline_cannot_inject_identifier(self):
        # Regression (security review LOW): a hand-poisoned baseline must not smuggle an
        # identifier (a fake finding key or a fake window value) into a --shared report,
        # which is built from `runs` only. compare_baseline sanitizes at the source.
        m = {"steps": {"median": 1}, "tokens": {"median": 900_000},
             "cost": {"median": 1.0}, "cache_hit": {"median": 0.9}}
        poison = {"schema_version": 1, "medians": {"tokens": 900_000},
                  "findings": [{"key": "POISON_KEY_ClientAcme", "fired": True}],
                  "window": [None, "POISON_WINDOW_HarborDeal"]}
        cur = [{"key": "re_read", "fired": False}, {"key": "shape", "fired": False}]
        d = g.compare_baseline(poison, m, cur)
        self.assertEqual(d["cleared"], [])            # unknown key dropped, not surfaced
        self.assertEqual(d["regressed"], [])
        self.assertEqual(d["prior_window"], [None, None])   # non-date window value dropped
        strip = g._baseline_html(d)
        self.assertNotIn("POISON_KEY_ClientAcme", strip)
        self.assertNotIn("POISON_WINDOW_HarborDeal", strip)

    def test_baseline_shape_excluded_from_cleared(self):
        # Regression (review MED): shape is a classifier, not a flag, on BOTH sides.
        m = {"steps": {"median": 1}, "tokens": {"median": 1},
             "cost": {"median": 1}, "cache_hit": {"median": 1}}
        prior = {"schema_version": 1, "medians": {},
                 "findings": [{"key": "shape", "fired": True}, {"key": "re_read", "fired": True}]}
        d = g.compare_baseline(prior, m, [{"key": "shape", "fired": False},
                                          {"key": "re_read", "fired": False}])
        self.assertEqual(d["cleared"], ["re_read"])
        self.assertNotIn("shape", d["cleared"])

    def test_pricing_wrong_shape_falls_back(self):
        # Regression (review LOW): valid JSON missing 'models' must fall back, not KeyError.
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "p.json")
            with open(bad, "w") as fh:
                fh.write('{"as_of":"2026-01-01"}')
            self.assertIs(g.load_config(bad, g.DEFAULT_PRICING,
                                        required=("models", "fallback")), g.DEFAULT_PRICING)
            rc = g.main(["--demo", "--pricing", bad, "--out", os.path.join(d, "r.html")])
            self.assertEqual(rc, 0)


class ConfigFallback(unittest.TestCase):
    def test_default_pricing_matches_file(self):
        # The embedded fallback must not drift from the shipped pricing.json on any
        # functional field (rates, multipliers, match order). Cosmetic keys are ignored.
        def functional(p):
            return {"cache_read_multiplier": p["cache_read_multiplier"],
                    "cache_write_5m_multiplier": p["cache_write_5m_multiplier"],
                    "cache_write_1h_multiplier": p["cache_write_1h_multiplier"],
                    "models": [{k: mo[k] for k in sorted(mo)} for mo in p["models"]],
                    "fallback": p["fallback"]}
        self.assertEqual(functional(g.DEFAULT_PRICING), functional(PRICING))

    def test_load_config_falls_back(self):
        self.assertIs(g.load_config("/no/such/file.json", g.DEFAULT_PRICING),
                      g.DEFAULT_PRICING)

    def test_demo_runs_without_pricing_file(self):
        # a lone copied gauntlet.py (no pricing.json) still audits via the embedded default
        with tempfile.TemporaryDirectory() as d:
            rc = g.main(["--demo", "--pricing", os.path.join(d, "missing.json"),
                         "--out", os.path.join(d, "r.html")])
            self.assertEqual(rc, 0)


class PricingFreshness(unittest.TestCase):
    def _one_run_report(self, as_of, today, stale_days=90):
        lines = [_invoke_line(),
                 _usage_line("m1", "claude-opus-4-8", inp=5, ccr=100, out=5)]
        pricing = dict(PRICING, as_of=as_of)
        with tempfile.TemporaryDirectory() as d:
            _write_session(d, "s1", lines)
            runs = g.find_runs("underwrite", d)
            m = g.measure(runs, pricing)
            findings = g.diagnose(runs, m, pricing, {})
            recs = g.recommendations(findings, m)
            os.environ["GAUNTLET_TODAY"] = today
            try:
                return g.render("underwrite", m, runs, findings, recs, pricing,
                                checklist={"pricing_stale_days": stale_days})
            finally:
                del os.environ["GAUNTLET_TODAY"]

    def test_age_days(self):
        self.assertEqual(g.pricing_age_days({"as_of": "2026-01-01"}, "2026-01-31"), 30)
        self.assertIsNone(g.pricing_age_days({"as_of": "nope"}, "2026-01-31"))
        self.assertIsNone(g.pricing_age_days({}, "2026-01-31"))

    def test_stale_pricing_warns(self):
        # NEGATIVE-TEST RULE: prove the warning actually fires on a known-stale rate card.
        html = self._one_run_report("2026-01-01", "2026-07-20")  # ~200 days
        self.assertIn("days old", html)
        self.assertIn("2026-01-01", html)

    def test_fresh_pricing_silent(self):
        html = self._one_run_report("2026-07-10", "2026-07-20")  # 10 days, under 90
        self.assertNotIn("days old", html)


CHECKLIST = g.load_json(os.path.join(os.path.dirname(__file__), "..", "checklist.json")) or {}


def _demo_report(shared=False):
    """Render a report from the bundled demo transcripts (fires real flags)."""
    with tempfile.TemporaryDirectory() as d:
        g.write_demo_transcripts(d, sessions=4)
        runs = g.find_runs("demo-skill", d)
        m = g.measure(runs, PRICING)
        findings = g.diagnose(runs, m, PRICING, CHECKLIST, shared=shared)
        recs = g.recommendations(findings, m)
        return g.render("demo-skill", m, runs, findings, recs, PRICING,
                        shared=shared, checklist=CHECKLIST)


class HardeningAndHonesty(unittest.TestCase):
    def test_report_carries_restrictive_csp(self):
        html = _demo_report()
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("default-src 'none'", html)
        # no scripts are emitted, so script-src stays absent and default-src 'none' blocks them
        self.assertNotIn("<script", html.lower())

    def test_write_text_is_atomic_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "sub", "r.html")   # parent does not exist yet
            g._write_text(out, "<html>ok</html>")
            self.assertEqual(_slurp(out), "<html>ok</html>")
            # the atomic temp file must not survive next to the target
            leftovers = [n for n in os.listdir(os.path.dirname(out)) if n.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_write_text_private_perms_on_posix(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r.html")
            g._write_text(out, "x")
            self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)

    def test_recommendations_are_hedged_not_asserted(self):
        # The overclaim words must be gone; the hedged framing must be present.
        html = _demo_report()
        self.assertIn("Opportunities to Investigate", html)
        self.assertNotIn("quality held constant", html)
        self.assertNotIn("<h2 class=\"section-title\">Recommendations</h2>", html)

    def test_shared_report_is_redacted_not_anonymous(self):
        html = _demo_report(shared=True)
        self.assertIn("redacted, not anonymous", html)
        self.assertIn("Review it before publishing", html)

    def test_privacy_language_is_accurate_not_never_reads(self):
        # We must NOT claim we never read content; we DO parse it, we just never emit it.
        html = _demo_report()
        self.assertIn("reads and parses your transcripts", html)
        self.assertNotIn("never leaves this machine", html)  # the old overclaim

    def test_console_entrypoint_is_claude_gauntlet_with_alias(self):
        # 3.9 has no tomllib; assert on the raw text of the packaging config.
        txt = _slurp(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"))
        self.assertIn('claude-gauntlet = "gauntlet:main"', txt)
        self.assertIn('gauntlet = "gauntlet:main"', txt)  # compatibility alias retained


if __name__ == "__main__":
    unittest.main()
