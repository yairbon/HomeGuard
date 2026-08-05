<#
HomeGuard — install the shared Claude Code plugin set at USER scope (Windows).

Why this exists: the repo already carries this plugin set at *project* scope
in .claude\settings.json, so any session opened on HomeGuard picks it up
automatically (including fresh ephemeral cloud containers, which start from a
clean home directory every time). This script is for the other case — getting
the same set into every OTHER repo you work on, by declaring it once at user
scope in ~\.claude\settings.json.

Run it once per machine:
    .\scripts\setup-claude-plugins.ps1
WSL2 / Linux / macOS equivalent: scripts/setup-claude-plugins.sh

Idempotent: re-running only refreshes the marketplace clones.
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Error "'claude' is not on PATH. Install Claude Code first: npm install -g @anthropic-ai/claude-code"
}

Write-Host "== Adding marketplaces (user scope) ==" -ForegroundColor Cyan
# claude-mem — persistent memory / context compression across sessions.
claude plugin marketplace add thedotmack/claude-mem --scope user
# headroom — session-start hooks.
claude plugin marketplace add headroomlabs-ai/headroom --scope user
# Anthropic's official plugin directory. It carries 278 plugins; --sparse
# limits the git checkout to just the manifest plus the one plugin we want,
# instead of cloning the whole monorepo.
claude plugin marketplace add anthropics/claude-plugins-official --scope user `
    --sparse .claude-plugin plugins/claude-code-setup

Write-Host ""
Write-Host "== Installing plugins (user scope) ==" -ForegroundColor Cyan
claude plugin install claude-mem@thedotmack --scope user
claude plugin install headroom@headroom-marketplace --scope user
claude plugin install claude-code-setup@claude-plugins-official --scope user

Write-Host ""
Write-Host "== Installing the task-observer skill ==" -ForegroundColor Cyan
# Task Observer (rebelytics/one-skill-to-rule-them-all) ships as a bare
# SKILL.md repo with no .claude-plugin\marketplace.json, so it cannot be
# installed via `claude plugin install`. It is vendored into this repo under
# .claude\skills\task-observer\ (CC BY 4.0); copying it to ~\.claude\skills\
# makes it available everywhere, not just in HomeGuard.
$SkillSrc  = Join-Path $RepoRoot ".claude\skills\task-observer"
$SkillDest = Join-Path $HOME ".claude\skills\task-observer"
if (Test-Path $SkillSrc) {
    New-Item -ItemType Directory -Force -Path $SkillDest | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $SkillSrc "*") -Destination $SkillDest
    Write-Host "  installed -> $SkillDest"
} else {
    Write-Warning "$SkillSrc not found; run this from a HomeGuard checkout."
}

Write-Host ""
Write-Host "== Done. Installed plugins: ==" -ForegroundColor Cyan
claude plugin list
Write-Host ""
Write-Host "Restart Claude Code (or start a new session) for the plugins to load."
