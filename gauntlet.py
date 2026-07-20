#!/usr/bin/env python3
"""GAUNTLET: per-skill token-efficiency auditor.

Point it at ONE skill or agent (e.g. --skill my-skill). It finds that skill's
historical runs in the local Claude Code transcripts, reconstructs each run's
workflow step by step, measures token/cache/cost efficiency across runs, runs a
waste checklist, and writes one HTML report with ranked recommendations.

Stdlib only. Reads counters and labels (model, tool names, file basenames), never
transcript CONTENT. Internal by default; --shared redacts through a leak guard.
"""
import argparse
import datetime
import glob
import html
import json
import os
import re
import shutil
import statistics
import sys
import tempfile

__version__ = "0.1.0"
JSON_SCHEMA_VERSION = 1   # --json layout; bump on any breaking field change (docs/json_schema.md)

# ------------------------------------------------------------------ constants
# Anchored: a genuine invocation writes the tag ALONE on its own line. A tag quoted
# inside a JSON blob, code sample, or prose has surrounding characters on the line
# (quotes, commas, backticks) and must never match. (Fabrication fix #2, 2026-07-18:
# a harvest tool wrote user-type lines whose string content was a JSON report quoting
# command tags as example data; the unanchored regex read them as real invocations
# and fabricated up to 100% of a skill's runs.)
CMD_RE = re.compile(r"(?m)^\s*<command-name>/?([A-Za-z0-9_:.-]{1,48})</command-name>\s*$")

# Built-in housekeeping commands: never a real skill invocation, never a boundary.
UTILITY_CMDS = {
    "compact", "clear", "model", "status", "cost", "context", "help", "doctor",
    "resume", "init", "mcp", "memory", "hooks", "permissions", "config",
    "agents", "login", "logout", "bug", "export", "rewind", "todos",
}

SYNTHETIC_MODELS = {"<synthetic>", "", "claude-unknown"}
HEAVY_RESULT_TOKENS = 20_000     # a tool_result above this is "heavy recon"
MECHANICAL_OUT_TOKENS = 80       # an assistant turn below this output is "mechanical"
MIN_RUNS_FOR_AVERAGE = 3         # below this, print per-run numbers, never an average
BYTES_PER_TOKEN = 4.0            # rough tokens estimate from a TEXT payload byte length

# Image tool_results are NOT text: a rendered page tokenizes by its pixel dimensions,
# not its file size, so bytes/4 overstates an image's context cost by ~40-90x (a 440KB
# PNG reads as ~110k "tokens" but costs ~1.5k in context). We estimate image tokens from
# the decoded dimensions using Anthropic's published rule: fit within a 1568px long edge
# and ~1.15M pixels, then tokens ~= pixels / 750. (Bug fix 2026-07-18: the byte-based
# estimate fabricated a ~614k-token "heavy recon" flag on a real skill's page-image
# reads; the true cost was ~1.2k-3.6k tokens each, proven from the cache-read deltas.)
IMAGE_LONG_EDGE = 1568
IMAGE_MAX_PIXELS = 1_150_000
IMAGE_PIXELS_PER_TOKEN = 750
IMAGE_TOKENS_FALLBACK = 1600     # a full page image whose dimensions cannot be read


def esc(s):
    return html.escape(str(s), quote=True)


