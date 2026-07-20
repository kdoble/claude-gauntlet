#!/usr/bin/env python3
"""Regenerate docs/sample_report.html from SYNTHETIC fixtures, so anyone can see what a
GAUNTLET report looks like without pointing the tool at their own transcripts.

Run from anywhere:  python examples/make_sample.py

This is a thin wrapper over the same synthetic data the CLI's --demo flag uses
(gauntlet.write_demo_transcripts), so the committed sample and --demo never drift.
Everything is fabricated: fake project, fake skill, fake file names, fake costs.
The date is pinned via GAUNTLET_TODAY so the committed HTML is reproducible.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import gauntlet  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as d:
        gauntlet.write_demo_transcripts(d)
        out = os.path.join(ROOT, "docs", "sample_report.html")
        env = dict(os.environ, GAUNTLET_TODAY="2026-07-18")  # fixed -> reproducible file
        rc = subprocess.call([sys.executable, os.path.join(ROOT, "gauntlet.py"),
                              "--skill", "demo-skill", "--claude-dir", d,
                              "--out", out], env=env)
        if rc == 0:
            print(f"sample report -> {out}")
        return rc


if __name__ == "__main__":
    sys.exit(main())
