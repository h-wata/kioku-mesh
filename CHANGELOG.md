# Changelog

All notable changes to this project will be documented in this file.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

mesh-mem is in `0.x`: APIs and on-disk storage schema may change between minor
versions without a migration path until `1.0.0`.

## [Unreleased]

### Added

- **GitHub Actions CI** (`.github/workflows/ci.yml`) running pre-commit
  and `pytest tests/` on `ubuntu-24.04` with Python 3.12, triggered on
  every PR and on push to `main`. (#22)

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