def _b64_image_dims(data, media_type):
    """(width, height) from a base64 image, or None. Stdlib only.

    PNG dimensions live in the IHDR chunk in the first ~24 bytes, so decoding a short
    prefix suffices. JPEG needs a scan to the SOF marker, so decode the whole blob.
    """
    import base64
    import struct
    data = data or ""
    mt = (media_type or "").lower()
    try:
        if "png" in mt:
            raw = base64.b64decode(data[:64] + "===")
            if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
                w, h = struct.unpack(">II", raw[16:24])
                return (w, h)
        if "jpeg" in mt or "jpg" in mt:
            buf = base64.b64decode(data + "===")
            i, n = 2, len(buf)
            while i + 9 < n:
                if buf[i] != 0xFF:
                    i += 1
                    continue
                marker = buf[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h = (buf[i + 5] << 8) | buf[i + 6]
                    w = (buf[i + 7] << 8) | buf[i + 8]
                    return (w, h)
                i += 2 + ((buf[i + 2] << 8) | buf[i + 3])
    except Exception:
        return None
    return None


def image_tokens(w, h):
    """True context-token cost of an image, per Anthropic's downscale-then-pixels rule."""
    import math
    if not w or not h:
        return IMAGE_TOKENS_FALLBACK
    scale = min(1.0, IMAGE_LONG_EDGE / float(max(w, h)))
    w2, h2 = w * scale, h * scale
    if w2 * h2 > IMAGE_MAX_PIXELS:
        s2 = math.sqrt(IMAGE_MAX_PIXELS / (w2 * h2))
        w2, h2 = w2 * s2, h2 * s2
    return max(1, math.ceil(w2 * h2 / IMAGE_PIXELS_PER_TOKEN))


def result_byte_equiv(content):
    """Byte-equivalent size of a tool_result, for the recon estimate.

    Text counts its real serialized length; an IMAGE block counts its true token cost
    times BYTES_PER_TOKEN, never the base64 blob length. This keeps `injected_tokens`
    (bytes/BYTES_PER_TOKEN) honest for both text and images.
    """
    if isinstance(content, list):
        total = 0
        for x in content:
            if isinstance(x, dict) and x.get("type") == "image":
                src = x.get("source") if isinstance(x.get("source"), dict) else {}
                dims = _b64_image_dims(str(src.get("data", "")), str(src.get("media_type", "")))
                toks = image_tokens(*dims) if dims else IMAGE_TOKENS_FALLBACK
                total += int(toks * BYTES_PER_TOKEN)
            else:
                total += len(json.dumps(x))
        return total
    return len(json.dumps(content))


# ------------------------------------------------------------------ data model
class Step:
    """One deduped billed API call in a run, plus what happened around it.

    Counters and labels only. read_targets holds file BASENAMES (for the re-read
    check and the internal trace); it never holds file contents.
    """
    __slots__ = ("idx", "model", "inp", "cc5", "cc1", "ccr", "out", "ts",
                 "tools", "read_targets", "injected_bytes", "spawns_agent")

    def __init__(self, idx, model, ts):
        self.idx = idx
        self.model = model
        self.ts = ts
        self.inp = self.cc5 = self.cc1 = self.ccr = self.out = 0
        self.tools = []
        self.read_targets = []
        self.injected_bytes = 0        # size of tool_results consumed after this step
        self.spawns_agent = False

    @property
    def context_total(self):
        return self.inp + self.cc5 + self.cc1 + self.ccr

    @property
    def billed_total(self):
        return self.context_total + self.out

    @property
    def cache_write(self):
        return self.cc5 + self.cc1

    @property
    def injected_tokens(self):
        return int(self.injected_bytes / BYTES_PER_TOKEN)


class Run:
    """One historical invocation of the target skill, as a trace of Steps."""
    __slots__ = ("session", "project", "file", "start_ts", "steps",
                 "synthetic_events", "mid_session", "parse_errors")

    def __init__(self, session, project, file):
        self.session = session
        self.project = project
        self.file = file
        self.start_ts = None
        self.steps = []
        self.synthetic_events = 0
        # True when the invocation landed in a session that already had billed work,
        # so step 0's context includes PRIOR UNRELATED content, not just the skill's
        # own load. The overhead check must not read such a run as the skill's floor.
        self.mid_session = False
        self.parse_errors = 0

    @property
    def day(self):
        return self.start_ts[:10] if self.start_ts and len(self.start_ts) >= 10 else None


# ------------------------------------------------------------------ FIND
def _user_text(o):
    """The user-authored text of a genuine user turn: string content or `text` blocks.

    Returns "" for anything else, crucially for a tool_result turn (whose content is a
    list of tool_result blocks, never text). A `<command-name>` tag quoted inside tool
    output must NEVER be read as an invocation, so command detection scans only this
    text, never the raw JSON line. (Security/correctness fix, 2026-07-18.)
    """
    if not isinstance(o, dict) or o.get("type") != "user":
        return ""
    content = (o.get("message") or {}).get("content")
    if isinstance(content, str):
        # Defense in depth vs tool-written user turns: a string body that IS a JSON
        # document was written by a tool, not typed by a person. Never a command.
        head = content.lstrip()[:1]
        if head in ("{", "["):
            try:
                json.loads(content)
                return ""
            except (json.JSONDecodeError, ValueError):
                pass
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _line_invokes(line, target):
    """Does this line invoke the target skill as a genuine user turn or Skill tool call?

    Only genuine user-authored command text or a real Skill tool_use counts. A tag merely
    quoted inside code, a fixture, a SKILL.md example, or any tool_result never matches.
    """
    tl = target.lower()
    if "<command-name>" in line and tl in line.lower():
        try:
            o = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return False
        for m in CMD_RE.findall(_user_text(o)):
            if m.lower() == tl:
                return True
    if '"Skill"' in line and '"skill"' in line and tl in line.lower():
        try:
            o = json.loads(line)
            for blk in (o.get("message") or {}).get("content") or []:
                if (isinstance(blk, dict) and blk.get("type") == "tool_use"
                        and blk.get("name") == "Skill"):
                    if (blk.get("input") or {}).get("skill", "").lower() == tl:
                        return True
        except (json.JSONDecodeError, ValueError, AttributeError):
            return False
    return False


def _line_other_skill(line, target):
    """The name of a DIFFERENT non-utility skill invoked in genuine user text (a run
    boundary), else None. Scans only user-authored text, never tool output."""
    tl = target.lower()
    if "<command-name>" not in line:
        return None
    try:
        o = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    for m in CMD_RE.findall(_user_text(o)):
        ml = m.lower()
        if ml in UTILITY_CMDS or ml == tl:
            continue
        return ml
    return None


def find_runs(target, claude_dir):
    """Return one Run per session that genuinely invoked `target`.

    Attribution rule (disclosed in the report so it is auditable, not hidden):
    a run is anchored at the FIRST genuine invocation of the skill in a session and
    spans to the end of that session. One session yields at most one run. Later lines
    that merely re-quote the command (compaction summaries, the assistant echoing the
    tag, fixtures) do NOT start a second run. If a clearly different top-level skill is
    invoked later in the session, the span is cut there so unrelated work is not
    attributed to the target.
    """
    runs = []
    paths = sorted(glob.glob(os.path.join(claude_dir, "*", "*.jsonl")))
    for path in paths:
        project = os.path.basename(os.path.dirname(path))
        session = os.path.basename(path)[:-6]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        n = len(lines)
        start = next((i for i in range(n) if _line_invokes(lines[i], target)), None)
        if start is None:
            continue
        # span to EOF, or to the next genuine different-skill user invocation that is
        # NOT itself a re-quote of the target (target re-quotes never cut the run).
        end = n
        for j in range(start + 1, n):
            if _line_invokes(lines[j], target):
                continue
            other = _line_other_skill(lines[j], target)
            if other:
                end = j
                break
        run = trace_run(lines[start:end], session, project, path)
        # billed work BEFORE the invocation means step 0 inherits prior context
        run.mid_session = any('"usage"' in lines[i] and '"assistant"' in lines[i]
                              for i in range(start))
        if run.steps:
            runs.append(run)
    return runs


def find_agent_runs(target, claude_dir):
    """Return one Run per SPAWN of the target agent (subagent_type == target).

    An agent's internal workflow lives in its own transcript file
    (<project>/<session>/subagents/agent-<id>.jsonl), so each spawn is traced from
    that file directly: the real steps, not just the parent's spawn + digest.
    """
    tl = target.lower()
    runs = []
    for path in sorted(glob.glob(os.path.join(claude_dir, "*", "*.jsonl"))):
        project = os.path.basename(os.path.dirname(path))
        session = os.path.basename(path)[:-6]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        spawn_ids = {}          # tool_use_id -> True for target spawns
        agent_files = []
        for line in lines:
            if "subagent_type" not in line and "agentId" not in line:
                continue
            try:
                o = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            content = ((o.get("message") or {}).get("content"))
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if (b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task")
                            and str((b.get("input") or {}).get("subagent_type", "")).lower() == tl):
                        spawn_ids[b.get("id")] = True
                    if b.get("type") == "tool_result" and b.get("tool_use_id") in spawn_ids:
                        mt = re.search(r"agentId[\"\\:\s]+([A-Za-z0-9]+)",
                                       json.dumps(b.get("content", "")))
                        if mt:
                            agent_files.append(mt.group(1))
            tur = o.get("toolUseResult")
            if isinstance(tur, dict) and tur.get("agentId") and spawn_ids:
                agent_files.append(str(tur["agentId"]))
        for aid in dict.fromkeys(agent_files):
            apath = os.path.join(os.path.dirname(path), session, "subagents",
                                 f"agent-{aid}.jsonl")
            if not os.path.exists(apath):
                continue
            try:
                with open(apath, "r", encoding="utf-8", errors="replace") as fh:
                    alines = fh.readlines()
            except OSError:
                continue
            run = trace_run(alines, f"agent-{aid}", project, apath)
            if run.steps:
                runs.append(run)   # a subagent file starts at spawn: never mid-session
    return runs


def find_inventory(claude_dir):
    """Every auditable name found in the transcripts: {(name, kind): {runs, last}}.
    kind is 'skill' (slash command or Skill tool) or 'agent' (subagent_type spawn)."""
    inv = {}

    def note(name, kind, day):
        k = (name, kind)
        e = inv.setdefault(k, {"runs": 0, "last": None})
        e["runs"] += 1
        if day and (e["last"] is None or day > e["last"]):
            e["last"] = day

    for path in sorted(glob.glob(os.path.join(claude_dir, "*", "*.jsonl"))):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        seen_here = set()       # a skill counts once per session (matches find_runs)
        day = None
        for line in lines:
            if day is None and '"timestamp"' in line:
                mt = re.search(r'"timestamp"\s*:\s*"(\d{4}-\d{2}-\d{2})', line)
                day = mt.group(1) if mt else None
            interesting = ("<command-name>" in line or '"Skill"' in line
                           or "subagent_type" in line)
            if not interesting:
                continue
            try:
                o = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            for mname in CMD_RE.findall(_user_text(o)):
                ml = mname.lower()
                if ml not in UTILITY_CMDS and ml not in seen_here:
                    seen_here.add(ml)
                    note(ml, "skill", day)
            content = ((o.get("message") or {}).get("content"))
            if isinstance(content, list):
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    if b.get("name") == "Skill":
                        sk = str((b.get("input") or {}).get("skill", "")).lower()
                        if sk and sk not in UTILITY_CMDS and sk not in seen_here:
                            seen_here.add(sk)
                            note(sk, "skill", day)
                    elif b.get("name") in ("Agent", "Task"):
                        st = str((b.get("input") or {}).get("subagent_type", "")).lower()
                        if st:
                            note(st, "agent", day)
    return inv


# ------------------------------------------------------------------ TRACE
def trace_run(span_lines, session, project, path):
    """Reconstruct one run's ordered Steps from its span of JSONL lines.

    Claude Code writes one line per content block, so a single API request spreads
    across several lines sharing a message id. Dedupe on that id or every call
    double-counts. tool_result payloads arrive on the following user turns; their
    byte size is attributed to the step that just requested them.
    """
    run = Run(session, project, path)
    seen_ids = set()
    cur = None                       # the step currently collecting injected results
    for line in span_lines:
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            run.parse_errors += 1    # counted so the report's "N skipped" caveat is honest
            continue
        if run.start_ts is None and o.get("timestamp"):
            run.start_ts = o.get("timestamp")
        msg = o.get("message")
        if not isinstance(msg, dict):
            continue

        # tool_result payloads (arrive as user turns): size -> current step
        content = msg.get("content")
        if isinstance(content, list) and cur is not None:
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    cur.injected_bytes += result_byte_equiv(b.get("content", ""))

        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        model = msg.get("model") or "claude-unknown"
        # Dedupe on the request id only. Never fall back to per-line `uuid`: it is unique
        # per JSONL line, so using it would make every content block a separate "call" and
        # defeat dedupe entirely (A7).
        mid = msg.get("id") or o.get("requestId")
        if mid is not None and mid in seen_ids:
            # same API call, another content block: still collect its tool_use names
            if cur is not None and isinstance(content, list):
                _collect_tools(content, cur)
            continue
        if mid is not None:
            seen_ids.add(mid)

        if model in SYNTHETIC_MODELS or model == "<synthetic>":
            run.synthetic_events += 1
            continue

        # One corrupt line must cost one line, never the whole scan: a single
        # non-numeric usage field in any historical file used to kill every audit.
        try:
            step = Step(len(run.steps), model, o.get("timestamp"))
            cc = usage.get("cache_creation")
            cc = cc if isinstance(cc, dict) else {}   # a non-dict shape must not crash (A8)
            cc5 = cc.get("ephemeral_5m_input_tokens")
            cc1 = cc.get("ephemeral_1h_input_tokens")
            if cc5 is None and cc1 is None:
                cc5 = usage.get("cache_creation_input_tokens", 0)
                cc1 = 0
            step.inp = _safe_int(usage.get("input_tokens"))
            step.cc5 = _safe_int(cc5)
            step.cc1 = _safe_int(cc1)
            step.ccr = _safe_int(usage.get("cache_read_input_tokens"))
            step.out = _safe_int(usage.get("output_tokens"))
            if isinstance(content, list):
                _collect_tools(content, step)
        except Exception:
            run.parse_errors += 1
            continue
        run.steps.append(step)
        cur = step
    return run


def _safe_int(v):
    """A usage counter, or 0 for any malformed value ('N/A', None, a list)."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _collect_tools(content, step):
    """Record tool names, Read basenames, and Agent/Task spawns on this step."""
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_use"):
            continue
        name = b.get("name") or ""
        step.tools.append(name)
        if name in ("Agent", "Task"):
            step.spawns_agent = True
        if name == "Read":
            fp = (b.get("input") or {}).get("file_path")
            if isinstance(fp, str) and fp:
                step.read_targets.append(os.path.basename(fp))


# ------------------------------------------------------------------ pricing
# Embedded fallback so a single copied gauntlet.py (or an install that did not ship the
# JSON) still runs. pricing.json is the editable source of truth; a test asserts the two
# never drift on their functional fields.
DEFAULT_PRICING = {
    "as_of": "2026-07-20",
    "cache_read_multiplier": 0.1,
    "cache_write_5m_multiplier": 1.25,
    "cache_write_1h_multiplier": 2.0,
    "models": [
        {"match": "fable", "label": "Claude Fable 5", "input": 10.0, "output": 50.0},
        {"match": "opus", "label": "Claude Opus", "input": 5.0, "output": 25.0},
        {"match": "sonnet", "label": "Claude Sonnet", "input": 3.0, "output": 15.0},
        {"match": "haiku", "label": "Claude Haiku", "input": 1.0, "output": 5.0},
        {"match": "codex", "label": "Codex model", "input": 1.25, "output": 10.0,
         "cache_read_multiplier": 0.1, "cache_write_5m_multiplier": 1.0,
         "cache_write_1h_multiplier": 1.0},
        {"match": "gpt-5", "label": "GPT-5 family", "input": 1.25, "output": 10.0,
         "cache_read_multiplier": 0.1, "cache_write_5m_multiplier": 1.0,
         "cache_write_1h_multiplier": 1.0},
    ],
    "fallback": {"label": "Unknown model (fallback rate)", "input": 5.0, "output": 25.0},
}


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_config(path, default, required=()):
    """Load a JSON config, or fall back to an embedded default if the file is absent,
    unreadable, or structurally wrong (missing a required key), so the tool runs even as a
    lone copied script or when handed a valid-JSON file of the wrong shape."""
    try:
        cfg = load_json(path)
    except (OSError, json.JSONDecodeError):
        return default
    if required and not (isinstance(cfg, dict) and all(k in cfg for k in required)):
        return default
    return cfg


def pricing_age_days(pricing, today):
    """Days between pricing.json's as_of and today, or None if either is unparseable.
    Used to warn when the rate card in the report is likely stale."""
    try:
        a = datetime.date.fromisoformat(pricing.get("as_of", ""))
        t = datetime.date.fromisoformat(today)
    except (ValueError, TypeError):
        return None
    return (t - a).days


def price_for(model, pricing):
    """First match wins, so pricing.json order IS priority. Bare family token for
    Claude (real 3.x ids interleave the version)."""
    ml = (model or "").lower()
    for p in pricing["models"]:
        if p["match"] in ml:
            return p, False
    return pricing["fallback"], True


def step_cost(step, pricing):
    """(estimated_dollars, dollars_without_caching) for one step."""
    p, _ = price_for(step.model, pricing)
    rd = p.get("cache_read_multiplier", pricing["cache_read_multiplier"])
    w5 = p.get("cache_write_5m_multiplier", pricing["cache_write_5m_multiplier"])
    w1 = p.get("cache_write_1h_multiplier", pricing["cache_write_1h_multiplier"])
    cost = (step.inp * p["input"] + step.out * p["output"]
            + step.ccr * p["input"] * rd
            + step.cc5 * p["input"] * w5 + step.cc1 * p["input"] * w1) / 1e6
    no_cache = (step.context_total * p["input"] + step.out * p["output"]) / 1e6
    return cost, no_cache


def run_totals(run, pricing):
    """Aggregate one run into a dict of totals."""
    t = {"steps": len(run.steps), "inp": 0, "cc5": 0, "cc1": 0, "ccr": 0,
         "out": 0, "cost": 0.0, "no_cache_cost": 0.0, "agent_spawns": 0,
         "fallback_calls": 0}
    for s in run.steps:
        for k in ("inp", "cc5", "cc1", "ccr", "out"):
            t[k] += getattr(s, k)
        c, nc = step_cost(s, pricing)
        t["cost"] += c
        t["no_cache_cost"] += nc
        if price_for(s.model, pricing)[1]:
            t["fallback_calls"] += 1
        if s.spawns_agent:
            t["agent_spawns"] += 1
    t["tokens"] = t["inp"] + t["cc5"] + t["cc1"] + t["ccr"] + t["out"]
    ctx_in = t["inp"] + t["cc5"] + t["cc1"] + t["ccr"]
    t["cache_hit"] = (t["ccr"] / ctx_in) if ctx_in else 0.0
    t["overhead"] = run.steps[0].context_total if run.steps else 0
    t["out_share"] = (t["out"] / t["tokens"]) if t["tokens"] else 0.0
    t["cr_series"] = [s.ccr for s in run.steps]
    return t


# ------------------------------------------------------------------ MEASURE
def _med_range(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"median": 0, "min": 0, "max": 0, "n": 0}
    return {"median": statistics.median(vals), "min": min(vals),
            "max": max(vals), "n": len(vals)}


def measure(runs, pricing, checklist=None):
    """Cross-run aggregate. Enforces the N>=floor rule for any average; the floor is
    checklist.json's min_runs_for_average (default 3), so that config key is live."""
    floor = (checklist or {}).get("min_runs_for_average", MIN_RUNS_FOR_AVERAGE)
    per = [run_totals(r, pricing) for r in runs]
    m = {"n_runs": len(runs),
         "enough_for_average": len(runs) >= floor,
         "avg_floor": floor,
         "per_run": per}
    m["steps"] = _med_range([p["steps"] for p in per])
    m["tokens"] = _med_range([p["tokens"] for p in per])
    m["cost"] = _med_range([p["cost"] for p in per])
    m["cache_hit"] = _med_range([p["cache_hit"] for p in per])
    # Standing overhead is measurable ONLY from runs that started their session:
    # a mid-session invocation's step-0 context includes prior unrelated work (a 9x
    # spread on real data), which is the session's history, not the skill's floor.
    fresh = [p["overhead"] for p, r in zip(per, runs) if not r.mid_session]
    m["overhead"] = _med_range(fresh)
    m["overhead_fresh_n"] = len(fresh)
    m["overhead_mid_n"] = len(runs) - len(fresh)
    m["out_share"] = _med_range([p["out_share"] for p in per])
    m["agent_spawns"] = _med_range([p["agent_spawns"] for p in per])
    m["fallback_calls"] = sum(p["fallback_calls"] for p in per)
    m["parse_errors"] = sum(r.parse_errors for r in runs)
    days = sorted(r.day for r in runs if r.day)
    m["window"] = (days[0], days[-1]) if days else (None, None)
    # representative run = the one whose step count is closest to the median
    if runs:
        target_steps = m["steps"]["median"]
        m["rep_idx"] = min(range(len(runs)),
                           key=lambda i: abs(per[i]["steps"] - target_steps))
    else:
        m["rep_idx"] = None
    return m


# ------------------------------------------------------------------ DIAGNOSE
def diagnose(runs, m, pricing, checklist, shared=False):
    """Run every waste check. Each returns fired|clean with evidence and, where the
    trace supports it, an estimated saving. Where it does not, we report the measured
    quantity and say the dollar figure is insufficiently determined, never invent it.

    `shared` mode suppresses any file basename from the finding text: the only check
    that would name a file is re_read, and a shared report must not carry basenames.
    The check still counts real re-reads; only the example name is withheld.
    """
    findings = []
    per = m["per_run"]
    # Honest language below the average floor: "median" may only be claimed with
    # enough runs; otherwise every cross-run number is labeled "observed" and the
    # check speaks about the runs it actually saw.
    qual = "median" if m["enough_for_average"] else f"observed (n={m['n_runs']})"
    # Thresholds come from checklist.json when present, else the module defaults, so the
    # config file is actually live (A2/B2). checklist may be {} (tests) -> defaults hold.
    ck = checklist or {}
    heavy_tok = ck.get("heavy_result_tokens", HEAVY_RESULT_TOKENS)
    mech_tok = ck.get("mechanical_out_tokens", MECHANICAL_OUT_TOKENS)
    big_ov_tok = ck.get("big_overhead_tokens", 60_000)
    lean_out = ck.get("lean_output_share", 0.06)
    simple_max = ck.get("simple_workflow_max_steps", 6)

    # 1. Re-read waste: same file Read 2+ times inside one run.
    total_rereads, ex = 0, None
    for r in runs:
        counts = {}
        for s in r.steps:
            for t in s.read_targets:
                counts[t] = counts.get(t, 0) + 1
        for name, c in counts.items():
            if c >= 2:
                total_rereads += (c - 1)
                # capture an example only for the internal report; a shared report
                # must never carry a real basename (a leak defeats --shared).
                if ex is None and not shared:
                    ex = (name, c)
    findings.append(_finding(
        "re_read", "Repeated file reads", total_rereads > 0,
        fired_msg=(f"{total_rereads} redundant file read(s) across {len(runs)} runs"
                   + (f"; e.g. {ex[0]} read {ex[1]}x in one run" if ex else "")),
        clean_msg="No file was read more than once within a run.",
        saving="Each avoided re-read saves the file's tokens plus one extra billed turn." if total_rereads else None))

    # 2. Cache breakage: cache_read collapses vs the prior step mid-run.
    breaks, rewarm_tokens, bx = 0, 0, None
    for pi, r in enumerate(runs):
        cr = per[pi]["cr_series"]
        # start at k=1 so the step0->step1 transition is checked; step 0's cache-read is
        # the standing floor and a collapse right after it is the most telling break (A3).
        for k in range(1, len(cr)):
            if cr[k - 1] > 50_000 and cr[k] < cr[k - 1] * 0.5:
                breaks += 1
                rewarm_tokens += r.steps[k].cache_write
                if bx is None:
                    bx = (r.steps[k].idx, cr[k - 1], cr[k])
    findings.append(_finding(
        "cache_break", "Cache invalidation mid-run", breaks > 0,
        fired_msg=(f"{breaks} step(s) where the cached prefix collapsed and was re-paid"
                   + (f"; first at step {bx[0]} ({bx[1]:,}->{bx[2]:,} cache-read tok)" if bx else "")),
        clean_msg="The cache prefix held across each run; no mid-run re-warm detected.",
        saving=(f"~{rewarm_tokens:,} cache-write tokens re-paid; stabilize prompt order to avoid."
                if rewarm_tokens else None)))

    # 3. Heavy recon in the main thread (a subagent digest would shrink it).
    heavy, heavy_tokens, hx = 0, 0, None
    for r in runs:
        for s in r.steps:
            if s.injected_tokens >= heavy_tok and not s.spawns_agent:
                heavy += 1
                heavy_tokens += s.injected_tokens
                if hx is None:
                    # tool names can identify a client/service (custom MCP servers);
                    # a shared report names no tools, same rule as basenames.
                    tools = "tool" if shared else (",".join(sorted(set(s.tools))) or "tool")
                    hx = (s.idx, s.injected_tokens, tools)
    est = int(heavy_tokens * 0.85)
    findings.append(_finding(
        "heavy_recon", "Heavy recon in main context", heavy > 0,
        fired_msg=(f"{heavy} tool result(s) over {heavy_tok:,} tok landed in main context"
                   + (f"; largest ~{hx[1]:,} tok at step {hx[0]} ({hx[2]})" if hx else "")),
        clean_msg="No oversized tool result hit the main context; recon stayed lean.",
        saving=(f"~{est:,} tokens recoverable across runs by moving that recon to a subagent that returns a digest."
                if heavy else None)))

    # 4. Model over-provisioning: premium tier doing mechanical (tiny-output) turns.
    over, over_saving, ox = 0, 0.0, None
    cheap_m = _cheapest_model(pricing)     # real cheapest model: use its OWN output rate (A4)
    cheap = cheap_m["input"]
    for r in runs:
        for s in r.steps:
            p, _ = price_for(s.model, pricing)
            if p["input"] > cheap * 1.5 and s.out and s.out < mech_tok:
                over += 1
                cur = step_cost(s, pricing)[0]
                alt = (s.context_total * cheap_m["input"] + s.out * cheap_m["output"]) / 1e6
                over_saving += max(0.0, cur - alt)
                if ox is None:
                    ox = (s.idx, p["label"], s.out)
    findings.append(_finding(
        "model_tier", "Premium model on mechanical turns", over > 0,
        fired_msg=(f"{over} premium-tier turn(s) produced under {mech_tok} output tokens"
                   + (f"; e.g. step {ox[0]} on {ox[1]} emitted {ox[2]} tok" if ox else "")),
        clean_msg="No expensive-tier call was doing trivial mechanical work.",
        saving=(f"~${over_saving:,.2f} across observed runs if those turns ran on {cheap_m['label']}."
                if over_saving > 0.01 else None)))

    # 5. Chatty turns: long chains of small single-tool steps (batching was possible).
    chatty, cx = 0, None
    for r in runs:
        streak = 0
        for s in r.steps:
            single = (s.out and s.out < mech_tok
                      and len([t for t in s.tools if t]) == 1)
            streak = streak + 1 if single else 0
            if streak >= 4:
                chatty += 1
                if cx is None:
                    cx = r.steps[max(0, s.idx - 3)].idx
                streak = 0
    findings.append(_finding(
        "chatty", "Chatty single-tool chains", chatty > 0,
        fired_msg=(f"{chatty} run-stretch(es) of 4+ tiny single-tool turns"
                   + (f"; first near step {cx}" if cx is not None else "")
                   + ". Each turn re-pays the standing context."),
        clean_msg="No long chain of tiny single-tool turns; tool calls were batched well.",
        saving="Batching independent tool calls into one turn removes repeated context re-reads." if chatty else None))

    # 6. Standing overhead (informational-with-teeth): the skill's per-invocation floor.
    # Measured ONLY from fresh-session runs; a mid-session invocation inherits the
    # session's prior context, which is not the skill's overhead. No fresh run means
    # the check is INCONCLUSIVE, and it says so; an inconclusive check never passes.
    ov = m["overhead"]
    mid_note = (f" ({m['overhead_mid_n']} mid-session run(s) excluded: their opening "
                f"context belongs to prior session work, not this skill)"
                if m["overhead_mid_n"] else "")
    if m["overhead_fresh_n"] == 0:
        findings.append(_finding(
            "overhead", "Standing overhead per invocation", False,
            fired_msg="", clean_msg=(
                f"cannot isolate: all {m['n_runs']} run(s) were invoked mid-session, so "
                f"step-0 context reflects prior work, not the skill's own load. Unverified, "
                f"not clean."), saving=None))
    else:
        big_overhead = ov["median"] >= big_ov_tok
        # the overhead sample is the FRESH runs only, so its qualifier keys off that
        # count, not the total run count
        ov_qual = ("median" if m["overhead_fresh_n"] >= m["avg_floor"]
                   else f"observed (n={m['overhead_fresh_n']})")
        findings.append(_finding(
            "overhead", "Standing overhead per invocation", big_overhead,
            fired_msg=(f"{ov_qual} first-call context is {ov['median']:,.0f} tokens "
                       f"(range {ov['min']:,}-{ov['max']:,}, from {m['overhead_fresh_n']} "
                       f"fresh-session run(s)){mid_note}: the system prompt, skill body, and "
                       f"any content pasted into the opening turn, re-read on every step. A large "
                       f"one-off paste in the first message inflates this, so confirm the driver "
                       f"before trimming skill files"),
            clean_msg=(f"first-call context is modest ({ov_qual} {ov['median']:,.0f} tok, from "
                       f"{m['overhead_fresh_n']} fresh-session run(s)){mid_note}."),
            saving=("Trim the skill's injected files / referenced docs, or avoid pasting large "
                    "inputs into the opening turn, to cut this floor." if big_overhead else None)))

    # 7. Output verbosity: output share of tokens well above a lean target.
    os_med = m["out_share"]["median"]
    verbose = os_med > lean_out
    findings.append(_finding(
        "verbosity", "Output verbosity", verbose,
        fired_msg=(f"{qual} output share is {os_med:.1%} of billed tokens (target under "
                   f"{lean_out:.0%}); output cannot be cached and bills ~5x input"),
        clean_msg=f"output share is lean ({qual} {os_med:.1%} of billed tokens).",
        saving="Tighter tool-use and terser turns cut the uncacheable output bill." if verbose else None))

    # 8. Workflow-shape verdict: is a multi-step workflow justified by the trace?
    steps_med = m["steps"]["median"]
    spawns_med = m["agent_spawns"]["median"]
    if steps_med <= simple_max and spawns_med == 0:
        shape_fired = True
        shape_msg = (f"{qual} {steps_med:.0f} steps, no subagents: this is a simple process. "
                     "A multi-stage workflow wrapper likely costs more than it saves here.")
    else:
        shape_msg = (f"{qual} {steps_med:.0f} steps with {spawns_med:.0f} subagent spawn(s): "
                     "genuinely multi-stage, so the workflow structure is justified.")
        shape_fired = False
    findings.append(_finding(
        "shape", "Workflow-shape verdict", shape_fired,
        fired_msg=shape_msg, clean_msg=shape_msg, saving=None))

    return findings


def _cheapest_model(pricing):
    """The model entry with the lowest input rate, used for the model_tier alternative
    cost so its OWN output rate is used, not a hardcoded ratio (A4)."""
    cands = list(pricing["models"]) + [pricing["fallback"]]
    return min(cands, key=lambda p: p["input"])


def _finding(key, title, fired, fired_msg, clean_msg, saving):
    return {"key": key, "title": title, "fired": bool(fired),
            "msg": fired_msg if fired else clean_msg, "saving": saving}


def recommendations(findings, m):
    """Rank fired findings into concrete recommendations, biggest lever first."""
    order = ["heavy_recon", "cache_break", "model_tier", "overhead", "re_read",
             "verbosity", "chatty", "shape"]
    recs = []
    fired = {f["key"]: f for f in findings if f["fired"]}
    for k in order:
        f = fired.get(k)
        if not f:
            continue
        recs.append({"title": _REC_TITLE.get(k, f["title"]),
                     "why": f["msg"], "saving": f["saving"]})
    return recs


_REC_TITLE = {
    "heavy_recon": "Move heavy recon into a subagent that returns a digest",
    "cache_break": "Stabilize prompt order so the cache prefix is not invalidated",
    "model_tier": "Demote mechanical turns to a cheaper model tier",
    "overhead": "Trim the skill's standing context (injected files and referenced docs)",
    "re_read": "Read each file once per run and reuse it",
    "verbosity": "Tighten output; fewer, terser turns",
    "chatty": "Batch independent tool calls into single turns",
    "shape": "Consider dropping the workflow wrapper for this simple process",
}


# ------------------------------------------------------------------ leak guard (shared)
def _identifying(name):
    """True if a file basename could actually identify a person/client/deal, so it is
    worth guarding against in a shared report. A bare lowercase word (an extension-less
    script like `run`, `main`, `build`) is not identifying and collides with the report's
    own vocabulary, so it is excluded from the guard (redaction still blanks it in the
    output). Anything with an extension, digit, separator, uppercase, or real length is
    identifying: report_q3.xlsx, ACME, deed_p0.png, 123-main-st.jpg."""
    if not name:
        return False
    if any(c in name for c in "._-") or any(c.isdigit() or c.isupper() for c in name):
        return True
    return len(name) >= 12   # a long all-lowercase name can still identify


def assert_no_leak(text, forbidden):
    """Raises if a forbidden string reached the output. The test suite proves this
    FAILS on a known-bad artifact before it is trusted."""
    for s in forbidden:
        if s and s in text:
            raise AssertionError("content leak: a forbidden string reached the report")


# Built-in Claude Code tools carry no user-identifying information. Anything else
# (mcp__<server>__<tool>, custom names) can name a client, deal, or service, so a
# shared report aliases them, same rule as file basenames.
BUILTIN_TOOLS = {
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "Grep", "Glob",
    "Task", "Agent", "Skill", "WebFetch", "WebSearch", "TodoWrite", "TaskCreate",
    "TaskUpdate", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "SendMessage",
}


def redact_for_share(m, runs):
    """Alias project names, drop file basenames, and alias non-builtin tool names
    for a shareable report. Returns the set of original non-builtin tool names so
    the caller can add them to the leak guard's forbidden list."""
    alias, tool_alias, hidden_tools = {}, {}, set()
    for r in runs:
        if r.project not in alias:
            alias[r.project] = f"project-{len(alias) + 1}"
        r.project = alias[r.project]
        for s in r.steps:
            s.read_targets = ["(file)" for _ in s.read_targets]
            renamed = []
            for t in s.tools:
                if t in BUILTIN_TOOLS:
                    renamed.append(t)
                else:
                    hidden_tools.add(t)
                    if t not in tool_alias:
                        tool_alias[t] = f"tool-{len(tool_alias) + 1}"
                    renamed.append(tool_alias[t])
            s.tools = renamed
    return hidden_tools


# ------------------------------------------------------------------ RENDER
def fmt_tok(x):
    x = int(x)
    if x >= 1e9:
        return f"{x / 1e9:.2f}B"
    if x >= 1e6:
        return f"{x / 1e6:.1f}M"
    if x >= 1e3:
        return f"{x / 1e3:.0f}k"
    return f"{x:,}"


def fmt_usd(x):
    return f"${x:,.2f}"


# The bespoke design system, authored by the design subagent (2026-07-18). The design
# reference (design_mock.html) lives with the project docs, not in this folder; this
# CSS constant is the single shipped source of truth. Self-contained, offline-safe
# (system fonts only), single dark theme, responsive.
CSS = """
:root {
  --navy: #122a46; --navy-deep: #1b3a5c; --gold: #c9a227; --gold-soft: #d8b855;
  --ink: #070d16; --ink-2: #0b1524; --ink-3: #0f1c30; --ink-4: #13263e;
  --hairline: #21344c; --hairline-2: #2c435f;
  --ember: #e08c3a; --ember-hot: #f0a94f; --ember-deep: #b5652233;
  --fired: #e0623a; --fired-glow: #e0623a26; --clean: #5fa88a; --clean-glow: #5fa88a1f;
  --text: #eaf0f7; --text-dim: #a9bad0; --text-mute: #6d829c; --text-faint: #47597219;
  --serif: Georgia, 'Times New Roman', 'Iowan Old Style', 'Palatino Linotype', Palatino, serif;
  --sans: ui-sans-serif, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  --mono: ui-monospace, 'SF Mono', 'Cascadia Mono', 'Segoe UI Mono', Consolas, 'Liberation Mono', monospace;
  --maxw: 1180px; --radius: 3px;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background:
    radial-gradient(1200px 620px at 78% -8%, #14304e2e, transparent 60%),
    radial-gradient(900px 500px at 8% 4%, #1b3a5c1f, transparent 55%), var(--ink);
  color: var(--text); font-family: var(--sans); font-size: 15px; line-height: 1.55;
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
}
.wrap { max-width: var(--maxw); margin: 0 auto; padding: 0 28px; }
h1, h2, h3 { font-family: var(--serif); font-weight: 600; letter-spacing: 0.2px; margin: 0; }
a { color: var(--gold-soft); text-decoration: none; }
.num { font-family: var(--sans); font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1; }
.mono { font-family: var(--mono); }
section { padding: 46px 0; border-top: 1px solid var(--hairline); }
section:first-of-type { border-top: none; }
.eyebrow { font-family: var(--sans); font-size: 11px; font-weight: 700; letter-spacing: 3px;
  text-transform: uppercase; color: var(--gold); display: inline-flex; align-items: center; gap: 10px; }
.eyebrow::before { content: ""; width: 22px; height: 1px; background: var(--gold); opacity: .8; }
.section-title { font-size: 25px; color: var(--text); margin: 14px 0 4px; }
.section-sub { color: var(--text-mute); font-size: 13.5px; max-width: 640px; }
.masthead { position: relative; padding: 40px 0 40px; border-bottom: 1px solid var(--hairline); overflow: hidden; }
.masthead::after { content: ""; position: absolute; left: 0; right: 0; bottom: -1px; height: 1px;
  background: linear-gradient(90deg, transparent, var(--gold) 22%, var(--gold) 78%, transparent); opacity: .55; }
.brandbar { display: flex; align-items: center; justify-content: space-between; gap: 16px;
  flex-wrap: wrap; padding-top: 26px; margin-bottom: 30px; }
.brandmark { display: flex; align-items: center; gap: 14px; }
.brandmark .seal { width: 40px; height: 40px; flex: 0 0 40px; }
.brandmark .wordmark { font-family: var(--serif); font-size: 20px; letter-spacing: 5px; color: var(--text); font-weight: 600; }
.brandmark .wordmark b { color: var(--gold); font-weight: 600; }
.brand-tag { font-size: 10.5px; letter-spacing: 2.5px; text-transform: uppercase; color: var(--text-mute);
  border-left: 1px solid var(--hairline); padding-left: 14px; }
.masthead-meta { text-align: right; font-size: 11.5px; color: var(--text-mute); letter-spacing: .5px; }
.masthead-meta .num { color: var(--text-dim); }
.hero-grid { display: grid; grid-template-columns: 1.35fr 1fr; gap: 40px; align-items: end; }
.skill-line { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
.skill-name { font-family: var(--mono); font-size: 46px; font-weight: 500; letter-spacing: -1px; color: var(--text); line-height: 1; }
.skill-name .slash { color: var(--gold); }
.skill-kind { font-size: 11px; letter-spacing: 2px; text-transform: uppercase; color: var(--text-mute);
  border: 1px solid var(--hairline-2); border-radius: 100px; padding: 4px 12px; }
.verdict { font-family: var(--serif); font-size: 21px; line-height: 1.42; color: var(--text-dim); margin-top: 20px; max-width: 560px; }
.verdict b { color: var(--gold-soft); font-weight: 600; }
.verdict .flagcount { color: var(--ember-hot); font-weight: 600; }
.hero-right { display: flex; flex-direction: column; gap: 12px; align-items: flex-end; }
.grade { display: inline-flex; align-items: center; gap: 14px; border: 1px solid var(--hairline-2);
  border-radius: var(--radius); padding: 12px 18px; background: linear-gradient(180deg, #10203608, #0000); }
.grade .glabel { font-size: 10.5px; letter-spacing: 2px; text-transform: uppercase; color: var(--text-mute); }
.grade .glabel .gnote { display: block; font-size: 8.5px; letter-spacing: 1px; color: var(--text-mute); margin-top: 3px; }
.grade .gval { font-family: var(--serif); font-size: 34px; color: var(--gold); line-height: 1; }
.grade .gval small { font-size: 15px; color: var(--text-mute); }
.gradekey { display: block; font-size: 10px; color: var(--text-mute); margin-top: 8px; line-height: 1.4; opacity: .85; }
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; margin-top: 34px;
  background: var(--hairline); border: 1px solid var(--hairline); border-radius: var(--radius); overflow: hidden; }
.stat { background: linear-gradient(180deg, var(--ink-3), var(--ink-2)); padding: 20px 20px 18px; position: relative; }
.stat::before { content: ""; position: absolute; top: 0; left: 0; width: 26px; height: 2px; background: var(--gold); opacity: .8; }
.stat .k { font-size: 10.5px; letter-spacing: 1.6px; text-transform: uppercase; color: var(--text-mute); }
.stat .v { font-size: 34px; font-weight: 600; letter-spacing: -.5px; color: var(--text); margin-top: 10px; line-height: 1; }
.stat .v .u { font-size: 15px; color: var(--text-mute); font-weight: 500; margin-left: 2px; }
.stat .sub { font-size: 11.5px; color: var(--text-mute); margin-top: 7px; }
.stat.accent .v { color: var(--ember-hot); }
.stat.good .v { color: var(--clean); }
.table-shell { border: 1px solid var(--hairline); border-radius: var(--radius); overflow: hidden; background: var(--ink-2); }
.mini { border-collapse: collapse; font-size: 13px; }
.mini td { padding: 6px 18px 6px 0; color: var(--text-dim); border-bottom: 1px solid #182a40; }
.mini td:first-child { color: var(--text-mute); text-transform: uppercase; font-size: 10.5px; letter-spacing: 1.2px; }
.mini td.num { font-variant-numeric: tabular-nums; color: var(--text); }
.econ { width: 100%; border-collapse: collapse; font-size: 13.5px; }
.econ thead th { font-family: var(--sans); font-size: 10.5px; letter-spacing: 1.5px; text-transform: uppercase;
  color: var(--text-mute); font-weight: 700; text-align: right; padding: 14px 18px; background: var(--ink-3); border-bottom: 1px solid var(--hairline); }
.econ thead th:first-child, .econ tbody td:first-child { text-align: left; }
.econ tbody td { padding: 12px 18px; text-align: right; border-bottom: 1px solid #182a40; color: var(--text-dim); }
.econ tbody tr:hover td { background: var(--ink-4); }
.econ .runid { color: var(--text); font-family: var(--mono); font-size: 12.5px; letter-spacing: .5px; }
.econ .runid .dot { display:inline-block; width:6px; height:6px; border-radius:50%; background:var(--gold); margin-right:9px; vertical-align: middle; opacity:.7; }
.econ td.tok { color: var(--text); font-variant-numeric: tabular-nums; }
.econ td.cost { color: var(--ember); font-variant-numeric: tabular-nums; }
.cachecell { display: inline-flex; align-items: center; gap: 10px; justify-content: flex-end; }
.cachebar { width: 68px; height: 5px; border-radius: 3px; background: #1a2c43; overflow: hidden; }
.cachebar > i { display: block; height: 100%; background: linear-gradient(90deg, var(--gold), var(--gold-soft)); }
.cacheval { color: var(--gold-soft); font-variant-numeric: tabular-nums; width: 42px; text-align: right; }
.econ tfoot td { padding: 15px 18px; text-align: right; border-top: 2px solid var(--hairline-2);
  background: var(--ink-3); color: var(--text); font-weight: 600; }
.econ tfoot td:first-child { text-align: left; font-family: var(--serif); font-size: 13px; letter-spacing: 1px; text-transform: uppercase; color: var(--gold); }
.econ tfoot .rng { color: var(--text-mute); font-weight: 400; font-size: 11.5px; display: block; margin-top: 2px; }
.trace-legend { display: flex; gap: 22px; flex-wrap: wrap; margin: 18px 0 8px; font-size: 11.5px; color: var(--text-mute); }
.trace-legend span { display: inline-flex; align-items: center; gap: 7px; }
.lg-swatch { width: 13px; height: 13px; border-radius: 2px; display: inline-block; }
.lg-line { width: 20px; height: 0; border-top: 2px solid var(--gold); display: inline-block; }
.lg-line.branch { border-top: 2px dashed var(--ember); }
.trace-scroll { overflow-x: auto; overflow-y: hidden; border: 1px solid var(--hairline); border-radius: var(--radius);
  background: linear-gradient(180deg, var(--ink-2), var(--ink)); padding: 6px; -webkit-overflow-scrolling: touch; }
.trace-scroll::-webkit-scrollbar { height: 10px; }
.trace-scroll::-webkit-scrollbar-track { background: #0a1320; }
.trace-scroll::-webkit-scrollbar-thumb { background: #26405e; border-radius: 6px; }
.trace-scroll svg { display: block; }
.scroll-hint { font-size: 11px; color: var(--text-mute); margin-top: 8px; letter-spacing: .5px; }
.curve-card { border: 1px solid var(--hairline); border-radius: var(--radius);
  background: linear-gradient(180deg, var(--ink-2), var(--ink)); padding: 8px 8px 4px; }
.curve-card svg { display: block; width: 100%; height: auto; }
.curve-notes { display: flex; gap: 26px; flex-wrap: wrap; margin-top: 14px; }
.curve-note { font-size: 12px; color: var(--text-mute); max-width: 320px; }
.curve-note b { color: var(--text-dim); font-weight: 600; }
.flag-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.flag { border: 1px solid var(--hairline); border-radius: var(--radius); background: var(--ink-3);
  padding: 20px 22px; position: relative; overflow: hidden; }
.flag::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; }
.flag.fired::before { background: var(--fired); }
.flag.clean::before { background: var(--clean); }
.flag.fired { background: linear-gradient(180deg, var(--fired-glow), transparent 60%), var(--ink-3); }
.flag-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.flag-name { font-family: var(--serif); font-size: 16.5px; color: var(--text); }
.flag-badge { font-size: 10px; font-weight: 700; letter-spacing: 1.6px; text-transform: uppercase;
  padding: 4px 10px; border-radius: 100px; white-space: nowrap; }
.flag.fired .flag-badge { color: var(--fired); border: 1px solid var(--fired); background: #e0623a12; }
.flag.clean .flag-badge { color: var(--clean); border: 1px solid var(--clean); background: var(--clean-glow); }
.flag-body { font-size: 13px; color: var(--text-dim); line-height: 1.5; }
.flag-body .mono { color: var(--gold-soft); font-size: 12px; }
.flag-save { margin-top: 14px; padding-top: 13px; border-top: 1px solid var(--hairline);
  display: flex; align-items: baseline; gap: 8px; font-size: 12px; color: var(--text-mute); }
.flag-save .amt { font-size: 15px; color: var(--ember-hot); font-weight: 600; font-variant-numeric: tabular-nums; }
.flag.clean .flag-save .amt { color: var(--clean); }
.flag.span2 { grid-column: 1 / -1; }
.rec-list { display: flex; flex-direction: column; gap: 14px; }
.rec { display: grid; grid-template-columns: 58px 1fr auto; gap: 22px; align-items: center;
  border: 1px solid var(--hairline); border-radius: var(--radius);
  background: linear-gradient(90deg, var(--ink-3), var(--ink-2)); padding: 18px 22px; position: relative; }
.rec:hover { border-color: var(--hairline-2); }
.rec-rank { font-family: var(--serif); font-size: 40px; color: var(--gold); line-height: 1; text-align: center; opacity: .9; }
.rec-rank small { display: block; font-family: var(--sans); font-size: 9px; letter-spacing: 2px; color: var(--text-mute); text-transform: uppercase; margin-top: 4px; }
.rec-main .rt { font-family: var(--serif); font-size: 17px; color: var(--text); margin-bottom: 5px; }
.rec-main .rr { font-size: 13px; color: var(--text-dim); }
.rec-main .rr .mono { color: var(--gold-soft); font-size: 12px; }
.rec-save { text-align: right; border-left: 1px solid var(--hairline); padding-left: 22px; min-width: 150px; }
.rec-save .amt { font-size: 20px; font-weight: 600; color: var(--ember-hot); font-variant-numeric: tabular-nums; line-height: 1.1; }
.rec-save .lbl { font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; color: var(--text-mute); margin-top: 4px; }
.rec-total { text-align: right; margin-top: 16px; font-size: 13px; color: var(--text-mute); }
.rec-total b { color: var(--ember-hot); font-weight: 600; font-size: 15px; }
.foot { border-top: 1px solid var(--hairline); margin-top: 12px; padding: 34px 0 60px; }
.foot-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 40px; }
.foot h4 { font-family: var(--serif); font-size: 12.5px; letter-spacing: 1px; text-transform: uppercase; color: var(--gold); margin-bottom: 10px; }
.foot p { font-size: 11.5px; color: var(--text-mute); line-height: 1.6; margin: 0 0 10px; }
.foot .owner { font-family: var(--serif); font-size: 15px; color: var(--text-dim); }
.foot .owner b { color: var(--gold-soft); font-weight: 600; }
.privacy { border: 1px solid var(--hairline); border-left: 3px solid var(--gold); border-radius: var(--radius);
  padding: 14px 16px; background: #0d1b2c; font-size: 11.5px; color: var(--text-dim); line-height: 1.55; }
.privacy b { color: var(--text); }
@media (max-width: 900px) {
  .hero-grid { grid-template-columns: 1fr; gap: 24px; }
  .hero-right { align-items: flex-start; }
  .stat-row { grid-template-columns: repeat(2, 1fr); }
  .flag-grid { grid-template-columns: 1fr; }
  .foot-grid { grid-template-columns: 1fr; }
  .rec { grid-template-columns: 44px 1fr; }
  .rec-save { grid-column: 1 / -1; text-align: left; border-left: none; border-top: 1px solid var(--hairline); padding: 12px 0 0; }
  .skill-name { font-size: 36px; }
}
@media (max-width: 540px) {
  .stat-row { grid-template-columns: 1fr; }
  .wrap { padding: 0 18px; }
  .econ thead { display: none; }
}
.flag.span2 { grid-column: 1 / -1; }
"""

SEAL_SVG = ("<svg class='seal' viewBox='0 0 40 40' fill='none' aria-hidden='true'>"
            "<circle cx='20' cy='20' r='18.5' stroke='#c9a227' stroke-width='1'/>"
            "<circle cx='20' cy='20' r='14' stroke='#c9a227' stroke-width='0.6' opacity='0.5'/>"
            "<path d='M20 7 L31 26 H9 Z' stroke='#c9a227' stroke-width='1.1' fill='#c9a22712'/>"
            "<path d='M20 15 L20 33 M13 33 L27 33' stroke='#c9a227' stroke-width='1.1'/></svg>")


MODEL_STYLE = {  # badge + node accent per model family (label from pricing)
    "opus": ("OPUS", "#8878ff", "#b9b0ff", "#6d5cff22"),
    "fable": ("FABLE", "#c9a227", "#d8b855", "#c9a22722"),
    "sonnet": ("SONNET", "#4ba3c7", "#a9d8ea", "#4ba3c722"),
    "haiku": ("HAIKU", "#5fa88a", "#bfe3d4", "#5fa88a22"),
}


def _model_style(label):
    ll = (label or "").lower()
    for k, v in MODEL_STYLE.items():
        if k in ll:
            return v
    return (label[:6].upper() if label else "MODEL", "#7f93ad", "#c7d3e0", "#7f93ad22")


def models_present(runs, pricing):
    """Ordered, de-duped list of model styles actually seen across the runs, so the
    trace legend shows only the families in the report (not a hardcoded pair)."""
    seen, out = set(), []
    for r in runs:
        for s in r.steps:
            badge, color, _light, bg = _model_style(price_for(s.model, pricing)[0]["label"])
            if badge not in seen:
                seen.add(badge)
                out.append((badge, color, bg))
    return out


def svg_trace(run, pricing, cap=14):
    """Horizontal execution-flow trace. Each step is a node card: model badge, tokens,
    a cache-read bar whose width grows with context, and its primary tool. A step that
    spawns a subagent is marked with an ember branch. Coordinates and viewBox are
    regenerated from the node count so any run length renders cleanly."""
    steps = run.steps[:cap]
    n = len(steps)
    pitch, nw, nh, top = 150, 120, 92, 104
    width = 30 + n * pitch + 210
    height = 460
    maxccr = max((s.ccr for s in steps), default=1) or 1
    out = [f"<svg viewBox='0 0 {width} {height}' width='{width}' height='{height}' "
           f"xmlns='http://www.w3.org/2000/svg'>",
           "<defs>"
           "<linearGradient id='nodeGrad' x1='0' y1='0' x2='0' y2='1'>"
           "<stop offset='0' stop-color='#12233a'/><stop offset='1' stop-color='#0c1a2c'/></linearGradient>"
           "<linearGradient id='cacheFill' x1='0' y1='0' x2='1' y2='0'>"
           "<stop offset='0' stop-color='#b56522'/><stop offset='1' stop-color='#e08c3a'/></linearGradient>"
           "</defs>"]
    for i, s in enumerate(steps):
        x = 30 + i * pitch
        label, accent, badge_txt_col, badge_fill = _model_style(price_for(s.model, pricing)[0]["label"])
        if i:
            px = 30 + (i - 1) * pitch + nw
            out.append(f"<line x1='{px}' y1='{top+nh/2}' x2='{x}' y2='{top+nh/2}' "
                       f"stroke='#2c435f' stroke-width='1.4'/>")
        out.append(f"<rect x='{x}' y='{top}' width='{nw}' height='{nh}' rx='4' "
                   f"fill='url(#nodeGrad)' stroke='#2c435f'/>")
        out.append(f"<rect x='{x}' y='{top}' width='{nw}' height='3' fill='{accent}' opacity='0.85'/>")
        tools = [t for t in dict.fromkeys(s.tools) if t]
        primary = tools[0] if tools else "turn"
        out.append(f"<text x='{x+11}' y='{top+19}' font-family='ui-monospace,monospace' "
                   f"font-size='11' fill='#6d829c'>{s.idx:02d}</text>")
        out.append(f"<text x='{x+nw-11}' y='{top+19}' text-anchor='end' "
                   f"font-family='ui-monospace,monospace' font-size='8.5' fill='#6d829c'>{esc(primary[:11])}</text>")
        name = esc((s.read_targets[0] if s.read_targets else primary)[:15])  # truncate then escape (B5)
        out.append(f"<text x='{x+11}' y='{top+40}' font-family='Georgia,serif' font-size='12' fill='#eaf0f7'>{name}</text>")
        out.append(f"<rect x='{x+11}' y='{top+48}' width='46' height='15' rx='7' fill='{badge_fill}' stroke='{accent}'/>")
        out.append(f"<text x='{x+34}' y='{top+59}' text-anchor='middle' font-family='ui-sans-serif' "
                   f"font-size='9' fill='{badge_txt_col}'>{label}</text>")
        out.append(f"<text x='{x+11}' y='{top+78}' font-family='ui-sans-serif' font-size='9.5' "
                   f"fill='#a9bad0'>in {fmt_tok(s.context_total)} · out {fmt_tok(s.out)}</text>")
        bw = int(98 * (s.ccr / maxccr))
        out.append(f"<rect x='{x+11}' y='{top+82}' width='98' height='6' rx='3' fill='#12233a'/>")
        out.append(f"<rect x='{x+11}' y='{top+82}' width='{max(2,bw)}' height='6' rx='3' "
                   f"fill='url(#cacheFill)' stroke='#e08c3a' stroke-width='0.5'/>")
        if s.spawns_agent:
            by = top + nh + 26
            out.append(f"<line x1='{x+nw/2}' y1='{top+nh}' x2='{x+nw/2}' y2='{by}' "
                       f"stroke='#e08c3a' stroke-width='1.4' stroke-dasharray='4 3'/>")
            out.append(f"<rect x='{x+18}' y='{by}' width='{nw-36}' height='30' rx='4' "
                       f"fill='#1a1206' stroke='#e08c3a' stroke-width='1'/>")
            out.append(f"<text x='{x+nw/2}' y='{by+19}' text-anchor='middle' font-family='ui-sans-serif' "
                       f"font-size='9' fill='#f0a94f'>SUBAGENT</text>")
    # terminal marker
    tx = 30 + n * pitch + 20
    out.append(f"<circle cx='{tx}' cy='{top+nh/2}' r='10' fill='#0e1830' stroke='#c9a227' stroke-width='1.4'/>")
    out.append(f"<path d='M{tx-5} {top+nh/2} l4 4 l7 -8' stroke='#c9a227' stroke-width='1.6' fill='none'/>")
    out.append(f"<text x='{tx}' y='{top+nh/2+32}' text-anchor='middle' font-family='ui-sans-serif' font-size='9' fill='#6d829c'>done</text>")
    # accumulation ribbon
    out.append(f"<text x='30' y='{height-14}' font-family='ui-sans-serif' font-size='10.5' "
               f"fill='#6d829c' letter-spacing='1'>CACHE READ ACCUMULATION  →  {fmt_tok(steps[0].ccr) if steps else 0}</text>")
    out.append(f"<text x='{width-20}' y='{height-14}' text-anchor='end' font-family='ui-sans-serif' "
               f"font-size='10.5' fill='#e08c3a' letter-spacing='1'>{fmt_tok(maxccr)} peak context</text>")
    out.append("</svg>")
    return "".join(out)


def svg_growth(run):
    """Context-accumulation area curve: cache_read per step, with a standing-overhead
    floor band. y-axis auto-scales to a round ceiling above the peak so points never clip."""
    cr = [s.ccr for s in run.steps] or [0]
    n = len(cr)
    peak = max(cr) or 1
    # round the ceiling up to a clean 100k multiple (min 100k)
    ceil = max(100_000, int((peak + 99_999) // 100_000) * 100_000)
    x0, x1, y0, y1 = 70, 960, 40, 280  # plot frame; y inverted
    def X(i):
        return x0 + (x1 - x0) * (i / max(1, n - 1))
    def Y(v):
        return y1 - (y1 - y0) * (v / ceil)
    line = " ".join(f"{'M' if i == 0 else 'L'}{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(cr))
    area = (f"M{X(0):.1f},{Y(cr[0]):.1f} "
            + " ".join(f"L{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(cr))
            + f" L{x1:.1f},{y1} L{x0:.1f},{y1} Z")
    floor_v = min(cr)
    fy = Y(floor_v)
    grid, labels = [], []
    steps_axis = 4
    for k in range(steps_axis + 1):
        gv = ceil * k / steps_axis
        gy = Y(gv)
        grid.append(f"<line x1='{x0}' y1='{gy:.1f}' x2='{x1}' y2='{gy:.1f}'/>")
        labels.append(f"<text x='60' y='{gy+4:.1f}'>{fmt_tok(gv)}</text>")
    pts = "".join(f"<circle cx='{X(i):.1f}' cy='{Y(v):.1f}' r='3' fill='#0b1524' "
                  f"stroke='#f0a94f' stroke-width='2'/>" for i, v in enumerate(cr))
    return (f"<svg viewBox='0 0 1000 340' xmlns='http://www.w3.org/2000/svg' role='img' "
            f"aria-label='Context growth curve' preserveAspectRatio='xMidYMid meet'>"
            f"<defs><linearGradient id='areaFill' x1='0' y1='0' x2='0' y2='1'>"
            f"<stop offset='0' stop-color='#e08c3a' stop-opacity='0.34'/>"
            f"<stop offset='1' stop-color='#e08c3a' stop-opacity='0.02'/></linearGradient>"
            f"<linearGradient id='floorFill' x1='0' y1='0' x2='0' y2='1'>"
            f"<stop offset='0' stop-color='#c9a227' stop-opacity='0.14'/>"
            f"<stop offset='1' stop-color='#c9a227' stop-opacity='0.02'/></linearGradient></defs>"
            f"<g stroke='#182a40' stroke-width='1'>{''.join(grid)}</g>"
            f"<g font-family='ui-sans-serif' font-size='10' fill='#6d829c' text-anchor='end'>{''.join(labels)}</g>"
            f"<line x1='{x0}' y1='{fy:.1f}' x2='{x1}' y2='{fy:.1f}' stroke='#c9a227' stroke-width='1' "
            f"stroke-dasharray='4 4' opacity='0.8'/>"
            f"<text x='{x0+6}' y='{fy-6:.1f}' font-family='ui-sans-serif' font-size='10' fill='#d8b855'>"
            f"Standing-overhead floor · {fmt_tok(floor_v)} (re-read every step)</text>"
            f"<path fill='url(#areaFill)' stroke='none' d='{area}'/>"
            f"<path fill='none' stroke='#f0a94f' stroke-width='2.4' stroke-linejoin='round' "
            f"stroke-linecap='round' d='{line}'/>{pts}</svg>")


def efficiency_grade(m, findings):
    """A letter grade from cache efficiency and how many checks fired. Heuristic, and
    labeled as such in the report."""
    fired = sum(1 for f in findings if f["fired"] and f["key"] != "shape")
    cache = m["cache_hit"]["median"]
    score = 100
    score -= fired * 8
    if cache < 0.90:
        score -= (0.90 - cache) * 200
    score -= max(0, (m["out_share"]["median"] - 0.06)) * 100
    score = max(40, min(100, score))
    table = [(93, "A"), (90, "A-"), (87, "B+"), (83, "B"), (80, "B-"),
             (77, "C+"), (73, "C"), (70, "C-"), (60, "D"), (0, "F")]
    for thr, g in table:
        if score >= thr:
            return g
    return "F"


def _baseline_html(baseline):
    """A compact before/after strip for the report when --baseline was passed."""
    if not baseline:
        return ""
    labels = {"tokens": "tokens", "steps": "steps", "cost": "cost", "cache_hit": "cache hit"}
    cells = []
    for k in ("tokens", "steps", "cost", "cache_hit"):
        d = baseline["metrics"].get(k)
        if not d:
            continue
        if k == "cost":
            old_s, new_s = fmt_usd(d["old"]), fmt_usd(d["new"])
        elif k == "cache_hit":
            old_s, new_s = f"{d['old']*100:.0f}%", f"{d['new']*100:.0f}%"
        elif k == "tokens":
            old_s, new_s = fmt_tok(d["old"]), fmt_tok(d["new"])
        else:
            old_s, new_s = f"{d['old']:.0f}", f"{d['new']:.0f}"
        pct = d["pct"]
        # for tokens/cost/steps, lower is better (green); for cache_hit, higher is better
        better = (d["diff"] < 0) if k != "cache_hit" else (d["diff"] > 0)
        color = "var(--good, #6bd08e)" if better else "var(--ember-hot, #ff8a5c)"
        chg = (f"<span style='color:{color}'>{'+' if pct and pct > 0 else ''}"
               f"{pct*100:.0f}%</span>" if pct is not None else "&mdash;")
        cells.append(f"<td>{esc(labels[k])}</td><td class='num'>{esc(old_s)} &rarr; "
                     f"{esc(new_s)}</td><td class='num'>{chg}</td>")
    flag_bits = []
    if baseline["cleared"]:
        flag_bits.append("cleared " + ", ".join(esc(x) for x in baseline["cleared"]))
    if baseline["regressed"]:
        flag_bits.append("new " + ", ".join(esc(x) for x in baseline["regressed"]))
    flags = ("; ".join(flag_bits)) if flag_bits else "no change in fired flags"
    rows = "".join(f"<tr>{c}</tr>" for c in cells)
    pw = baseline.get("prior_window")
    since = (f" (baseline through {esc(str(pw[1]))})" if pw and pw[1] else "")
    return (f"<section class='wrap' style='margin-top:26px'>"
            f"<div class='eyebrow'>Since baseline{since}</div>"
            f"<table class='mini' style='margin-top:10px'><tbody>{rows}</tbody></table>"
            f"<p class='section-sub' style='margin-top:8px'>Flags: {flags}.</p></section>")


def render(target, m, runs, findings, recs, pricing, shared=False,
           owner="", site="", is_agent=False, checklist=None, dollars_first=False,
           baseline=None):
    # GAUNTLET_TODAY lets the sample generator pin a fixed date so the committed
    # docs/sample_report.html is reproducible (no dirty tree when CI regenerates it).
    today = os.environ.get("GAUNTLET_TODAY") or datetime.date.today().isoformat()
    stale_days = checklist.get("pricing_stale_days", 90) if checklist else 90
    age = pricing_age_days(pricing, today)
    pricing_warn = (f" The bundled rate card is dated {esc(str(pricing.get('as_of', '?')))}, "
                    f"about {age} days old; verify pricing.json against the current provider "
                    f"rate card before quoting any dollar figure."
                    if age is not None and age > stale_days else "")
    n = m["n_runs"]
    sig = "@" if is_agent else "/"
    doc_open = (f"<!doctype html><meta charset='utf-8'>"
                f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
                f"<title>GAUNTLET · {sig}{esc(target)}</title><style>{CSS}</style>")
    if n == 0:
        return (f"{doc_open}<header class='masthead'><div class='wrap'>"
                f"<h1 class='section-title'>GAUNTLET</h1>"
                f"<p class='section-sub'>No historical run of {sig}{esc(target)} was found "
                f"in the local transcripts. Nothing was fabricated. Run <span class='mono'>"
                f"--list</span> to see every auditable skill and agent with run counts, "
                f"check the name, or point <span class='mono'>--claude-dir</span> at your "
                f"transcript root.</p></div></header>")

    w0, w1 = m["window"]
    per = m["per_run"]
    fired_n = sum(1 for f in findings if f["fired"] and f["key"] != "shape")
    shape = next(f for f in findings if f["key"] == "shape")
    grade = efficiency_grade(m, findings)
    kind = "Simple process" if shape["fired"] else "Multi-stage workflow"

    # combined savings across fired recs, for the hero + rec total
    combined = sum(_saving_usd(r["saving"]) for r in recs)
    med_cost = m["cost"]["median"] or 1
    pct = combined / med_cost if med_cost else 0

    # Tokens lead by default: on a flat subscription the token/context budget is the scarce
    # resource, and the dollar figure is counterfactual. --dollars restores cost as the lead.
    tok_cls = "stat" if dollars_first else "stat accent"
    cost_cls = "stat accent" if dollars_first else "stat"
    cost_sub = ("range {a} &ndash; {b}" if dollars_first
                else "counterfactual · {a} &ndash; {b}").format(
                    a=fmt_usd(m['cost']['min']), b=fmt_usd(m['cost']['max']))
    if m["enough_for_average"]:
        tiles = f"""<div class="stat-row">
<div class="stat"><div class="k">Runs Analyzed</div><div class="v num">{n}</div>
 <div class="sub">{esc(str(w0))} &rarr; {esc(str(w1))}</div></div>
<div class="{tok_cls}"><div class="k">Median Tokens / Run</div>
 <div class="v num">{_split_unit(fmt_tok(m['tokens']['median']))}</div>
 <div class="sub">range {fmt_tok(m['tokens']['min'])} &ndash; {fmt_tok(m['tokens']['max'])}</div></div>
<div class="{cost_cls}"><div class="k">Median Cost / Run</div>
 <div class="v num">{fmt_usd(m['cost']['median'])}</div>
 <div class="sub">{cost_sub}</div></div>
<div class="stat good"><div class="k">Cache Hit Rate</div>
 <div class="v num">{m['cache_hit']['median']*100:.0f}<span class="u">%</span></div>
 <div class="sub">of input tokens served from cache</div></div></div>"""
    else:
        tiles = (f"<div class='stat-row'><div class='stat' style='grid-column:1/-1'>"
                 f"<div class='k'>Thin data</div><div class='v num'>{n} run(s)</div>"
                 f"<div class='sub'>below the {m['avg_floor']}-run floor; per-run numbers "
                 f"shown below, no average claimed</div></div></div>")

    # run economics
    rows = []
    for i, (r, p) in enumerate(zip(runs, per)):
        ch = p["cache_hit"]
        rows.append(
            f"<tr><td class='runid'><span class='dot'></span>Run {i+1:02d}</td>"
            f"<td class='num'>{esc(r.day or '?')}</td><td class='num'>{p['steps']}</td>"
            f"<td class='tok num'>{fmt_tok(p['tokens'])}</td>"
            f"<td class='cost num'>{fmt_usd(p['cost'])}</td>"
            f"<td><span class='cachecell'><span class='cachebar'>"
            f"<i style='width:{ch*100:.0f}%'></i></span>"
            f"<span class='cacheval num'>{ch*100:.0f}%</span></span></td></tr>")
    tfoot = ""
    if m["enough_for_average"]:
        tfoot = (f"<tfoot><tr><td>Median <span class='rng'>across {n} runs</span></td>"
                 f"<td class='num'>&mdash;</td>"
                 f"<td class='num'>{m['steps']['median']:.0f}<span class='rng'>"
                 f"{m['steps']['min']:.0f} &ndash; {m['steps']['max']:.0f}</span></td>"
                 f"<td class='num'>{fmt_tok(m['tokens']['median'])}<span class='rng'>"
                 f"{fmt_tok(m['tokens']['min'])} &ndash; {fmt_tok(m['tokens']['max'])}</span></td>"
                 f"<td class='num' style='color:var(--ember-hot)'>{fmt_usd(m['cost']['median'])}"
                 f"<span class='rng'>{fmt_usd(m['cost']['min'])} &ndash; {fmt_usd(m['cost']['max'])}</span></td>"
                 f"<td class='num' style='color:var(--gold-soft)'>{m['cache_hit']['median']*100:.0f}%"
                 f"<span class='rng'>{m['cache_hit']['min']*100:.0f}% &ndash; {m['cache_hit']['max']*100:.0f}%</span></td>"
                 f"</tr></tfoot>")
    econ = (f"<div class='table-shell' style='margin-top:22px'><table class='econ'>"
            f"<thead><tr><th>Run</th><th>Date</th><th>Steps</th><th>Tokens</th>"
            f"<th>Cost</th><th>Cache Hit</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody>" + tfoot + "</table></div>")

    rep = runs[m["rep_idx"]]
    rep_i = m["rep_idx"] + 1
    trace = svg_trace(rep, pricing)
    growth = svg_growth(rep)
    model_legend = "".join(
        f"<span><i class='lg-swatch' style='background:{bg};border:1px solid {color}'></i> "
        f"{esc(badge.title())}</span>" for badge, color, bg in models_present(runs, pricing))
    shown = min(14, len(rep.steps))
    trace_sub = (f"Run {rep_i:02d} ({esc(rep.day or '?')}), reconstructed step by step. "
                 f"Node width carries cache-read weight; watch it grow as context accumulates."
                 + (f" First {shown} of {len(rep.steps)} steps shown." if len(rep.steps) > 14 else ""))

    flags = []
    for f in findings:
        if f["key"] == "shape":
            continue
        cls = "fired" if f["fired"] else "clean"
        badge = "Fired" if f["fired"] else "Clean"
        if f["fired"] and f["saving"]:
            amt = _saving_amt(f["saving"])
            if amt:  # numeric saving: highlight the amount, then the tail
                save = (f"<div class='flag-save'><span class='amt'>{esc(amt)}</span> "
                        f"{esc(_saving_tail(f['saving']))}</div>")
            else:    # qualitative saving (no number): render the note plainly, no amt slot
                save = f"<div class='flag-save'>{esc(f['saving'])}</div>"
        elif not f["fired"]:
            save = "<div class='flag-save'><span class='amt'>Nominal</span> no action needed</div>"
        else:
            save = ""
        # esc() at the sink: finding messages are plain text by contract, and any
        # transcript-derived substring (basenames, tool names) is escaped HERE so a
        # future check that forgets its own esc() cannot inject into the report.
        flags.append(f"<div class='flag {cls}'><div class='flag-head'>"
                     f"<div class='flag-name'>{esc(f['title'])}</div>"
                     f"<div class='flag-badge'>{badge}</div></div>"
                     f"<div class='flag-body'>{esc(f['msg'])}</div>{save}</div>")

    recs_html = []
    for i, r in enumerate(recs):
        amt = _saving_usd(r["saving"])
        amt_html = (f"<div class='rec-save'><div class='amt'>~{fmt_usd(amt)}</div>"
                    f"<div class='lbl'>across runs</div></div>") if amt else \
                   ("<div class='rec-save'><div class='amt'>&mdash;</div>"
                    "<div class='lbl'>see note</div></div>")
        recs_html.append(
            f"<div class='rec'><div class='rec-rank'>{i+1}<small>Rank</small></div>"
            f"<div class='rec-main'><div class='rt'>{esc(r['title'])}</div>"
            f"<div class='rr'>{esc(r['why'])}</div></div>{amt_html}</div>")
    if not recs_html:
        recs_html = ["<p class='section-sub'>No efficiency flag fired. This skill is already lean.</p>"]
    rec_total = ""
    if combined > 0.01:
        rec_total = (f"<div class='rec-total'>Combined recovery: "
                     f"<b>~{fmt_usd(combined)} across the {n} runs observed</b> · roughly "
                     f"<b>{pct:.0%}</b> of median run cost, quality held constant.</div>")

    privacy = ("<b>Shared report.</b> Project and file names are aliased. This variant passed "
               "the leak guard before it was written."
               if shared else
               "<b>Internal report.</b> Tool names and file names are shown in full because this "
               "audit is for your eyes only. It never leaves this machine unless you explicitly "
               "share it with --shared, which redacts it.")

    if is_agent:
        method_note = (f"A run is one spawn of the {esc(target)} agent, traced from the "
                       f"agent's own transcript file, so its internal steps are fully counted.")
    else:
        method_note = (f"A run is one invocation of /{esc(target)}, anchored at the first "
                       f"genuine invocation in a session and spanning to the next different "
                       f"skill or end of session. Subagent-internal turns are not in the "
                       f"parent transcript, so a spawned agent shows as its spawn plus the "
                       f"digest it returned, not its internal token cost.")
    notes = []
    if m.get("fallback_calls"):
        notes.append(f"{m['fallback_calls']} call(s) had an unrecognized model id and were "
                     f"priced at the fallback rate; add the model to pricing.json for "
                     f"accurate dollars.")
    if m.get("parse_errors"):
        notes.append(f"{m['parse_errors']} malformed transcript line(s) were skipped.")
    caveats = (" " + " ".join(notes)) if notes else ""
    owner_html = (f"<p class='owner'><b>{esc(owner)}</b>"
                  + (f" &middot; {esc(site)}" if site else "") + "</p>") if owner else ""

    return f"""{doc_open}
<header class="masthead"><div class="wrap">
  <div class="brandbar">
    <div class="brandmark">{SEAL_SVG}<div class="wordmark">GAUNT<b>LET</b></div>
      <span class="brand-tag">Token Efficiency Audit</span></div>
    <div class="masthead-meta">{'Agent' if is_agent else 'Skill'} <span class="num">{sig}{esc(target)}</span><br>
      Generated <span class="num">{today}</span></div>
  </div>
  <div class="hero-grid">
    <div>
      <div class="eyebrow">{'Agent' if is_agent else 'Skill'} Under Audit</div>
      <div class="skill-line" style="margin-top:16px">
        <span class="skill-name"><span class="slash">{sig}</span>{esc(target)}</span>
        <span class="skill-kind">{esc(kind)}</span></div>
      <p class="verdict">{esc(shape['msg'])}
        <span class="flagcount">{fired_n} efficiency flag{'s' if fired_n != 1 else ''}</span> found.</p>
    </div>
    <div class="hero-right">
      <div class="grade"><span class="glabel">Efficiency<br>Grade<span class="gnote">heuristic{' · &lt;3 runs' if not m['enough_for_average'] else ''}</span></span>
        <span class="gval">{esc(grade[0])}<small>{esc(grade[1:])}</small></span></div>
      <div style="font-size:11.5px;color:var(--text-mute);text-align:right;max-width:230px">
        {('The fixes below recover an estimated <span style="color:var(--ember-hot)">' + f'{pct:.0%}</span> of median run cost.') if combined > 0.01 else (f'{fired_n} flag(s) fired; the ranked fixes below carry token, not dollar, estimates.' if fired_n else 'No material waste found; the workflow runs lean.')}
        <span class="gradekey">Grade: 100 base, &minus;8 per fired flag, penalized for cache &lt;90% and output &gt;6%, floored at 40. A relative signal, not a benchmark.</span></div>
    </div>
  </div>
  {(f'<p class="section-sub" style="margin-top:18px">{esc(m["filter_note"])}</p>') if m.get('filter_note') else ''}
  {tiles}
</div></header>
{_baseline_html(baseline)}
<main class="wrap">
  <section id="economics">
    <div class="eyebrow">01 · Ledger</div>
    <h2 class="section-title">Run Economics</h2>
    <p class="section-sub">Every historical <span class="mono" style="color:var(--gold-soft)">{sig}{esc(target)}</span>
      run reconstructed from its transcript. Cost is billed tokens at posted rates; cache reads are discounted.</p>
    {econ}
  </section>
  <section id="trace">
    <div class="eyebrow">02 · The Trace</div>
    <h2 class="section-title">Execution Trace &mdash; Representative Run</h2>
    <p class="section-sub">{esc(trace_sub)}</p>
    <div class="trace-legend">
      {model_legend}
      <span><i class="lg-swatch" style="background:var(--ember-deep);border:1px solid var(--ember)"></i> cache read (bar width)</span>
      <span><i class="lg-line branch"></i> subagent branch</span></div>
    <div class="trace-scroll">{trace}</div>
    <div class="scroll-hint">Scroll horizontally to follow the full trace.</div>
  </section>
  <section id="growth">
    <div class="eyebrow">03 · Accumulation</div>
    <h2 class="section-title">Context Growth Curve</h2>
    <p class="section-sub">Cache-read tokens per step across the representative run. The context
      never shrinks: once loaded, the standing overhead is re-read on every step. That floor is
      what the flags below attack.</p>
    <div class="curve-card" style="margin-top:22px">{growth}</div>
  </section>
  <section id="flags">
    <div class="eyebrow">04 · Waste Checks</div>
    <h2 class="section-title">Efficiency Flags</h2>
    <p class="section-sub">Independent waste checks run against every step of every run. A flag
      FIRES only when the pattern recurs with real savings.</p>
    <div class="flag-grid" style="margin-top:22px">{''.join(flags)}</div>
  </section>
  <section id="recs">
    <div class="eyebrow">05 · The Fixes</div>
    <h2 class="section-title">Recommendations</h2>
    <p class="section-sub">Ranked by recovered cost. Recommendations only; none change the
      skill's logic. GAUNTLET proposes, it does not rewrite.</p>
    <div class="rec-list" style="margin-top:22px">{''.join(recs_html)}</div>
    {rec_total}
  </section>
</main>
<footer class="foot"><div class="wrap"><div class="foot-grid">
  <div><h4>Method &amp; Attribution</h4>
    <p>{method_note} Billed API calls are
      deduped on message id. Costs are counterfactual metered pricing at the illustrative
      rates in pricing.json (edit them; verify against the current provider rate card before
      quoting a dollar figure), not your bill. Text tool-result sizes are estimated at ~4
      bytes per token; image reads use pixel-based token math. Savings are modeled from
      observed step sizes and are directional, not invoiced. Each median tile is a per-metric
      median across runs, so different tiles can come from different runs; the representative
      trace is the single run closest to the median step count.
      {n} run(s), {esc(str(w0))} through {esc(str(w1))}.{pricing_warn}{caveats}</p>
    {owner_html}</div>
  <div><h4>Privacy</h4><div class="privacy">{privacy}</div></div>
</div></div></footer>"""


def _split_unit(s):
    """Wrap a trailing M/k/B unit in a <span class='u'> for the stat tiles."""
    for u in ("B", "M", "k"):
        if s.endswith(u):
            return f"{s[:-1]}<span class='u'>{u}</span>"
    return s


def _saving_usd(saving):
    """Pull the first ~$N.NN dollar figure out of a saving string, else 0.0."""
    if not saving:
        return 0.0
    mt = re.search(r"\$([0-9][0-9,]*\.?[0-9]*)", saving)
    return float(mt.group(1).replace(",", "")) if mt else 0.0


def _saving_amt(saving):
    """The numeric quantity of a saving string for the flag's amt slot, preserving a
    leading `~` or `$`. Returns "" when the saving has no number, so a digit-less saving
    (e.g. the re_read / chatty qualitative notes) never shows a word like 'Each' as an
    amount (B3, B4)."""
    if not saving:
        return ""
    # word-bounded unit so "tok" is never bitten out of a compound word
    mt = re.search(r"~?\$?[0-9][0-9,\.]*(?:\s*(?:tokens?|tok|k|M)\b)?", saving)
    return mt.group(0).strip() if mt else ""


def _saving_tail(saving):
    amt = _saving_amt(saving)
    if not amt:
        return saving.strip(" ·;,")
    tail = saving[saving.find(amt) + len(amt):]
    return tail.strip(" ·;,") or "estimated"


# ------------------------------------------------------------------ main
def scan_stats(claude_dir):
    """(files, lines, json_ok) across the transcript tree. Lets --debug distinguish a
    schema mismatch (files present, nothing parses) from genuinely-zero usage."""
    nf = nl = nj = 0
    for path in glob.glob(os.path.join(claude_dir, "*", "*.jsonl")):
        nf += 1
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    nl += 1
                    try:
                        json.loads(line)
                        nj += 1
                    except (json.JSONDecodeError, ValueError):
                        pass
        except OSError:
            continue
    return nf, nl, nj


def _write_text(path, text):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)   # ~/Downloads is not a given on every OS
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ------------------------------------------------------------------ RUN FILTER
def filter_runs(runs, since=None, last=None):
    """Narrow the discovered runs so an audit can measure only the runs that matter (e.g.
    the ones after a fix). `since` keeps runs on/after an ISO date; `last` keeps the N most
    recent by day. Returns (kept_runs, note_or_None); the note is disclosed in the report."""
    if not since and last is None:
        return list(runs), None
    kept = list(runs)
    if since:
        kept = [r for r in kept if r.day and r.day >= since]
    if last is not None:
        kept = sorted(kept, key=lambda r: (r.day or ""), reverse=True)[:max(0, last)]
        kept = sorted(kept, key=lambda r: (r.day or ""))
    bits = []
    if since:
        bits.append(f"since {since}")
    if last is not None:
        bits.append(f"most recent {last}")
    note = f"Filtered to {len(kept)} of {len(runs)} run(s): {', '.join(bits)}."
    return kept, note


# ------------------------------------------------------------------ FLEET / BASELINE
def audit_all(claude_dir, pricing, checklist):
    """Measure every auditable skill and agent, so a user knows what to audit first.
    Returns rows sorted worst-first by median tokens/run. Reuses find_runs/measure/diagnose,
    so a row means the same thing a full single-skill audit would."""
    rows = []
    for (name, kind) in find_inventory(claude_dir):
        runs = (find_agent_runs(name, claude_dir) if kind == "agent"
                else find_runs(name, claude_dir))
        if not runs:
            continue
        m = measure(runs, pricing, checklist)
        findings = diagnose(runs, m, pricing, checklist)
        flags = sum(1 for f in findings if f["fired"] and f["key"] != "shape")
        rows.append({"name": name, "kind": kind, "n_runs": m["n_runs"],
                     "tokens": m["tokens"]["median"], "steps": m["steps"]["median"],
                     "cost": m["cost"]["median"], "flags": flags,
                     "enough": m["enough_for_average"]})
    rows.sort(key=lambda r: -r["tokens"])
    return rows


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso_or_none(x):
    """x if it is an ISO date string, else None. Sanitizes an untrusted baseline window so
    only a date (never an injected identifier) can reach the report."""
    return x if isinstance(x, str) and _ISO_DATE.match(x) else None


def _fired_keys(findings):
    """Fired non-shape check keys from a findings list of any shape. `shape` is a workflow
    classifier, never a flag, so it is excluded on BOTH sides of a baseline diff."""
    if not isinstance(findings, list):
        return set()
    return {f["key"] for f in findings
            if isinstance(f, dict) and f.get("fired") and f.get("key") != "shape"}


def compare_baseline(prior, m, findings):
    """Diff a prior --json blob against the current audit. Raises ValueError on a schema
    mismatch or a baseline that is not a JSON object (never compare incomparable layouts).
    Every field is type-validated: a user hand-edits their baseline, so a fat-fingered value
    (a quoted number, a non-list findings) must degrade gracefully, never traceback."""
    if not isinstance(prior, dict):
        raise ValueError("baseline is not a JSON object")
    if prior.get("schema_version") != JSON_SCHEMA_VERSION:
        raise ValueError(f"baseline schema_version {prior.get('schema_version')!r} "
                         f"!= this tool's {JSON_SCHEMA_VERSION}; re-record the baseline")
    pm = prior.get("medians")
    pm = pm if isinstance(pm, dict) else {}
    cur = {k: m[k]["median"] for k in ("steps", "tokens", "cost", "cache_hit")}
    metrics = {}
    for k in ("tokens", "steps", "cost", "cache_hit"):
        old = pm.get(k)
        # ignore anything that is not a real number (bool is not a metric)
        if not isinstance(old, (int, float)) or isinstance(old, bool):
            continue
        new = cur.get(k)
        metrics[k] = {"old": old, "new": new, "diff": new - old,
                      "pct": ((new - old) / old) if old else None}
    # Whitelist prior finding keys against the check keys THIS run defines, so a hand-edited
    # baseline cannot inject arbitrary text (a client name in a fake key) into the report,
    # including a --shared one. The current findings enumerate every legal check key.
    valid_keys = {f["key"] for f in findings if isinstance(f, dict) and "key" in f}
    prior_fired = _fired_keys(prior.get("findings")) & valid_keys
    cur_fired = _fired_keys(findings)
    win = prior.get("window")
    if isinstance(win, (list, tuple)) and len(win) >= 2:
        # a window element is an ISO date or nothing; anything else is dropped, so a poisoned
        # baseline window cannot carry a name into a shared report.
        prior_window = [_iso_or_none(win[0]), _iso_or_none(win[1])]
    else:
        prior_window = None
    return {"metrics": metrics,
            "cleared": sorted(prior_fired - cur_fired),
            "regressed": sorted(cur_fired - prior_fired),
            "prior_window": prior_window}


# ------------------------------------------------------------------ DEMO DATA
# A self-contained synthetic dataset so --demo (and the sample report) work with zero real
# history. Everything here is fabricated: fake project, fake skill, fake files, fake costs.
def _demo_usage_line(mid, model, inp=0, ccr=0, cc5=0, out=0, tool=None, tool_input=None,
                     ts="2026-07-01T09:00:00Z"):
    content = []
    if tool:
        content.append({"type": "tool_use", "id": f"tu-{mid}", "name": tool,
                        "input": tool_input or {}})
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "id": mid, "model": model, "content": content,
        "usage": {"input_tokens": inp, "cache_read_input_tokens": ccr,
                  "cache_creation_input_tokens": cc5, "output_tokens": out}}})


def _demo_invoke_line():
    body = ("<command-message>demo-skill</command-message>\n"
            "<command-name>/demo-skill</command-name>\n<command-args>run</command-args>")
    return json.dumps({"type": "user", "timestamp": "2026-07-01T09:00:00Z",
                       "message": {"role": "user", "content": body}})


def _demo_result_line(tid, payload):
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": payload}]}})


def _demo_session(seed):
    """One plausible multi-stage run: skill load, recon, a heavy read, work, output."""
    lines = [_demo_invoke_line()]
    ccr = 0
    files = ["report_q3.xlsx", "notes.md", "summary.md", "report_q3.xlsx"]
    for i in range(10 + seed * 3):
        model = "claude-opus-4" if i % 4 else "claude-haiku-4"
        tool = ["Bash", "Read", "Grep", "Read"][i % 4]
        tin = {"file_path": "C:/demo/" + files[i % 4]} if tool == "Read" else {}
        cc5 = 40_000 if i == 0 else 900 + 40 * i
        lines.append(_demo_usage_line(f"m{seed}-{i}", model, inp=8, ccr=ccr, cc5=cc5,
                                      out=140 if i % 4 else 40, tool=tool, tool_input=tin))
        if i == 3:
            lines.append(_demo_result_line(f"tu-m{seed}-{i}", "x" * 120_000))  # heavy recon
        ccr += cc5
    return lines


def write_demo_transcripts(claude_dir, sessions=4):
    """Write synthetic demo transcripts under claude_dir/demo-project (skill 'demo-skill').
    Shared by --demo and examples/make_sample.py, so the demo and the sample never drift."""
    proj = os.path.join(claude_dir, "demo-project")
    os.makedirs(proj, exist_ok=True)
    for s in range(sessions):
        with open(os.path.join(proj, f"session-{s}.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(_demo_session(s)) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Audit one skill's or agent's token efficiency.")
    ap.add_argument("--version", action="version", version=f"gauntlet {__version__}")
    ap.add_argument("--skill", default=None,
                    help="skill or agent name to audit, e.g. my-skill (see --list)")
    ap.add_argument("--list", action="store_true",
                    help="list every auditable skill and agent found, with run counts")
    ap.add_argument("--claude-dir",
                    default=os.path.expanduser("~/.claude/projects"),
                    help="Claude Code transcript root (default: ~/.claude/projects)")
    ap.add_argument("--pricing", default=os.path.join(os.path.dirname(__file__), "pricing.json"),
                    help="pricing table JSON (default: bundled pricing.json)")
    ap.add_argument("--checklist", default=os.path.join(os.path.dirname(__file__), "checklist.json"),
                    help="waste-check thresholds JSON (default: bundled checklist.json)")
    ap.add_argument("--out", default=os.path.expanduser("~/Downloads/gauntlet_report.html"),
                    help="report output path (default: ~/Downloads/gauntlet_report.html)")
    ap.add_argument("--json", default=None, help="also write metrics JSON here")
    ap.add_argument("--debug", action="store_true",
                    help="print how many transcript files and lines were scanned")
    ap.add_argument("--shared", action="store_true", help="produce a redacted, shareable report")
    ap.add_argument("--owner", default="", help="optional byline name for the report footer")
    ap.add_argument("--site", default="", help="optional byline site for the report footer")
    ap.add_argument("--demo", action="store_true",
                    help="run the full pipeline over bundled synthetic data (no history needed)")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="only analyze runs on or after this date (measure a change)")
    ap.add_argument("--last", type=int, default=None, metavar="N",
                    help="only analyze the N most recent runs")
    ap.add_argument("--dollars", action="store_true",
                    help="lead with cost instead of tokens (dollars are counterfactual on a flat plan)")
    ap.add_argument("--all", action="store_true", dest="all_",
                    help="rank every auditable skill and agent by median tokens (what to audit first)")
    ap.add_argument("--baseline", default=None, metavar="PRIOR.json",
                    help="diff this audit against a prior --json output (measure a change)")
    args = ap.parse_args(argv)

    demo_dir = None
    if args.demo:
        demo_dir = tempfile.mkdtemp(prefix="gauntlet-demo-")
        write_demo_transcripts(demo_dir)
        args.claude_dir = demo_dir
        args.skill = args.skill or "demo-skill"
    try:
        return _run(args, ap)
    finally:
        if demo_dir:
            shutil.rmtree(demo_dir, ignore_errors=True)


def _run(args, ap):
    if not os.path.isdir(args.claude_dir):
        print(f"error: transcript root not found: {args.claude_dir}", file=sys.stderr)
        return 2

    if args.debug:
        nf, nl, nj = scan_stats(args.claude_dir)
        print(f"[debug] {args.claude_dir}: {nf} transcript file(s), {nl} line(s), "
              f"{nj} parsed as JSON. If files>0 but nothing is found, your Claude Code "
              f"build may write a transcript shape this version does not parse.",
              file=sys.stderr)

    if args.list:
        inv = find_inventory(args.claude_dir)
        if not inv:
            nf = scan_stats(args.claude_dir)[0]
            print(f"No skill or agent runs found (scanned {nf} transcript file(s) under "
                  f"{args.claude_dir}). Run with --debug for parse counts.")
            return 1
        rows = sorted(inv.items(), key=lambda kv: -kv[1]["runs"])
        w = max(len(name) for (name, _), _ in rows)
        print(f"{'NAME'.ljust(w)}  KIND   RUNS  LAST SEEN")
        for (name, kind), e in rows:
            print(f"{name.ljust(w)}  {kind:<5}  {e['runs']:>4}  {e['last'] or '?'}")
        print(f"\n{len(rows)} auditable name(s). Audit one with: --skill <name>")
        return 0

    pricing = load_config(args.pricing, DEFAULT_PRICING, required=("models", "fallback"))
    checklist = load_json(args.checklist) if os.path.exists(args.checklist) else {}

    if args.all_:
        rows = audit_all(args.claude_dir, pricing, checklist)
        if not rows:
            nf = scan_stats(args.claude_dir)[0]
            print(f"No skill or agent runs found (scanned {nf} transcript file(s)).")
            return 1
        w = max(len(r["name"]) for r in rows)
        print(f"{'NAME'.ljust(w)}  KIND   RUNS  TOK/RUN   STEPS  $/RUN*  FLAGS")
        any_thin = False
        for r in rows:
            thin = "" if r["enough"] else "~"
            if not r["enough"]:
                any_thin = True
            print(f"{r['name'].ljust(w)}  {r['kind']:<5}  {r['n_runs']:>4}  "
                  f"{fmt_tok(r['tokens']):>6}{thin:<1}  {r['steps']:>5.0f}  "
                  f"{fmt_usd(r['cost']):>6}  {r['flags']:>5}")
        print(f"\n{len(rows)} name(s), worst-first by median tokens. Audit one: --skill <name>")
        if any_thin:
            print(f"~ = fewer than {checklist.get('min_runs_for_average', MIN_RUNS_FOR_AVERAGE)} "
                  f"runs, so the figure is observed, not a median.")
        print("* $/run is counterfactual metered cost (pricing.json), not a bill.")
        return 0

    if not args.skill:
        ap.error("--skill is required (or use --list to see what is auditable)")

    target = args.skill.lstrip("/").lstrip("@").lower()

    runs = find_runs(target, args.claude_dir)
    is_agent = False
    if not runs:
        runs = find_agent_runs(target, args.claude_dir)
        is_agent = bool(runs)

    runs, filter_note = filter_runs(runs, since=args.since, last=args.last)

    m = measure(runs, pricing, checklist)
    if filter_note:
        m["filter_note"] = filter_note
    findings = diagnose(runs, m, pricing, checklist, shared=args.shared) if runs else []
    recs = recommendations(findings, m) if runs else []

    baseline = None
    if args.baseline and runs:
        try:
            baseline = compare_baseline(load_json(args.baseline), m, findings)
        except (ValueError, OSError, json.JSONDecodeError, TypeError,
                AttributeError, IndexError, KeyError) as e:
            print(f"error: cannot compare baseline: {e}", file=sys.stderr)
            return 2

    forbidden = []
    if args.shared:
        acct = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        home_user = os.path.basename(os.path.expanduser("~"))   # env-independent account name
        # forbidden = REAL project dir names + OS account + identifying file basenames +
        # non-builtin tool names. All captured BEFORE redact_for_share mutates them, so the
        # guard RAISES if any ever reaches a shared report. Project names must be read here,
        # before redact aliases them, or the guard would hold the aliases, not the secrets.
        projects = {r.project for r in runs}
        # Only IDENTIFYING basenames go in the guard. A bare lowercase word like "run" or
        # "main" (an extension-less script) is not identifying AND collides with the
        # report's own vocabulary ("run reconstructed..."), which used to crash the guard
        # with a false "content leak". Redaction still blanks EVERY basename in the output;
        # the guard only backstops the ones that could actually identify someone.
        basenames = {t for r in runs for s in r.steps for t in s.read_targets
                     if _identifying(t)}
        hidden_tools = redact_for_share(m, runs)
        forbidden = (list(projects) + [acct, home_user]
                     + list(basenames) + list(hidden_tools)) if runs else []

    html_doc = render(target, m, runs, findings, recs, pricing, shared=args.shared,
                      owner=args.owner, site=args.site, is_agent=is_agent, checklist=checklist,
                      dollars_first=args.dollars, baseline=baseline)
    if args.shared:
        assert_no_leak(html_doc, forbidden)

    _write_text(args.out, html_doc)

    if args.json:
        # schema_version lets consumers (and --baseline) detect a breaking layout change.
        # Documented in docs/json_schema.md; bump it when a field's meaning changes.
        blob = {"schema_version": JSON_SCHEMA_VERSION,
                "gauntlet_version": __version__,
                "skill": target, "kind": "agent" if is_agent else "skill",
                "n_runs": m["n_runs"], "enough_for_average": m["enough_for_average"],
                "window": list(m["window"]),
                "medians": {k: m[k]["median"] for k in
                            ("steps", "tokens", "cost", "cache_hit", "overhead", "out_share")},
                "findings": [{"key": f["key"], "fired": f["fired"]} for f in findings]}
        if args.shared:
            assert_no_leak(json.dumps(blob), forbidden)
        _write_text(args.json, json.dumps(blob, indent=2))

    sig = "@" if is_agent else "/"
    print(f"GAUNTLET {sig}{target}: {m['n_runs']} run(s). Report -> {args.out}")
    if filter_note:
        print(f"  {filter_note}")
    if runs:
        # honor the same no-median-below-floor rule the report uses; tokens lead (dollars
        # are counterfactual on a flat plan) unless --dollars was passed.
        lbl = "median" if m["enough_for_average"] else f"observed n={m['n_runs']}"
        cost_tag = fmt_usd(m['cost']['median']) + ("/run" if args.dollars else "/run est")
        flags = sum(1 for f in findings if f['fired'] and f['key'] != 'shape')
        print(f"  {lbl} {fmt_tok(m['tokens']['median'])} tok, {m['steps']['median']:.0f} steps, "
              f"{m['cache_hit']['median']*100:.0f}% cache ({cost_tag}); {flags} flags fired")
        if baseline:
            tok = baseline["metrics"].get("tokens")
            if tok and tok["pct"] is not None:
                arrow = "down" if tok["diff"] < 0 else "up"
                print(f"  vs baseline: median tokens {arrow} {abs(tok['pct'])*100:.0f}% "
                      f"({fmt_tok(tok['old'])} -> {fmt_tok(tok['new'])})"
                      + (f"; cleared {', '.join(baseline['cleared'])}" if baseline['cleared'] else "")
                      + (f"; NEW {', '.join(baseline['regressed'])}" if baseline['regressed'] else ""))
        return 0
    if filter_note:
        # Runs existed; the --since/--last filter removed them all. Not a schema problem.
        print("  All runs were excluded by the filter. Widen --since/--last.")
        return 1
    nf = scan_stats(args.claude_dir)[0]
    hint = ("  No runs found. " + (
            f"Scanned {nf} transcript file(s) but matched none, which can mean the name is "
            f"off (try --list) or your Claude Code build writes a transcript shape this "
            f"version does not parse (run --debug, and if so please open an issue)."
            if nf else
            f"Scanned {nf} transcript file(s). Point --claude-dir at your Claude Code "
            f"transcript root, or use --demo to see a report with no history."))
    print(hint)
    return 1


if __name__ == "__main__":
    sys.exit(main())
