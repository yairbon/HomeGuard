#!/bin/bash
# HomeGuard — install the shared Claude Code plugin set at USER scope.
#
# Why this exists: the repo already carries this plugin set at *project*
# scope in .claude/settings.json, so any session opened on HomeGuard picks
# it up automatically (including fresh ephemeral cloud containers, which
# start from a clean $HOME every time). This script is for the other case —
# getting the same set into every OTHER repo you work on, by declaring it
# once at user scope in ~/.claude/settings.json.
#
# Run it once per machine / per WSL2 distro:
#   bash scripts/setup-claude-plugins.sh
# Windows-native PowerShell equivalent: scripts/setup-claude-plugins.ps1
#
# Idempotent: re-running only refreshes the marketplace clones.

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: 'claude' is not on PATH. Install Claude Code first:" >&2
    echo "  npm install -g @anthropic-ai/claude-code" >&2
    exit 1
fi

echo "== Adding marketplaces (user scope) =="
# claude-mem — persistent memory / context compression across sessions.
claude plugin marketplace add thedotmack/claude-mem --scope user
# headroom — session-start hooks.
claude plugin marketplace add headroomlabs-ai/headroom --scope user
# Anthropic's official plugin directory. It carries 278 plugins; --sparse
# limits the git checkout to just the manifest plus the one plugin we want,
# instead of cloning the whole monorepo.
claude plugin marketplace add anthropics/claude-plugins-official --scope user \
    --sparse .claude-plugin plugins/claude-code-setup

echo
echo "== Installing plugins (user scope) =="
claude plugin install claude-mem@thedotmack --scope user
claude plugin install headroom@headroom-marketplace --scope user
claude plugin install claude-code-setup@claude-plugins-official --scope user

echo
echo "== Installing the task-observer skill =="
# Task Observer (rebelytics/one-skill-to-rule-them-all) ships as a bare
# SKILL.md repo with no .claude-plugin/marketplace.json, so it cannot be
# installed via `claude plugin install`. It is vendored into this repo under
# .claude/skills/task-observer/ (CC BY 4.0); copying it to ~/.claude/skills/
# makes it available everywhere, not just in HomeGuard.
SKILL_SRC="$REPO_ROOT/.claude/skills/task-observer"
SKILL_DEST="$HOME/.claude/skills/task-observer"
if [ -d "$SKILL_SRC" ]; then
    mkdir -p "$SKILL_DEST"
    cp -r "$SKILL_SRC/." "$SKILL_DEST/"
    echo "  installed -> $SKILL_DEST"
else
    echo "  WARNING: $SKILL_SRC not found; run this from a HomeGuard checkout." >&2
fi

echo
echo "== Done. Installed plugins: =="
claude plugin list
echo
echo "Restart Claude Code (or start a new session) for the plugins to load."
