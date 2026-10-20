# Contributing

Bug fixes, corrections and support for new memory locations in Claude Code
or Codex are welcome. A new feature should keep the tool small and the
allowlist narrow.

```sh
uv sync --locked
uv run python -m pytest --cov=agent_memory_sync --cov-fail-under=90
uv run ruff check .
uv run ruff format --check .
gitleaks git --redact --no-banner .
python3 scripts/check_public_tree.py
```

The integration test uses the real Gitleaks binary:

```sh
uv run python -m pytest -m integration
```

## Rules

- Never commit a real memory file, transcript, config file or path from your
  own machine, not even in a temporary commit. Use the synthetic home folder
  in `examples/home` or build one in `tmp_path`.
- Tests must not read the real `~/.claude`, `~/.codex` or `~/.claude.json`.
  Pass `--home` or a temporary `HOME`.
- Build secret-shaped test values from fragments at run time, so this
  repository's own scans stay clean.
- Keep the runtime on the Python standard library.
- A new include rule needs a source: link the official documentation for the
  file in the pull request. A new exclude rule needs no justification beyond
  "this can hold something private".
- Do not weaken the secret scan, the symlink checks or the exclude-wins rule
  to make something work. Explain the problem in an issue instead.

Use a public-safe Git author address. Report vulnerabilities through
[SECURITY.md](SECURITY.md), not a public issue.
