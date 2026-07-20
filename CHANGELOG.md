# Changelog

All notable changes to GAUNTLET are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added
- `--version` flag and a `__version__` string.
- `--demo` runs the full pipeline over bundled synthetic transcripts, so you can see a
  real report with no Claude Code history of your own.
- `--since YYYY-MM-DD` and `--last N` filter which runs are analyzed, so you can re-audit
  only the runs after a change and measure the difference.
- `--baseline prior.json` diffs the current run against a saved `--json` output and shows
  what moved (median tokens, flags cleared/added).
- `--all` prints a ranked table of every auditable skill and agent, worst-first, so you
  know what to audit first.
- Tokens and steps are now the headline metrics (dollars are counterfactual on a flat
  plan); `--dollars` restores cost as the lead metric.
- Machine-readable `--json` now carries a `schema_version`; the field layout is documented
  in `docs/json_schema.md`.
- The report footer warns when the bundled rate card is older than the configured
  staleness window (`pricing_stale_days`, default 90).
- `pricing.json` now records a `source` URL and a verified `as_of` date.

### Changed
- README repositioned around token/context budgets for flat-plan users, with a live sample
  report, a comparison to existing cost tools, and a "verify the source yourself" section.

## [0.1.0]
- First public release: per-skill/agent token-efficiency audit from local Claude Code
  transcripts, step-by-step trace, cache and waste checks, ranked recommendations,
  `--list`, `--shared` redaction with a leak guard, HTML report. Stdlib only, MIT.
