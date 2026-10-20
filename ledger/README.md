# agent-memory-sync

## Goal

A small tool that copies Claude Code and Codex memory and instruction files into a private Git repository on a schedule, scans them for secrets before every commit and never copies transcripts or credentials, for people who use these agents and do not want to lose what the agents have learned. A release is finished when the tests and ruff pass in CI on Linux and macOS, the secret scan fails closed in every tested case, `scripts/check_public_tree.py` and gitleaks report nothing, and every example is synthetic.

## Environments

Local only. The tool runs on the user's own machine under launchd (macOS) or a systemd user timer (Linux) and pushes to a Git remote the user chooses. CI (`.github/workflows/ci.yml`) runs the tests on Linux and macOS with Python 3.11 to 3.14.

## Manual checks

Before a release, run a backup against a synthetic home directory with planted secrets and transcripts, and check that only allowlisted files reach the backup repository and that a planted secret stops the commit. Install and remove the launchd agent and the systemd timer once each and check the log. Follow the restore guide from a fresh clone.
