# What is copied

agent-memory-sync uses an allowlist. A file is copied only when it matches an
include rule and no exclude rule. Exclude rules always win, also over your own
`include` globs. Paths are relative to the source folder.

Source folders:

- Claude Code: `$CLAUDE_CONFIG_DIR`, or `~/.claude` when it is not set.
- Codex: `$CODEX_HOME`, or `~/.codex` when it is not set.

`~/.claude.json` is outside both folders and is never read. It holds your
sign-in session and MCP server settings, which often contain tokens.

## Claude Code

| Copied | What it is |
| --- | --- |
| `CLAUDE.md` | Your user-level instructions |
| `rules/**/*.md` | Rule files |
| `projects/*/memory/**/*.md` | Auto memory that Claude writes for each project |
| `agent-memory/**/*.md` | Subagent memory |
| `settings.json` | Settings. Scanned like everything else, because its `env` block can hold secrets |
| `agents/**`, `commands/**`, `skills/**`, `output-styles/**`, `hooks/**` | Your own agents, commands, skills, styles and hooks |
| `statusline.sh`, `keybindings.json` | Status line script and key bindings |

## Codex

| Copied | What it is |
| --- | --- |
| `AGENTS.md`, `AGENTS.override.md` | Global instructions |
| `config.toml`, `*.config.toml` | Config and profiles. Scanned, because MCP server entries can hold tokens |
| `rules/**`, `prompts/**`, `skills/**`, `agents/**` | Rules, prompts, skills and agents |
| `hooks.json` | Hooks |
| `memories/**/*.md` | Memory files, when the memories feature writes Markdown |

## Never copied

| Excluded | Why |
| --- | --- |
| `.credentials.json`, `auth.json`, `.claude.json`, `*.pem`, `*.key`, `.env`, `.env.*` | Credentials, sign-in state and keys |
| Any file or folder name containing `token`, `secret`, `credential` or `password` | Likely credential notes. This also skips harmless names |
| `*.jsonl` | Session transcripts and prompt history in both tools |
| `*.sqlite`, `*.sqlite-*`, `*.db` | Databases, including Codex state and memories in SQLite |
| `cache/`, `plugins/`, `file-history/`, `shell-snapshots/`, `shell_snapshots/`, `session-env/`, `sessions/`, `archived_sessions/`, `log/`, `logs/`, `debug/`, `node_modules/`, `.git/` | Runtime state, logs and installable packages |
| `*.bak*`, `*.lock` | Backups and lock files |
| `settings.local.json` | Machine-specific permissions |
| `projects/-private-var-folders-*`, `projects/-tmp-*`, `projects/-private-tmp-*`, `projects/-var-folders-*` | Project folders from sessions started in temporary directories |

## Other rules

- Exclusions ignore letter case: `Auth.JSON`, `.ENV` and `chat.JSONL` are
  excluded too. Your own globs are case-sensitive.
- Symlinks are skipped and never followed, for files and for folders.
- Hard-linked files are skipped, because the same file can also exist under
  an excluded name.
- Files over `max_file_bytes` (1 MiB by default) are skipped.
- Binary files and files that are not UTF-8 are skipped.
- Files whose names contain `:`, a backslash or a control character are
  skipped, because `restore` could not write them back safely.

Run `agent-memory-sync plan` to see every file with the rule that included or
skipped it.

## Adding your own rules

```toml
[sources.claude]
include = ["notes/**/*.md"]
exclude = ["projects/*/memory/scratch.md"]
```

Your `exclude` globs add to the built-in ones. Your `include` globs cannot
bring back a file that a built-in rule excludes. If you need one of those
files, back it up some other way.
