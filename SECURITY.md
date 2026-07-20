# Security Policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue.

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and click
**Report a vulnerability**. That creates a private advisory visible only to the maintainers.

## Scope and attack surface

GAUNTLET is one standard-library Python file. It:

- reads and parses local Claude Code transcripts under `~/.claude/projects` (or `--claude-dir`),
- makes no network requests and has no runtime package dependencies,
- writes a single local HTML file to the path you choose.

The report is data-only (no scripts) and ships with a restrictive Content-Security-Policy as
defense in depth. Even so, reports of any of the following are welcome and will be addressed
promptly:

- a way to make the report contain prompt, response, or tool-result **content** (it should carry
  only derived counters and labels),
- a `--shared` **leak-guard bypass** (a real project name, file basename, or custom tool name
  reaching a redacted report),
- HTML/CSS injection in the rendered report from crafted transcript values,
- path traversal or an unexpected write outside the chosen output path.

## What to include

Your GAUNTLET version (`--version`), OS, Python version, and steps to reproduce. A sample
transcript that triggers the issue helps, but redact anything sensitive first.
