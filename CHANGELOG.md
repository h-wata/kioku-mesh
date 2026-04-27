# Changelog

All notable changes to this project will be documented in this file.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

mesh-mem is in `0.x`: APIs and on-disk storage schema may change between minor
versions without a migration path until `1.0.0`.

## [Unreleased]

### Added
- Observation schema extended with `memory_type`, `importance`, `subject`,
  `summary`, `source_files`, `supersedes` fields (Refs #9)
- MCP tool `save_observation` extended with `memory_type`, `importance`,
  `subject`, `summary`, `source_files`, `supersedes` (all optional, Refs #9)
- MCP tool `get_memory` added for retrieving full observation by ID (Refs #9)
- `search_memory` display now prefers `summary` over full content (Refs #9)
- CLI `mesh-mem gc --project <name>` filters retention sweep by project (#11)

### Fixed
- Unify default search `limit` to 50 across CLI/MCP/API (#1)

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
