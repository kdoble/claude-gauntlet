---
name: False efficiency flag
about: A waste check fired (or stayed silent) when it should not have
title: "[flag] <check name> fired incorrectly"
labels: false-positive
---

**Which check**
e.g. `re_read`, `heavy_recon`, `overhead`, `model_tier`, `shape`.

**What it claimed**
Paste the flag text from the report.

**Why you think it is wrong**
What the workflow actually does, and why the flag misreads it.

**Context**
- `gauntlet --version`:
- How many runs were analyzed (from the report header):
- Skill or agent, and roughly what it does:

Attaching a `--shared` report (redacted) is fine and helps; the default report is not, it
carries your file basenames.
