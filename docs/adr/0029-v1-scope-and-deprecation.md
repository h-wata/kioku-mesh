# ADR-0029: v1.0 scope and compatibility deprecation policy

- Status: Proposed（semver 条項のみ ADR-0030 に Superseded）
- Date: 2026-07-02
- Supersedes: なし
- Related: ADR-0019, ADR-0024, ADR-0026, ADR-0028, ADR-0030

## Context

v0.8.0 completed ADR-0019 Phase D: default writes now target the visibility-tiered
namespace, legacy writes are only available via `KIOKU_MESH_LEGACY_WRITE_EMERGENCY=on`,
and legacy reads are only available via `KIOKU_MESH_LEGACY_READ_FALLBACK=on`.
Both flags were documented as v0.8.x-only and removed in v1.0.

ADR-0024 introduced compatibility shims for the old `mesh_mem` import path and
`MESH_MEM_*` environment variable prefix; current implementation/docs
(`src/mesh_mem/__init__.py`, `src/kioku_mesh/core/_env_compat.py`, README, CHANGELOG)
mark them for removal in v1.0.0. Meanwhile ADR-0028 defines
the long-term memory model: raw observations are the source of truth, derived views
are rebuildable, and stale decision/config entries should be superseded rather than
deleted.

Before declaring v1.0, kioku-mesh needs a narrow scope definition so v1.0 means
"stable contract begins" rather than "all backlog is done".

## Decision

### 1. v1.0 scope

v1.0 includes:

- removal of `KIOKU_MESH_LEGACY_WRITE_EMERGENCY`;
- removal of `KIOKU_MESH_LEGACY_READ_FALLBACK`;
- removal of `mesh_mem` import compatibility and old `MESH_MEM_*` env fallback,
  unless explicitly deferred by user decision before implementation;
- CHANGELOG/README update stating that from v1.0 onward public CLI/MCP/Python API
  and on-disk schema follow semantic versioning: breaking changes require semver-major
  or an explicit migration path;
- pre-release health gates: `doctor --check-legacy-namespace` reports zero legacy
  obs/tomb, `doctor` reports zero conflicting_latest groups after manual cleanup,
  CI tests and `ruff check` pass;
- ADR-0028 layering invariant hardening tracked by issue #249.

v1.0 does not include:

- messaging backlog issues #191/#192/#193/#201/#202;
- parked/upstream issues #104/#106;
- macOS verification #87;
- `init --install-systemd` bin-dir fallback #223;
- broad search/supersede test improvements #230/#236 unless maintainers choose to
  pull them in opportunistically;
- automatic `doctor --fix` for conflicting_latest;
- graph, embedding, summary, or automatic consolidation features.

### 2. Deprecation operation

A separate v0.9 release is not required. If another unrelated 0.x feature release
occurs, it may carry the stronger warnings, but the normal path is:

1. strengthen v0.8.x warnings and docs so both legacy flags explicitly say they are
   removed in v1.0.0 and point to doctor/migrate commands;
2. remove the legacy write emergency path in a focused v1.0 PR;
3. remove the legacy read fallback path in a focused v1.0 PR;
4. remove old package/env compatibility shims and prepare the release.

Warning activation remains once per process. Actual legacy-hit warning remains once
per process. Per-record warnings are forbidden because rebuild/search can touch many
legacy keys.

### 3. conflicting_latest cleanup

`conflicting_latest` is resolved manually before v1.0. The default remediation is a
new current decision/config observation with `supersedes=[old_ids]`. Delete is reserved
for entries that should not exist at all, such as secrets, test junk, or invalid data.

v1.0 does not add automatic `doctor --fix`. A future v1.x command may print suggested
supersede commands, but it must not silently choose the current decision from timestamp
alone.

## Consequences

- v1.0 has a small, reviewable scope and starts the semver stability contract from a
  clean compatibility state.
- Users who still depend on legacy namespace data must migrate before v1.0; the
  documented path is `kioku-mesh doctor --check-legacy-namespace` followed by
  `kioku-mesh migrate-visibility --from legacy --to <user|team|mesh>`.
- Manual conflicting_latest cleanup preserves historical truth and records the WHY of
  the current decision, consistent with ADR-0028.
- Deferring messaging, platform, and future derived-view features prevents v1.0 scope
  creep while keeping them available for v1.x.

## Implementation split

1. Memo PR: add this ADR and update any release-planning notes.
2. Warning/docs PR: stronger v0.8.x deprecation warning text and upgrade notes.
3. Legacy write removal PR.
4. Legacy read fallback removal PR.
5. Compatibility shim removal and release-prep PR.
