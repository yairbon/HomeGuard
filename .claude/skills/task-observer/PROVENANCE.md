# Provenance

This skill is **vendored**, not authored here.

- Upstream: https://github.com/rebelytics/one-skill-to-rule-them-all
- Vendored at commit: `281f13466cd3a73e9ebc9d210907748e1941a3dd` (v2.0.0)
- License: Creative Commons Attribution 4.0 International (see `LICENSE.txt`)

## Why vendored instead of installed as a plugin

The upstream repo ships a bare `SKILL.md` at its root with no
`.claude-plugin/marketplace.json`, so `claude plugin marketplace add` rejects
it and `claude plugin install` has nothing to resolve. Copying the skill
directory in is the supported way to consume a repo shaped like that.

## Updating

```bash
git clone --depth 1 https://github.com/rebelytics/one-skill-to-rule-them-all /tmp/task-observer
cp /tmp/task-observer/SKILL.md            .claude/skills/task-observer/
cp /tmp/task-observer/references/*.md     .claude/skills/task-observer/references/
cp /tmp/task-observer/LICENSE.txt         .claude/skills/task-observer/
```

Then bump the commit hash recorded above. If upstream ever adds a real
marketplace manifest, drop this directory and install it as a plugin instead
(alongside the other three in `.claude/settings.json`).
