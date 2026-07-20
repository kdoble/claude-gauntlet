# Changelog

All notable changes to GAUNTLET are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

## [0.1.0] - 2026-07-20

First public release: per-skill/agent token-efficiency audit from local Claude Code
transcripts.

### Features
- Reconstructs one skill's or agent's attributed session runs into a step-by-step trace,
  measures token/cache/cost, runs eight waste checks, and renders a self-contained HTML report
  with ranked opportunities to investigate.
- `--demo` runs the full pipeline over bundled synthetic transcripts, so you can see a real
  report with no Claude Code history of your own.
- `--all` prints a ranked table of every auditable skill and agent, worst-first, so you know
  what to audit first; `--list` lists every auditable name with run counts.
- `--since YYYY-MM-DD` and `--last N` filter which runs are analyzed; `--baseline prior.json`
  diffs the current run against a saved `--json` output and shows what moved.
- `--shared` produces a redacted variant, aliasing project names, file basenames, and custom
  tool names, with a leak guard proven by a negative test.
- Tokens and steps are the headline metrics (dollars are counterfactual on a flat plan);
  `--dollars` restores cost as the lead. `--version` prints the version.
- Machine-readable `--json` carries a `schema_version` (documented in `docs/json_schema.md`).

### Honesty and privacy
- Language tightened to match what the code does. GAUNTLET reads and parses transcripts locally
  but never writes prompt, response, or tool-result content into the report; it is not claimed to
  "never read" content. `--shared` is described as redacted-not-anonymous ("review before
  publishing"), not "safe to share". Recommendations are labeled "opportunities to investigate,"
  not proven fixes; modeled savings are an upper bound to validate. The unit is named an
  "attributed session run," and the at-most-one-run-per-session merge is disclosed.
- `pricing.json` records a `source` URL and a verified `as_of` date; the report footer warns when
  the rate card is older than the configured staleness window (`pricing_stale_days`, default 90).
  Rates verified against the published Anthropic rate card.

### Packaging and hardening
- Installed console command is `claude-gauntlet` (PyPI already has an unrelated `gauntlet`);
  `gauntlet` remains a compatibility alias.
- Reports are written atomically (temp file + `os.replace`) with `0o600` permissions where the OS
  honors them, and carry a restrictive Content-Security-Policy.
- CI pins actions to commit SHAs, sets least-privilege `permissions: contents: read`, builds and
  installs the wheel, invokes the installed console command, and asserts the sample regenerates
  byte-identical. Adds `SECURITY.md`, `dependabot.yml` (github-actions), and issue templates.

Stdlib only, no runtime dependencies. MIT licensed.
