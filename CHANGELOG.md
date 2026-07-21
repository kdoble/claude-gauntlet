# Changelog

All notable changes to GAUNTLET are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

## [0.2.0] - 2026-07-21

Honesty fixes from the first outside audit ([#1](https://github.com/kdoble/claude-gauntlet/issues/1)),
run against a 2.2 GB / 383-session corpus. Both engine issues were reproduced and confirmed
against the source before anything changed.

### Added
- **Self-share and turn-count attribution metrics.** Every run now reports the share of its steps
  that ran before the next human-typed turn (`SELF`) and how many of your typed turns the span
  swallowed (`TURNS`). Because a skill run spans to end of session, a skill invoked early in a
  long session absorbs the tail, which could rank a document generator above the build skill that
  did the work. Both columns appear in `--all`, the report states the finding in words, and
  `--json` carries `medians.self_share`, `medians.human_turns`, and `tail_runs`. Thresholds
  `low_self_share` and `tail_turns` live in `checklist.json`.
  A row is marked `!` only when most of its runs are BOTH mostly-not-self-contained AND span
  five or more of your turns. Self-share alone was not enough: on real data nearly every
  interactive skill reads near 0% self-contained because you answer it once, so flagging on that
  alone fires on almost every row and ranks nothing. The turn count is what separates one
  invocation from a whole conversation.
  The attribution span is deliberately unchanged, and the numbers it produces are byte-identical
  to 0.1.0: a conversational skill legitimately continues across follow-up turns, so cutting at
  the first human turn would under-count those instead. The distortion is measured and disclosed,
  never silently corrected.
- Limitations entries for headless `claude -p` runs (no command tag, so not attributable to any
  skill) and for Windows cp1252 consoles re-reading the UTF-8 report.

### Fixed
- **The combined opportunity figure no longer omits cache re-warm.** `cache_break` reported only
  a token count, so `_saving_usd` scored it at $0.00 and the headline total was effectively the
  `model_tier` check alone, understating the report's own biggest finding. Cache writes bill at a
  known multiple of the input rate and those multipliers were already in `pricing.json`, so the
  re-warm is now priced with the same formula `step_cost` uses.
- The combined figure now names the fired checks it does not price, instead of presenting a
  partial sum as the whole opportunity.
- **`--shared` could not produce a report at all for some skills, present since 0.1.0.** If the
  audited skill happened to read a file named like one of GAUNTLET's own (`pricing.json`,
  `checklist.json`, and so on), that basename entered the leak guard's forbidden list, and the
  report prints those names in its own methodology text, so the guard raised on its own
  boilerplate every time. Found by running `--shared` against real transcripts during
  verification of this release. GAUNTLET's own shipped filenames are now excluded from the
  guard; redaction still blanks every basename in finding text, and a genuinely identifying
  basename is still guarded (both asserted by test).
- **Unit error in the combined percentage, present since 0.1.0.** The combined saving is a total
  across every observed run, but it was divided by the *per-run* median cost and labeled "of
  median run cost". The two are different units. It shipped reading a plausible 93% and was only
  exposed when the correctly-priced cache re-warm pushed the same expression to 2373%. The
  denominator is now the spend actually observed across those runs, and a test asserts the
  percentage can never exceed 100%.

### Tests
- Eight new tests. Two are negative tests proven to FAIL on a build with the fixes removed
  (`13 != 1` self-contained steps; `0.0 != 2.5` unpriced cache re-warm) and to pass with them in.
  Others pin the harness-injection boundary (a system-reminder is not a human turn), the
  no-false-flag case (one follow-up turn is not a tail-absorbed run), and the percentage ceiling.

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
