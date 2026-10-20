# Restore

Use this after a new install, on a new machine, or when a memory file was
deleted or damaged.

## 1. Get the backup

```sh
git clone https://github.com/YOUR-NAME/agent-memory.git ~/backups/agent-memory
```

If the backup repository is already on the machine, run `git pull` in it.

Each machine has its own folder. Pick the label of the machine you want to
restore from:

```sh
ls ~/backups/agent-memory
```

## 2. Preview

Close Claude Code and Codex first, so they do not write to the same files
while you restore.

```sh
agent-memory-sync restore --from ~/backups/agent-memory --machine laptop
```

Nothing is written yet. Each line says what would happen to one file:

| Action | Meaning |
| --- | --- |
| `write` | The file is missing and would be created |
| `unchanged` | The file already has the same content |
| `conflict` | The file exists with different content and would be left alone |
| `overwrite` | Shown instead of `conflict` when you add `--overwrite` |

Use `--source claude` or `--source codex` to restore one tool only.

Before it does anything, `restore` checks every selected file against the
`MANIFEST.json` in the machine folder. A file whose size or SHA-256 does not
match stops the whole restore.

## 3. Write

```sh
agent-memory-sync restore --from ~/backups/agent-memory --machine laptop --apply
```

This writes the missing files only. Folders are created with mode 700 and
files with mode 600. Files that are not in the manifest are never touched.

To replace files that differ, add `--overwrite`:

```sh
agent-memory-sync restore --from ~/backups/agent-memory --machine laptop --apply --overwrite
```

Each replaced file is first copied next to itself as
`<name>.agent-memory-sync-<UTC time>.bak`.

## 4. Check

Start Claude Code and run `/memory` to see the loaded memory and instruction
files. In Codex, ask what your global instructions say.

## An older version

The backup repository is plain Git, so every earlier run is a commit:

```sh
cd ~/backups/agent-memory
git log --oneline -- laptop/claude/CLAUDE.md
git show <commit>:laptop/claude/CLAUDE.md
```

To restore a whole machine as it was at an earlier commit, check that commit
out in a separate clone and run `restore --from` against that clone.

## Restore without the tool

The files are stored under `<machine>/claude/` and `<machine>/codex/` with the
same relative paths as in `~/.claude` and `~/.codex`. You can copy them back
by hand:

```sh
cp -n ~/backups/agent-memory/laptop/claude/CLAUDE.md ~/.claude/CLAUDE.md
```

Restored auto memory is in `projects/<project-slug>/memory/`. The slug is the
project's absolute path with `/` replaced by `-`. If your home folder or
project paths differ on the new machine, rename the slug folder to match.

Credentials, sign-in state and MCP server settings are never in the backup.
Sign in again and add your MCP servers again after a restore.
