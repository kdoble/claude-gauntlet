# Contributing to GAUNTLET

Thanks for looking. GAUNTLET is deliberately small and has two hard design rules; please keep
them:

1. **One file, standard library only.** The auditor is a single `gauntlet.py` with no runtime
   dependencies. A tool that reads your private transcripts should be auditable in one sitting
   and carry no supply chain. Do not add a dependency.
2. **Every guard is proven to fail.** A checker that has never been seen to fail is not a
   checker. The leak guard, the waste checks, the fabrication guards, and the pricing staleness
   warning each ship with a test that FIRES on a known-bad input. If you add a check, add its
   negative test.

## Run the tests

```
python -m unittest discover tests
```

Keep the suite green and Python 3.9 compatible (CI runs 3.9 and 3.13 on Windows, macOS, and
Linux). If you change what the report renders, regenerate the sample and commit it:

```
python examples/make_sample.py
```

It is pinned to a fixed date, so the output is reproducible.

## Pull requests

- One focused change per PR, with a short "why."
- If you touch the `--json` output, update `docs/json_schema.md` and bump `schema_version` on a
  breaking change.
- If you touch pricing, update `pricing.json` (and its embedded fallback `DEFAULT_PRICING`; a
  test guards them against drift) and cite the source.

Bug reports and false-positive reports are welcome; see the issue templates.
