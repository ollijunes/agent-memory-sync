# Security

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/ollijunes/agent-memory-sync/security/advisories/new).
Do not put memory files, paths from your home folder, a backup repository URL
or a real secret in a public issue. Describe the problem with synthetic files
and include the affected version.

## What the tool does

- Copies only allowlisted files. Exclusions for credentials, transcripts,
  databases and runtime folders always win over includes, also over your own
  config. [What is copied](docs/what-is-copied.md) lists every rule.
- Never reads `~/.claude.json`, never follows symlinks and skips binary,
  non-UTF-8 and oversized files.
- Copies files first into a private temporary folder (mode 700, files 600).
  Gitleaks and a built-in scanner both scan that folder. If either finds
  anything, or Gitleaks fails or writes a report the tool cannot read, the
  run stops and nothing is written to the backup repository.
- Reports findings as file, line and rule only. The matched text is never
  printed or logged.
- Refuses to run without Gitleaks unless you opt in to the built-in scanner,
  and never adds allowlist entries by itself.
- Removes credentials from remote URLs in error messages.
- Touches only `<machine>/` in the backup repository and stops when that
  folder has uncommitted changes.
- Refuses a backup repository inside `~/.claude` or `~/.codex`, or one that
  contains them.
- `restore` writes only files listed in the manifest, after checking each
  file's size and SHA-256, refuses paths that leave the target folder, and
  keeps a `.bak` copy of every file it overwrites.
- No network access of its own. Pushing uses your Git setup. The optional
  visibility check calls the `gh` CLI.

## What it does not do

- The scanners find known key formats and obvious `key = value` assignments.
  They cannot tell that a sentence in a memory file is private: a customer
  name, a health detail or an internal hostname passes the scan. The backup
  repository must stay private.
- Files are stored as plain text. Anyone who can read the backup repository,
  including your Git host, can read your memory files. There is no
  encryption.
- A secret that is already in the backup repository's history stays there
  after you remove it from the source file. Rewrite or recreate the backup
  repository if that happens, and rotate the secret.
- Your own `.gitleaks.toml` allowlist and inline allow markers can hide a
  real secret. Keep them narrow.
- The public-visibility warning needs the `gh` CLI and works for GitHub
  only.

## Repository hygiene

This repository contains no real memory files, transcripts or credentials.
Everything in `examples/` and the tests is synthetic, and secret-shaped test
values are built at run time. CI scans the full Git history with Gitleaks and
with `scripts/check_public_tree.py`, which looks for private network
addresses, home-directory paths, email addresses outside example domains and
private files. Automated scans support a manual review; they do not prove
that nothing private slipped in.
