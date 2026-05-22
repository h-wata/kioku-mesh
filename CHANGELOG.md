# Changelog

All notable changes to this project will be documented in this file.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

mesh-mem is in `0.x`: APIs and on-disk storage schema may change between minor
versions without a migration path until `1.0.0`.

## [Unreleased]

### Fixed

- **Tier 1 mesh integration** (#112 post-merge fix): `mesh-mem mesh start` now starts an index subscriber within the router process, so peer saves published via `ZENOH_CONNECT` are written to the router's local SQLite index and visible from `mesh-mem search` in the router context. `mesh join` is now foreground (Ctrl-C to stop) and also starts a replication subscriber. Addresses post-merge review B1/B2/I1/I2/N1.
- **`mesh start` peer hint now shows real host IP** (B3 fix): when listening on a wildcard address (`0.0.0.0`), the startup message now auto-detects the host's LAN IP(s) and shows separate hints for same-host (`127.0.0.1`) and other-host connections. Previously the other-host hint showed loopback `127.0.0.1`, causing remote peers to connect to their own loopback.
- **`mesh-mem doctor` connected-peer count**: `check_embedded_router` uses `router_zids` from an external peer probe as an approximation; full peer enumeration requires in-process router state and is deferred to #113.
- **Forward-compatibility for Observation schema**: when a peer running
  an older release receives a PUT carrying fields it doesn't know about,
  those fields are now preserved via a `_extras` side channel and re-emitted
  on `to_json`, instead of being silently stripped on SQLite round-trip.
  Fixes silent data loss during rolling upgrades. (#75)
- **`mesh-mem-mcp` interactive misinvocation now exits with usage**
  instead of starting the stdio loop and flooding stderr with
  JSON-RPC parse errors. Set `MESH_MEM_MCP_ALLOW_TTY=1` to bypass
  the check for protocol-level debugging. (#98)

### Added

- **Tier 1 embedded zenoh router** (#112): `mesh-mem mesh start` opens an in-process zenoh router (`mode=router`) with configurable TCP listen endpoint (default `tcp/0.0.0.0:17447`) — no `zenohd` binary required. `mesh-mem mesh join <peer>` opens an in-process peer session and verifies connectivity. `mesh-mem doctor` reports embedded router reachability via `MESH_MEM_ROUTER_ENDPOINT` (default `tcp/localhost:17447`). Backend abstraction unchanged: no tier-specific branch in `store.put_observation()`.
- **Introduce `local` backend** (#109): `mesh-mem init --mode local` provisions a config that does NOT require `zenohd` on PATH. The existing SQLite store (`local_index.py`) is promoted from sidecar to first-class backend. Both the CLI and MCP server route through the same `MemoryBackend` abstraction so the demo path and the agent path are byte-for-byte the same code. Select with `mesh-mem init --mode local` or `MESH_MEM_BACKEND=local`. Unlocks: `mesh-mem demo` (#108) and issues #110–#113.
- **README adds an "Install zenohd" section** (#83) before the Quick start. Covers the apt one-liner (Eclipse Debian repo) plus the `zenohd --version` + rocksdb-backend-loaded verify steps so a Debian / Ubuntu first-touch user can get a working router without reading upstream Zenoh docs. macOS / Windows / non-apt Linux paths defer to [zenoh.io/docs/getting-started/installation](https://zenoh.io/docs/getting-started/installation/) rather than embedding fragile prebuilt-zip / cargo recipes that would drift out of sync with upstream. The existing `## Requirements` Zenoh 1.9 bullet cross-links to the new section.
- **`mesh-mem doctor` diagnostic command** (#84). Runs a small set of deterministic checks so first-touch users can answer "why isn't this working" without reading three separate README sections: `zenohd_binary` (PATH lookup), `config_file` (`~/.config/mesh-mem/zenohd.json5` present), `zenohd_reachable` (TCP probe to `ZENOH_CONNECT`, default `tcp/localhost:7447`), and `state_dir_hardlinks` (writable + POSIX hard-link capable, the same constraint `get_pc_id` relies on). Each result carries `status` (`pass`/`warn`/`fail`), one-line `summary`, actionable `hint`, and machine-readable `details`. Exit code reflects the worst severity (0/1/2) for scripting. `--json` emits `{ok, worst_status, checks: [{name, status, summary, hint, details}]}`. Process-owner discrimination, time-sync inspection, and MCP-client registration probes are deferred — they are platform-specific and easier to misdiagnose than to skip; the v0.3 scope is the testable core.
- **`client_id` defaults to `<user>@<host_short>` when env-unset** (#82). v0.3 onboarding: first-touch users no longer need to export `MESH_MEM_CLIENT_ID` for observations to carry an informative identity. `agent_family` keeps the `unknown` default — it's an aggregation axis where the cost of misclassification (e.g. labeling a non-Claude session `claude` because an env var happened to leak) outweighs the cost of an uninformative default. Launcher detection is deferred. `mesh-mem status` now shows the provenance of each identity value (`from MESH_MEM_AGENT_FAMILY` / `default — set MESH_MEM_AGENT_FAMILY to override`) so users can confirm what's being written. New `identity.IdentitySource` enum (`env` / `detected` / `default`) and `resolve_agent_family()` / `resolve_client_id()` helpers expose the value+source tuple. Identity segments are sanitized against Zenoh-reserved characters (`/ * ? $ #`) before they enter the key namespace.
- **`mesh-mem mcp install --client <claude-code|codex-cli>` for one-shot MCP registration** (#85). Removes the largest first-touch friction for AI-coding-agent users: instead of reading `docs/mcp-clients.md` and picking the right per-client recipe, one command bakes the absolute path to `mesh-mem-mcp` and sensible env defaults into the chosen client's config. Claude Code goes through `claude mcp add -s user ... -e ... -- <path>` (the only registration path Claude Code actually reads). Codex CLI gets a `[mcp_servers.<name>]` TOML block in `~/.codex/config.toml`, with idempotent block-level substitution that preserves other servers and user comments outside the block. Flags: `--name` (registry key, default `mesh_mem`), `-e KEY=VALUE` (env override; repeatable), `--dry-run` (print without executing), `--force` (replace existing registration). Claude Desktop, Gemini CLI, and ChatGPT Desktop are deferred (Claude Desktop pending #87 macOS / Windows verification; Gemini and ChatGPT Desktop pending stable upstream config schemas) — their manual recipes remain in `docs/mcp-clients.md`.
- **`mesh-mem init --install-systemd`** (#86). One extra flag on `init` writes a user-scope systemd unit at `~/.config/systemd/user/mesh-mem-zenohd.service` (XDG-aware) pointing at the same `zenohd.json5` the init step wrote, with the absolute `zenohd` path baked in so the user manager doesn't need shell PATH. `--print` extends to emit both bodies (config + unit) separated by a comment header so the user can split them. `--force` covers both files. The platform check refuses cleanly on macOS / Windows / hosts without `systemctl`. Missing `zenohd` binary degrades to a warning + documented fallback path rather than aborting — the unit is still installable, the user just edits ExecStart before enabling.
- **`Observation.references` field** for first-class PR / Issue / external identifiers (#73). CLI: `mesh-mem save --references "#67,PR#68"`. MCP: `references=["#67", "PR#68"]`. Old JSON without the field deserializes to `[]`.
- **Shell completion for the `mesh-mem` CLI** via `argcomplete` (#76). Install the new `completion` extra (`pip install -e '.[completion]'`) and run `eval "$(register-python-argcomplete mesh-mem)"` from `.bashrc` / `.zshrc`. Subcommands, static flags, and `--memory-type` complete from argparse metadata; `--project` / `--pc-id` / `--by-pc-id` use dynamic completers that read distinct values from the **local SQLite index only** (no Zenoh round-trip, no `rebuild_from_zenoh`), so tab-completion stays sub-100 ms even on populated meshes. `argcomplete` is optional — if it is not installed the CLI behaves exactly as before.

### Changed

- **MCP tool descriptions reinforced for proactive save**: `save_observation`,
  `search_memory`, and `get_memory_status` docstrings now carry per-tool
  PROACTIVELY reminders so the protocol stays active in long sessions where
  the server instructions may have been pushed out of the context window.
  `get_memory_status` output now includes `last_save_at` (ISO timestamp of the
  most recent index entry) to surface skipped saves as a self-check hint. (#51)

- **`mesh-mem delete` no longer aborts at 10 000 matches** (#66). The bulk-delete path now pages via a `(created_at, observation_id)` DESC cursor (`LocalIndex.search` gained an inclusive `until_iso` filter and a stable tiebreaker), tombstoning all matching rows regardless of size. `--batch-size` (default `1000`, max `MAX_SEARCH=10000`) controls per-page and progress granularity. Individual `put_tombstone` failures no longer abort the sweep — they increment a `failures` counter and the process exits non-zero only at the end. When the target set exceeds `MAX_SEARCH` the interactive prompt prints an extra warning suggesting `mesh-mem --rebuild gc --retention-days 0 --project ...` as the faster path when the rows live only in the local SQLite index (ADR-0010 / ADR-0011 shadow-sweep). The same hint is appended to stderr on every bulk-delete completion to discourage raw `DELETE FROM obs_index` workarounds.

- **MCP server instructions add an explicit SKIP list and type/importance guidance** (#73). PR/Issue lifecycle ticks, restated PR/ADR/CHANGELOG content, in-conversation progress logs, and bare `tests pass` notes are now called out as save-skip cases. `decision` / `bug` / `pattern` / `config` are preferred over `summary`; `importance` 1-2 invites reconsidering whether to save at all.
- **Docs: install guidance now leads with `uv tool install`.** README Quick start and `docs/mcp-clients.md` recommend `uv tool install git+https://github.com/h-wata/mesh-mem.git` (or `--editable .` for a local checkout), which exposes `mesh-mem` / `mesh-mem-mcp` at `~/.local/bin/` without venv activation or full-path invocation. MCP registration examples updated accordingly. The manual `python3 -m venv ~/.venv/mesh-mem` flow is retained as a fallback. No code or runtime behaviour change.
- docs: README rewritten around v0.3 hero + Tier 0/1/2 narrative (#110)
- docs: README Power users section polish — Features ordering, internal anchors, mesh-specific doctor placement (#111)

### Documentation

- **README rewritten for v0.3 first-impression**: top-down structure with a
  one-paragraph pitch, demo placeholder, Wave 1-2 quick start (init / doctor /
  mcp install), and a "What you get" capabilities list. English-primary; Japanese
  sections clearly demarcated. (#89)
- **README "Status & known limitations" reframed as design scope** (#88): the section now leads with the LAN/VPN trusted-peer design statement, separates Versioning (SemVer commitment) from Operational notes (cold-era resync, gc broadcast, MAX_SEARCH cap), and keeps the "don't expose to untrusted networks" callout intact. No factual claims removed.

## [0.2.5] - 2026-05-19

### Added

- **`gc --retention-days` now sweeps shadowed index rows alongside tombstones**
  (#70). After #67 introduced shadow-delete, long-shadowed rows had no
  physical-removal path and grew the SQLite index forever. The retention
  sweep now collects shadow rows whose `shadowed_at` predates the same
  cutoff used for tombstones, **re-verifies each candidate against the
  live Zenoh state**, and either upserts the row back to live (false-
  shadow recovery — the upstream obs reappeared since the rebuild that
  flagged it) or physically deletes it (genuine expiry). The CLI driver
  additionally runs `rebuild_from_zenoh` before the sweep so that
  stale-but-not-yet-shadowed local rows enter the discovery branch on
  a one-shot `mesh-mem gc` invocation (CLI startup skips rebuild by
  default per #38). If the live query fails the sweep is skipped
  entirely — never delete on transport ambiguity. Output reads
  `retention N-day sweep: physically deleted {n} tombstones / {m} shadows (revived {k})`.
  Pass `--no-shadow-prune` to opt out (tombstone-only sweep, prior
  behavior; rebuild is also skipped in that branch). The shadow sweep
  is otherwise local-only — no Zenoh delete is issued for the purged
  half because the upstream key is already absent; other peers run the
  sweep independently and converge.

### Changed

- **All user-facing CLI / MCP runtime strings are now English** (#53). Previously
  `mesh-mem` CLI prints (`save`, `search`, `delete`, `status`, `drain`, `gc` and
  argparse `--help`) and MCP tool returns (`save_observation`, `search_memory`,
  `get_memory`, `delete_memory`, `get_memory_status`, `drain_pending_puts`)
  mixed Japanese and English. They are now uniformly English to match the
  already-English `logger.*` output and to keep MCP responses safely parseable
  by non-Japanese agents. The Japanese trigger phrase `"前にやった"` inside the
  MCP `instructions` field is preserved on purpose — it is a deliberate hint
  for recognizing Japanese user input. **Breaking** for any script that greps
  Japanese substrings from CLI or MCP output (e.g. `保存完了`, `削除`, `件数`).

### Fixed

- **Rebuild now reconciles SQLite index against Zenoh, not just appends to it**
  (#67). `LocalIndex.rebuild_from_zenoh` was add-only: it upserted whatever
  it saw in Zenoh but never pruned `existing - zenoh_set`. Combined with
  the subscriber gap fixed in #65, that left long-lived ghost rows after
  a peer purged keys on Zenoh while another peer was offline. Rebuild now
  marks `existing` rows that did not appear in the Zenoh scan as
  *shadowed* via a new `shadowed_at` column. Shadowed rows are hidden
  from `search` / `find_by_id` (same as tombstones) but a later upsert —
  including replay of the obs from Zenoh — clears the shadow and the row
  comes back to life. Tombstones remain stronger than shadows: applying
  a tombstone clears any prior shadow, and rebuild no longer overwrites
  an existing tombstone's `deleted_at` timestamp. Rebuild also skips
  writes for live rows whose `payload_json` is unchanged, avoiding WAL
  inflation on populated meshes (ADR-0007 / Issue #32).
- **`get_memory_status` exposes index visibility counts**. Output now
  includes `index_rows: live=N / tomb=N / shadow=N` so operators can see
  the read-path state, not just the Zenoh-scan totals.

### Added

- **`LocalIndex.mark_shadowed_missing` + `VisibilityCounts`**. New
  index methods backing the rebuild reconcile path. Schema migrates
  forward from v1 to v2 by adding a `shadowed_at TEXT` column;
  existing rows are treated as live until the next rebuild revisits
  them.

### Fixed

- **Replication subscriber now mirrors Zenoh DELETE into the SQLite index**
  (#64). `start_index_subscriber` previously parsed every `mem/obs/**` /
  `mem/tomb/**` sample as JSON and silently dropped DELETE-kind samples
  (empty payload → `JSONDecodeError` → DEBUG log). As a result, a
  `mesh-mem gc --by-pc-id ... --execute` (or any `session.delete` on an
  obs/tomb key) issued on one peer purged Zenoh storage and that peer's
  local index, but left ghost rows in every other peer's
  `~/.local/share/mesh-mem/index.db`, inflating `get_memory_status`
  counts and search hits. The subscriber now dispatches on `sample.kind`
  and calls `LocalIndex.physical_delete` for DELETE samples whose key
  ends in a 32-hex `observation_id`. Malformed keys (wrong length, non-
  hex, missing trailing segment) are conservatively ignored.

### Added

- **Local fallback queue for failed puts** (#50). `put_observation` /
  `put_tombstone` retryable failures are now persisted to
  `state_dir()/pending_puts.db` and replayed on the next successful save.
  `pending_puts` count is exposed in CLI `status` and MCP
  `get_memory_status`.
- **Startup and manual drain for pending puts** (#57). `mesh-mem-mcp`
  now starts a daemon background drain on startup when transport is
  reachable and queued `pending_puts` exist. Operators can also trigger
  replay explicitly via `mesh-mem drain --pending [--limit N]` or the MCP
  `drain_pending_puts` tool.
- **`mesh-mem search --format {text,markdown,json}`** (#58). Search now
  supports stable machine-oriented JSON output plus single-line markdown
  bullets suitable for SessionStart hooks, while preserving the previous
  human-readable text output as the default.
- **Sample Claude Code SessionStart hook script** (#58). Added
  `scripts/hooks/session-start.sh` plus README setup instructions for
  loading recent mesh-mem context into a new Claude Code session.

### Changed

- **`TransportStatus` schema gained `pending_puts: int`**. Callers that
  destructure the dataclass need to pick up the new field.
- **Drain progress is surfaced in status output**. CLI `status` and MCP
  `get_memory_status` now report whether a drain is in progress, the last
  drain timestamp, and the cumulative number of queued rows replayed by
  the current process.

## [0.2.4] - 2026-05-11

### Added

- **`mesh-mem gc --by-pc-id PCID [--session-prefix X] [--execute] [--yes]`**:
  bulk physical purge of every observation that was saved under a given
  ``pc_id``, optionally narrowed by ``session_id`` prefix. Use case: a
  benchmark / smoke run on a peer host saved tens of thousands of
  synthetic observations under throwaway sessions and they are now
  flooding the mesh. ``--execute`` is required to actually delete; the
  default is dry-run with a per-session histogram. With ``--execute`` the
  CLI also gates on an interactive ``yes`` prompt (skip with ``--yes``
  for CI / scripted use; non-interactive ``--execute`` without ``--yes``
  is rejected with exit 2 so an operator cannot pipe the command into a
  background job and have it auto-destroy). For every matched obs the
  mirrored ``mem/tomb/...`` slot is also exact-key deleted, so legitimate
  tombstones under the same ``pc_id`` are cleaned up at O(1)/match
  without falling back to the ``mem/tomb/**`` global sweep that
  ``--force-id`` performs (the sweep stalls on ``GET_TIMEOUT`` past 30k
  tombstones). Backed by ``store.scan_obs_by_pc_id`` +
  ``store.execute_bulk_purge``.

### Changed

- **CLI skips `rebuild_from_zenoh` on startup by default** (#38). The
  one-shot `mesh-mem` process previously paid the full ~15 s zenoh
  scan + JSON-parse + SQLite-membership-check on every invocation
  against a populated mesh, which made interactive use unworkable on
  busy peers (~117k records observed). The local SQLite index still
  converges via the replication subscriber within the process
  lifetime, so `save` / `search` / `get-memory` / `delete` / `status`
  see live writes without the rebuild. Long-running processes
  (`mesh-mem-mcp`, autonomous agents) keep the previous behavior —
  the rebuild cost amortizes across their uptime.
  Opt back in per-invocation with `mesh-mem --rebuild ...` or via the
  new `MESH_MEM_FORCE_REBUILD=1` env var. ``--rebuild`` uses the new
  explicit-override channel (codex review P2) so it outranks even an
  ambient ``MESH_MEM_SKIP_REBUILD=1`` exported from a shell profile or
  wrapper script — direct user intent on the typed invocation always
  wins over env-level config. Resolution order: explicit override >
  ``MESH_MEM_FORCE_REBUILD`` > ``MESH_MEM_SKIP_REBUILD`` > module default.
- **One-off migration scripts moved under ``scripts/migrations/``**.
  ``cleanup_legacy_memory_types.py`` (v0.2.2 → v0.2.3 enum migration)
  is operator tooling that should not be shipped as a CLI subcommand,
  but should still travel with the repo for any peer that has not yet
  migrated. The ad-hoc ``scripts/purge_observations_by_pc_id.py`` is
  removed in favor of the CLI flag above.

### Fixed

- **CLI commands no longer hang on exit** (#44). ``mesh-mem`` short-lived
  invocations now explicitly close the Zenoh session on exit (including
  on early returns / exceptions). Previously the session lingered until
  process teardown, which on some hosts left the CLI waiting on its own
  background tasks and made shell scripts that chain mesh-mem commands
  unusable.

### Performance

- **Project-scoped gc switches to the SQLite local index** (#32-A).
  ``gc_expired_tombstones(project=...)`` previously enumerated the entire
  Zenoh ``mem/tomb/**`` namespace (~60s on production data with months
  of test residue) regardless of how few tombstones actually matched the
  project. The new fast path queries the local index for
  ``(project, deleted_at)`` rows, then issues exact-key deletes — O(N)
  on the project-scoped subset, not O(M) on the global tombstone count.
  Always realigns the index via ``rebuild_from_zenoh`` before the SQLite
  query (codex review P1) — a non-empty sidecar from earlier short-lived
  CLI runs may be partial, and gating the rebuild on ``row_count() == 0``
  would silently miss older project tombstones. Falls through to the
  legacy global scan when the index is disabled
  (``MESH_MEM_DISABLE_INDEX=1``) or the fast path raises.
- **SQLite WAL bounded checkpoint policy** (#32-B). Long-running
  ``mesh-mem-mcp`` processes hold the index connection open
  indefinitely, which blocks SQLite's automatic checkpoint from
  completing the truncate phase — observed WAL grew to 130 MB (≈ same
  size as the main DB) on a host that had been writing for weeks.
  ``LocalIndex`` now issues an explicit
  ``PRAGMA wal_checkpoint(TRUNCATE)`` every 256 upserts and once on
  ``close()``, keeping the WAL bounded without introducing a
  background thread.

### Documentation

- **README Windows quick start refreshed** (#36): drop the misleading
  `pip install mesh-mem` line (the package is not on PyPI yet); call
  out the user-local zenohd install path for non-admin (`%LOCALAPPDATA%\Programs\zenoh\`)
  alongside the admin `Program Files\zenoh\` path; pin the exact zip
  asset names (`zenoh-1.9.0-x86_64-pc-windows-msvc-standalone.zip` and
  the rocksdb backend equivalent) so users stop guessing among the four
  naming patterns; clarify that `New-NetFirewallRule` needs an elevated
  PowerShell and that outbound-only peers can skip the inbound rule
  entirely; document the venv path-style mapping
  (`~/.venv/mesh-mem/bin/<bin>` → `Scripts\<bin>.exe`); cross-link the
  new `--rebuild` opt-in (#38) for first-time alignment on a populated
  mesh.
- **Spec.md, ADR-0006..0009, hub-and-spoke topology PoC report**
  (#41–#43): canonical specification, four new ADRs (hub-and-spoke
  topology, SQLite local index sidecar, project-aware O(N) gc, MCP
  server instructions protocol — supersedes ADR-0003 and ADR-0005),
  and an empirical 3-PC topology verification report.

## [0.2.3] - 2026-05-08

### Added

- **`memory_type` is now validated against a closed enum**
  (`note`, `decision`, `bug`, `pattern`, `config`, `summary`, exposed as
  `mesh_mem.models.VALID_MEMORY_TYPES`). The MCP `save_observation` tool
  returns a friendly error string and refuses to persist when an LLM
  passes an out-of-enum value (regression introduced when the v0.2.2
  PROACTIVE SAVE protocol shipped without a corresponding type guard);
  the CLI's `--memory-type` choices are derived from the same constant.
  ``Observation.from_json`` clamps unknown values from peers to
  ``"note"`` with a WARNING log, preserving forward-compat with peers
  on a future-extended schema.
- **README §"Non-interactive smoke from `claude -p`"**: documents the
  `--permission-mode bypassPermissions` flag required for MCP tool
  calls in `-p` mode (without it, the first tool call lands in
  `permission_denials` and the LLM exits with "permission needed").
  (#34)

### Changed

- **CLI `--memory-type` choices narrowed.** v0.2.2 accepted
  `note / decision / bugfix / discovery / config / pattern / fact /
  status / learning`; v0.2.3 accepts only the canonical six listed
  above. New `mesh-mem save --memory-type bugfix` (or `discovery` /
  `fact` / `status` / `learning`) is now rejected by argparse.
  Existing observations on the mesh whose `memory_type` is one of
  the dropped values continue to display unchanged — this is a
  write-side restriction, not a read-side one.
- **`README` Windows host setup marked Experimental** and now opens
  with a "WSL2 strongly recommended" callout. Native Windows is not
  in CI; the section remains for the rare cases (e.g. Claude Desktop
  on Windows) where WSL2 is not an option. (#36 partial — sub-points
  1, 2, 4 still open.)

### Fixed

- **Issue #31: index subscriber non-JSON payloads no longer log at
  WARNING.** `gc` broadcast-purge and similar control payloads can
  arrive on `mem/obs/**` / `mem/tomb/**` with non-Observation bytes;
  the subscriber now catches `JSONDecodeError` specifically and logs
  at DEBUG, while other exceptions continue to log at WARNING. A new
  unit test asserts no WARNING is emitted for non-JSON payloads.

## [0.2.2] - 2026-05-08

### Added

- **MCP server now ships a PROACTIVE SAVE protocol** via FastMCP's
  `instructions=` field. Claude Code (and any MCP host that surfaces
  `initialize_result.instructions`) now sees the trigger list —
  decision / bug / discovery / pattern / config / feature /
  preference / session summary — on connect, so coding agents
  auto-call `save_observation` without per-project CLAUDE.md tweaks.
  Previously the tool was registered but had no in-band signal
  telling agents *when* to use it, so dogfooding fell back to manual
  saves only. A smoke test pins the protocol so future refactors
  can't silently drop it.
- **GitHub Actions CI** (`.github/workflows/ci.yml`) running pre-commit
  and `pytest tests/` on `ubuntu-24.04` with Python 3.12, triggered on
  every PR and on push to `main`. (#22)
- **Claude Code Action workflow** (`.github/workflows/claude.yml`)
  posting an automated AI review on every PR and responding to
  `@claude` mentions in comments. Requires `ANTHROPIC_API_KEY`
  repo secret to be set by the maintainer. (#23)

### Changed

- **`_search_via_zenoh` filter evaluation order is now test-locked at
  the internal-state level**: a unit test asserts that an item which
  matches `keyword` but fails `project` is never registered in
  `results_by_id`, catching a regression that final-result inclusion
  tests would miss. The first of the existing filter-order tests had
  its docstring re-aligned with the assertion. (#13, Codex review
  IMPORTANT 2 follow-up)
- **`scripts/smoke_5peer_mesh.py` `_cli_search_count()` now raises** on
  non-zero exit rather than collapsing the failure to `0`. This
  separates replication zero-result from CLI / transport failure and
  makes flaky-test triage tractable. (#14, Codex review IMPORTANT 5
  follow-up)
- **`scripts/smoke_5peer_mesh.py` `_start_router()` closes the parent's
  log-file handle** once `subprocess.Popen` has duped the fd into the
  child, freeing Windows from the open-handle delete-block during
  cleanup. (#15, Codex review IMPORTANT 7 follow-up)
- **`scripts/smoke_5peer_mesh.py` `_cli_save()` parsing now anchors on
  a 32-char hex `observation_id` regex** instead of `split()[-1]`, so
  adding a trailing summary line to the save CLI output no longer
  silently corrupts the smoke. (#18, Codex review NICE-TO-HAVE 3
  follow-up)
- **`scripts/smoke_5peer_mesh.py` Phase 1 connectivity check now raises**
  on missing links instead of printing-and-continuing, so a partial
  mesh fails fast in Phase 1 rather than producing confusing results
  in Phase 2/3. (#17, Codex review NICE-TO-HAVE 2 follow-up)

### Removed

- **Claude Code Action workflow** (`.github/workflows/claude.yml`,
  introduced in #23) removed. In the current dev flow Claude is
  already involved in authoring most diffs, so a same-model
  auto-review on the merged result added little independent value;
  cross-vendor review (Codex) is run manually for high-stakes
  changes instead. (#26)

### Documentation

- **`state_dir()` clarifies `MESH_MEM_STATE_DIR=''` semantics**: an
  empty string is treated as "not set" and falls through to the per-OS
  default. v0.2.0 interpreted an empty string as the current working
  directory; v0.2.1+ does not. Use `MESH_MEM_STATE_DIR=.` to keep the
  cwd-relative behavior. A unit test pins this fallback. (#16, Codex
  review NICE-TO-HAVE 1 follow-up)

## [0.2.1] - 2026-05-02

### Added

- **5-peer mesh config template** (`config/zenohd_peer.json5.template`)
  with `{SELF_IP}` / `{PEER_N_IP}` placeholders, plus a 5-host walkthrough
  (`config/peers/example_5peer.md`) including sample IPs, ufw / iptables
  rules, and verification commands. (`2a6beae`)
- **README "Multi-agent identity" section** explaining how to run
  multiple Claude Code / Codex / autonomous agents on a single host
  using distinct `MESH_MEM_CLIENT_ID` values, with naming conventions,
  `direnv` examples, and MCP harness env-block configuration. (`2a6beae`)
- **README "Multi-host mesh setup" section** documenting N-peer setup
  steps (topology, per-peer config, firewall, boot, verify) with a
  troubleshooting table. (`2a6beae`)
- **README "Windows host setup" section** covering zenohd install,
  NSSM service registration, `New-NetFirewallRule` for TCP 7447, and
  `w32tm` time-sync verification. Documentation only; the project has
  not yet field-tested a mixed-OS LAN/VPN deployment. (`3fe7161`)
- **`mesh-mem status` `mesh_ready` field** reporting `yes` once the
  local node has at least one successful peer probe and has been up
  for the minimum settle time (~5 s warm in the localhost smoke; cold-era
  catch-up may take longer). Informational only; no API change. (#8, `63c2907`)
- **5-peer mesh smoke test** (`scripts/smoke_5peer_mesh.py`) that runs
  five zenohd routers **on localhost**, verifies 100/100 observation
  propagation in Phase 2, peer-restart convergence in Phase 3, and
  latency p50/p99 in Phase 4. Two consecutive runs PASS; this validates
  the wiring at peer count 5, not real LAN/VPN deployment. (`a93681d`,
  `6bfd0a9`)

### Changed

- **`state_dir()` now resolves per-OS**:
  Linux keeps the fixed `~/.local/share/mesh-mem` path
  (`XDG_DATA_HOME` is intentionally NOT honored to preserve
  pre-v0.2.1 behavior and avoid a silent migration for users who
  set it). macOS uses `~/Library/Application Support/mesh-mem` and
  Windows uses `%LOCALAPPDATA%\mesh-mem` via `platformdirs`. The
  `MESH_MEM_STATE_DIR` environment-variable override is unchanged
  on all OSes. New runtime dependency: `platformdirs>=4.0`.
  macOS / Windows users who previously placed data outside the new
  default location should set `MESH_MEM_STATE_DIR` before the first run
  after upgrade to keep using the existing store; otherwise an empty
  store is created at the new default and the old data remains
  untouched at the previous path. (`3fe7161`, `3325109`; Codex review
  BLOCKER fix — Linux silent migration when `XDG_DATA_HOME` is set)
- **`smoke_5peer_mesh.py` cleanup hardened**: routers we started are
  terminated by PID first, with a `cmdline`-verified port lookup as
  fallback only for orphan `zenohd` processes — closing a TOCTOU
  where a port reuse during cleanup could SIGKILL an unrelated
  process. A shared `_wait_for_rocksdb_lock_to_disappear()` helper
  raises `RuntimeError` from both `_graceful_stop_router()` and
  `_cleanup_smoke_processes()` when the RocksDB `LOCK` file persists
  past the deadline, surfacing a hung previous `zenohd` instead of
  silently continuing. Idempotent reruns no longer leave residual
  rows nor risk killing an unrelated process. (Codex review BLOCKER
  + IMPORTANT; `6bfd0a9`, `79694b2`, `1b81e7a`)

### Fixed

- **`search_observations` Zenoh fallback filter order is now
  test-locked**: tombstone → project / identity → since → keyword.
  This eliminates the post-restart "`--project` returns 0 while empty
  keyword returns rows" race observed when `MESH_MEM_DISABLE_INDEX=1`
  is in effect. The SQLite-first read path (v0.2.0 default) was
  already race-free via `PRIMARY KEY` deduplication and indexed
  filtering. (closes #8, `63c2907`)

- **`mesh_ready` no longer hangs on an empty store**: a successful
  zero-reply probe is now treated as ready, so freshly initialised
  deployments stop reporting permanent `waiting` in `mesh-mem
  status`. (Codex review IMPORTANT, `9e61871`)
- **`scripts/smoke_5peer_mesh.py` no longer hardcodes a developer
  home directory**: the result YAML path is configurable via
  `--result-yaml` and the script is documented as POSIX-only.
  (Codex review IMPORTANT, `9e61871`)

### Added (test deps)

- **`PyYAML>=6.0`** added to the `test` optional-dependency, fixing
  the missing dependency that would otherwise break the 5-peer smoke
  runs in clean environments. (Codex review IMPORTANT, `79694b2`)

## [0.2.0] - 2026-05-01

### Added

- **SQLite local index sidecar** for fast observation search. Populated on
  every `save_observation` / `put_tombstone` and rebuilt from Zenoh-RocksDB
  on startup. Keeps results consistent after restart and cross-host
  replication. (#7, 8b06c14 / 73e8ba2 / f195cd5)
- **Tier-4 benchmark** verifies `search_observations` stays sub-200 ms at 50k
  observations (6.47 ms p50 limit=1000, 352× faster than Tier-3 baseline).
  (#7, c06f0b0)
- **Observation schema extended** with six optional structured fields:
  `memory_type`, `importance`, `subject`, `summary`, `source_files`,
  `supersedes`. All fields default to backward-compatible values; old
  observations decode correctly with `from_json`. (#9, 7a5ccd3 / 469a516)
- **MCP tool `save_observation`** accepts the six new structured fields
  (all optional). (#9, 469a516)
- **MCP tool `get_memory(observation_id)`** returns the full record including
  all structured fields. (#9, 469a516)
- **CLI `mesh-mem save`** accepts `--memory-type`, `--importance`, `--subject`,
  `--summary`, `--source-files`, `--supersedes`. (#9, b4e9fc0)
- **CLI `mesh-mem get-memory <id>`** fetches a single observation by full
  32-char ID. (#9, b4e9fc0)
- **CLI `mesh-mem gc --project <name>`** scopes retention sweep to one project,
  preventing accidental cross-project tombstone deletion. (#11, 2faad5b)
- Issue #8 reproduction script (`scripts/repro_issue_8.py`) with 2-router
  localhost configs; reveals Zenoh routing behavior and a latent
  `observation_id` deduplication gap. (#8, b06661f)
- ADRs 0001–0005 documenting PoC design decisions (transport choice,
  tombstone semantics, filter strategy, identity env, gc scope).
- `config/zenohd_localhost.json5` for single-host development without LAN
  peers. (`844f1a3`)
- Systemd drop-in override example `docs/systemd-zenohd-override.example.conf`
  for auto-starting zenohd via the apt-packaged unit. (#3, `c4cfaee`)
- `fastmcp` added as a `test` extra dependency enabling MCP smoke tests.
  (#4, `e5768c4`)
- DR 24h test writer script `scripts/run_dr_writer.sh`. (`4aca9f1`)
- Benchmark script `scripts/bench_bulk_save.py` (Tier-1/2/3). (`66e0a08`)

### Changed

- **`search_observations` / `find_observation_by_id` now read from the
  SQLite local index by default.** Latency at 50 k observations: sub-200 ms
  (was 2.2 s at 16 k with the full Zenoh scan). Set `MESH_MEM_DISABLE_INDEX=1`
  to revert to the Zenoh full-scan fallback. (#7, f195cd5)
- **`search_memory` (MCP) and `mesh-mem search` (CLI) output format** now
  shows `[memory_type][importance] created_at (project) subject` on line 1
  and `summary` (or `content[:80]`) on line 2, separated by `---`. (#9)
- **Default `limit` unified to 50** across CLI (`mesh-mem search`), MCP
  (`search_memory`), and API (`search_observations`). Previously 20 for
  CLI/MCP and 50 for API. (#1, `c0f5194`)
- `ZENOH_BACKEND_ROCKSDB_ROOT` default path aligned to
  `~/.local/share/mesh-mem` (was `~/.local/share/zenoh-mem`). (#2, `2a39ff5`)
- `config/zenohd_home.json5` and `config/zenohd_office.json5` LAN IP
  placeholders reverted to `192.168.3.x / 192.168.3.y`; hardcoded deployment
  IPs removed. (`36c12b7`)

### Fixed

- **Default search `limit` unified** to 50 across all interfaces. (#1, `c0f5194`)
- `search_observations` zenoh fallback path now deduplicates results by
  `observation_id`; the SQLite-first path was already deduplicated by
  `PRIMARY KEY`. Surfaces in multi-router topologies. (#12, `8cb0f54`)
- `test_search_respects_since_iso_filter` pinned to a fixed `created_at`
  value, eliminating CI clock dependency. (`40b1fe9`)

### Documentation

- README `## Time sync` section expanded: `chrony` installation, `chronyc
  tracking` / `chronyc sources -v` / cross-host `date -u` verification,
  `timedatectl` warning (12.75 s drift observed with `synchronized: yes`),
  `chronyc makestep` recovery, and links to NTP skew PoC results. (#10, `69cc40b`)
- `plan.md` and `README.md` synced to as-built state: Observation schema,
  MCP/CLI signatures, PoC verification results summary, and open issues
  section. (`88e2019`)

### Security

- Replaced hardcoded LAN IPs in zenohd config templates with
  `192.168.3.x / 192.168.3.y` placeholders to avoid leaking
  deployment-specific addresses. (`36c12b7`)

---

## [0.1.0] — 2026-04-24

Initial tagged release. Experimental / early preview.

### Added
- Python package `mesh-mem` (entry points: `mesh-mem`, `mesh-mem-mcp`).
- CLI subcommands `save`, `search`, `delete` (logical / tombstone), `status`,
  and `gc` (physical delete: `--retention-days` sweep, `--force-id` emergency
  purge).
- FastMCP-based stdio MCP server exposing `save_observation`, `search_memory`,
  `delete_memory` (tombstone), and `get_memory_status`.
- Zenoh 1.9 transport with RocksDB storage backend; replication via
  `zenohd` mesh.
- E2E tests covering save/search, split-brain / reconnect sync, tombstone
  emission, and physical gc. FastMCP in-process and subprocess smoke tests.
- Documentation: quick start, MCP registration (Claude Code via
  `claude mcp add`, Claude Desktop, Gemini CLI, Codex CLI), systemd user
  unit, firewall (ufw / iptables) recipe, time-sync requirement, retention
  cron, and an emergency purge runbook.
- `LICENSE` (MIT, Copyright © 2026 h-wata) and
  `pyproject.toml` metadata (`license`, `license-files`, `authors`,
  `keywords`).

### Verified
- Local single-host topology (`config/zenohd_localhost.json5`): zenohd
  starts, RocksDB backend persists, CLI and MCP both round-trip.
- Two MCP clients on the same host (Claude Code + Codex CLI) share a
  single zenohd: an `observation` saved by one client is visible to
  `search_memory` from the other.

### Security
- Replaced hardcoded LAN IPs in `config/zenohd_home.json5` and
  `config/zenohd_office.json5` with placeholders (`192.168.3.x` /
  `192.168.3.y`) to align with README guidance and avoid leaking
  deployment-specific addresses.

### Known limitations
- No transport-level authentication or encryption; LAN-only.
- MCP transport is stdio only — web apps (`claude.ai`, `chatgpt.com`) are
  not supported in this release.
- Real two-host (Home ↔ Office) LAN deployment is documented but not yet
  field-tested.
- Search is a scan over up to `MAX_SEARCH=10000` observations; there is
  no full-text index yet.
- `gc --force-id` broadcast is best-effort; missed replicas catch up on
  their next `gc --retention-days` run.
