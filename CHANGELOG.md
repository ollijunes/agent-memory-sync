# Changelog

## 0.1.0

- `run` copies allowlisted Claude Code and Codex memory, instruction and
  config files into `<machine>/` in a separate Git repository, commits and
  pushes. Built-in exclusions for credentials, transcripts, databases and
  runtime folders always win.
- Gitleaks and a built-in scanner check every run before anything is written
  to the backup repository. Findings show file, line and rule only.
- `plan` shows every file and the rule that included or skipped it.
- `init`, `doctor`, with a warning when a GitHub backup repository is public.
- `restore` with preview, manifest checks, conflict listing and `.bak`
  copies on overwrite.
- `schedule install`, `print` and `uninstall` for launchd and systemd user
  timers, with templates in `contrib/`.
- Tests on Linux and macOS with Python 3.11–3.14, an integration test with
  the real Gitleaks binary, and a privacy scan of the full history in CI.
