# kioku-mesh — agent rules

## Git workflow

- **Do NOT push directly to `main`.** Always go through a PR: branch → push → `gh pr create` → merge after review.
- This repo is PRIVATE on GitHub Free, so branch protection / rulesets are not enforceable at the server side (the protection API returns 403 unless upgraded to Pro). The "PR-only" rule is therefore a convention that must be held by the agent and the human alike, not a guardrail the server will catch.
- If a direct push to `main` happens by mistake, surface it immediately — do not silently continue.

## Attribution

- **Never embed a Claude Code session URL** (`https://claude.ai/code/session_…`) in commit messages, PR descriptions, PR/issue comments, or any other artifact pushed to this repo. These URLs are not externally resolvable and leak session identifiers.
- The `Co-Authored-By: Claude <noreply@anthropic.com>` commit trailer is fine and should stay.
