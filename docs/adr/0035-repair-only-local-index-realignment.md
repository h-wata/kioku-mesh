# ADR-0035: repair-only local index realignment keeps absence non-destructive

- Status: Proposed
- Date: 2026-08-18
- Supersedes: none
- Related: ADR-0028, TASK-441, TASK-450, TASK-451, PR #324

## Context

The per-host SQLite index is a derived view of Zenoh observations. PR #324
fixed the subscriber session lifecycle and the startup scan/subscribe gap, but
a host can still be absent while updates are published. Existing
`rebuild_from_zenoh()` both backfills records it sees and shadows a local live
row it does not see. That is appropriate only for an authoritative sweep whose
source coverage is known; it is unsafe as a periodic recovery mechanism because
a timeout, partial storage response, or local-only record is observationally
indistinguishable from a confirmed absence.

ADR-0028 establishes the raw observation source of truth and explains shadow as
a reconciliation state. It does not define the safety contract for automated,
periodic index repair.

## Decision

Automated local-index realignment is **repair-only**:

- It applies positive source facts only: observation upsert and tombstone
  application.
- It never shadows or physically deletes an index row because that row was
  absent from a repair scan.
- It collects and validates a scan before one transactional apply. Any scan or
  apply failure records observable failure state and does not advance its
  successful-completion state.
- It is enabled only for an MCP process after that process has opened an index.
  Tombstone repair runs every 15 minutes; a full observation+tombstone repair
  runs every 6 hours. Normal one-shot CLI commands do not implicitly repair.
- A stale-row shadow sweep remains a separate, explicit operator action until
  source coverage and local-only semantics have their own accepted design.

The persisted alignment timestamps describe completed repair attempts, not a
proof that every authoritative source responded. They are diagnostic and
scheduling metadata, not a data cursor. An explicit CLI repair command may be
used by an opt-in external scheduler on CLI-only hosts.

## Consequences

- Good: a periodic worker can close missed-update windows without gaining the
  ability to hide local-only data.
- Good: failures become visible after process restart through local metadata and
  doctor output.
- Good: the destructive stale-cleanup problem remains independently reviewable.
- Trade-off: without a source-side time selector, full repair still reads all
  matching source records; cadence is bounded rather than cursor-optimized.
- Trade-off: stale rows removed upstream are not automatically shadowed by this
  worker. They require the separate explicit sweep.
- Dependency boundary: orphan tombstone durability belongs to TASK-451. Repair
  must consume its apply API when it lands, rather than duplicating an orphan
  representation.

## Rejected alternatives

- Reuse `rebuild_from_zenoh()` on a timer: absent source rows would be shadowed
  after a partial scan.
- Filter locally by `last_aligned_at`: current selectors cannot apply that
  timestamp remotely, and filtering would lose old missed records.
- Enable repair for every CLI request: violates the existing latency contract
  for one-shot CLI commands.
