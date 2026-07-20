---
name: Schema mismatch / zero runs found
about: The tool reports no runs but you know the skill ran (likely a transcript format change)
title: "[schema] zero runs where usage exists"
labels: schema
---

**What you ran**
```
python gauntlet.py --skill <name> --debug
```

**`--debug` output** (paste the `[debug] ... N transcript file(s), N line(s), N parsed` line):

```
paste here
```

**Versions**
- `gauntlet --version`:
- Claude Code version:
- OS and Python version:

**What you expected**
The skill ran N times recently, so you expected at least N runs.

**Anything else**
A high file count with zero runs usually means this build writes a transcript shape the parser
does not recognize. If you can share the shape of one assistant line (keys only, no content),
that helps a lot.
