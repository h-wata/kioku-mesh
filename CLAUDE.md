# kioku-mesh — agent rules

## Git workflow

- **Do NOT push directly to `main`.** Always go through a PR: branch → push → `gh pr create` → merge after review.
- This repo is public on GitHub Free, which means branch protection rules and required PR reviews *are* available at the server side and should be enabled on `main`. Until they are, the "PR-only" rule above remains a convention that the agent and the human have to hold themselves.
- If a direct push to `main` happens by mistake, surface it immediately — do not silently continue.

## Attribution

- **Never embed a Claude Code session URL** (`https://claude.ai/code/session_…`) in commit messages, PR descriptions, PR/issue comments, or any other artifact pushed to this repo. These URLs are not externally resolvable and leak session identifiers.
- The `Co-Authored-By: Claude <noreply@anthropic.com>` commit trailer is fine and should stay.
