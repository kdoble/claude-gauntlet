# `--json` output schema

`gauntlet --skill <name> --json out.json` writes a machine-readable summary next to the
HTML report. This is the tool's stable extensibility surface: build dashboards, CI gates,
or trend tracking on it. `--baseline` reads this same format to diff two audits.

Stability: the `schema_version` integer is bumped whenever a field's meaning changes or a
field is removed. New optional fields may be added without a bump. Pin on `schema_version`
if you consume this programmatically.

## Fields (schema_version 1)

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | Layout version. `1` today. |
| `gauntlet_version` | string | The `gauntlet` version that produced the file. |
| `skill` | string | The audited skill or agent name (normalized, no leading `/` or `@`). |
| `kind` | string | `"skill"` or `"agent"`. |
| `n_runs` | int | Number of runs analyzed (after any `--since`/`--last` filter). |
| `enough_for_average` | bool | True when `n_runs` met the averaging floor (`min_runs_for_average`, default 3). When false, the medians are single-observation values, not true medians. |
| `window` | [string, string] | `[first_day, last_day]` (ISO dates) of the analyzed runs; either may be `null`. |
| `medians.steps` | number | Median billed API calls per run. |
| `medians.tokens` | number | Median total tokens per run (input + cache read + cache write + output). |
| `medians.cost` | number | Median counterfactual metered cost per run, USD, at `pricing.json` rates. Not a bill. |
| `medians.cache_hit` | number | Median share of input tokens served from cache, 0..1. |
| `medians.overhead` | number | Median standing overhead (step-0 context) across fresh-session runs only. |
| `medians.out_share` | number | Median output tokens as a share of total, 0..1. |
| `findings[]` | array | One entry per waste check. |
| `findings[].key` | string | Stable check id (e.g. `re_read`, `heavy_recon`, `overhead`, `shape`). |
| `findings[].fired` | bool | Whether the check fired for this audit. |

## Notes

- Below the averaging floor (`enough_for_average: false`), treat `medians.*` as observed
  values from too few runs, not statistical medians. The HTML report labels this explicitly.
- Costs are counterfactual: subscription users pay a flat plan. Use dollars to compare runs,
  not as an invoice. See `pricing.json` `as_of` and `source`.
