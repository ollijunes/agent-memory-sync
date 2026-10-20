# T1 Initial tool: allowlist backup, secret scan, schedulers, restore

- Status: done
- Lane: feat/initial-tool-20261020
- Evidence: CI 12/12 with tests on Linux and macOS (Intel runner) × Python 3.11–3.14, the Gitleaks integration job, lint, secrets and ledger: https://github.com/ollijunes/agent-memory-sync/actions/runs/39477835077

## Plan

Goal: a standard-library CLI that copies allowlisted Claude Code and Codex memory and instruction files into a separate private Git repository, scans the copies with gitleaks and a built-in scanner before anything reaches Git, commits and optionally pushes; launchd and systemd schedules; a restore command and guide; tests, CI and a privacy scan. Every example is synthetic.

Checks: pytest with 90 % coverage, ruff check and format, `scripts/check_public_tree.py`, gitleaks over the full history, an integration test with the real gitleaks binary, a manual run against a synthetic home directory.
