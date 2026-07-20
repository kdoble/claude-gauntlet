---
name: gauntlet
description: >
  Audit ONE specific Claude Code skill or agent for token efficiency. Point it at a
  skill name (e.g. /my-skill) or an agent name (e.g. web-researcher) and it
  reconstructs how that workflow actually ran across its historical runs on disk,
  diagrams it step by step, measures token/cache/cost efficiency, runs a waste
  checklist, and produces one self-contained HTML report with ranked recommendations
  to make it cheaper. Use when the user says "gauntlet", "/gauntlet", "run X through the
  gauntlet", "audit the /<skill> skill", "how token-efficient is <skill>", "how many
  calls does <skill> make", "where is <skill> wasting tokens", or "make <skill> more
  efficient". This is per-skill and workflow-level, reading local Claude Code
  transcripts; it never re-runs the skill.
---

# GAUNTLET: per-skill token-efficiency auditor

## What it does
Takes one skill or agent and answers: what does its workflow actually do, step by step;
how many calls per run; how the cache behaves; where it wastes tokens; and how to make it
cheaper. Output is one self-contained dark HTML report.

It reads local Claude Code transcripts (counters and labels only, never transcript
content), so it costs nothing to run and needs no live re-execution.

## How to run it
```
python gauntlet.py --demo                    # full report from synthetic data (no history needed)
python gauntlet.py --all                     # rank every skill/agent by tokens: what to audit first
python gauntlet.py --list                    # every auditable skill/agent, with run counts
python gauntlet.py --skill <name>            # audit one skill
python gauntlet.py --skill <agent-name>      # agents work too (traced from their transcripts)
python gauntlet.py --skill <name> --shared   # redacted, shareable variant
```
It resolves its own paths, so it runs from any working directory. The report lands at
`~/Downloads/gauntlet_report.html` by default. Options:
- `--demo`           run the whole pipeline over bundled synthetic data (zero history needed).
- `--all`            rank every auditable skill and agent worst-first by median tokens.
- `--list`           list every auditable skill and agent found, with run counts and
                     last-seen dates. Start here when unsure of the exact name.
- `--skill <name>`   the skill or agent to audit. Agents (invoked via the Agent/Task
                     tool) are traced from their own subagent transcript files.
- `--since <date>` / `--last <N>`  analyze only recent runs, to measure a change.
- `--baseline <prior.json>`  diff this audit against a saved `--json` output.
- `--dollars`        lead the report with cost instead of tokens.
- `--out <path>`     where to write the HTML (default `~/Downloads/gauntlet_report.html`)
- `--json <path>`    also write a metrics JSON (schema in docs/json_schema.md)
- `--shared`         redacted, shareable variant: aliases project names, file basenames,
                     and non-builtin tool names, then runs a leak guard before writing.
                     The default report is INTERNAL; do not share it as-is.
- `--owner/--site`   optional footer byline (default: none)
- `--debug`          print files-scanned vs lines-parsed (diagnose a schema mismatch)
- `--version`        print the version.

## What the report contains
1. Run economics: every historical run with steps, tokens, cost, cache hit, plus a
   median-and-range aggregate. It refuses to average below the run floor
   (checklist.json, default 3); it shows per-run numbers instead.
2. The trace: a step-by-step flow of a representative run. Each node is one billed API
   call with its model, tokens in/out, cache-read bar, and the tool it called. Subagent
   spawns branch off.
3. Context growth curve: cache-read tokens climbing across the run, with the standing-
   overhead floor marked.
4. Efficiency flags: named waste checks (re-reads, cache breakage, heavy recon in main
   context, model over-provisioning, chatty chains, standing overhead, verbosity,
   workflow-shape). Each fires with evidence and savings, or reports clean.
5. Ranked recommendations, biggest lever first. Recommendations only; it never rewrites
   the skill.

## Honesty rules built in
- A skill run is anchored at the first genuine human-typed invocation in a session.
  Tags quoted inside tool output or tool-written JSON never count. An agent run is one
  spawn, traced from the agent's own transcript file.
- Standing overhead is measured only from runs that started their session; mid-session
  invocations inherit prior context and are excluded, with the exclusion disclosed.
- Costs are counterfactual metered pricing from the editable, dated `pricing.json`, not a
  real bill; savings are modeled and labeled directional. Below the run floor, no median
  is claimed anywhere, including the flags. The efficiency grade is labeled a heuristic.

## Scope
Historical mining of Claude Code transcripts. It does not re-run the skill. Stdlib only,
no dependencies. See README.md for the full user guide and the sample report.
