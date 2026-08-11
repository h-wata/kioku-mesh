# Rolling out the messaging ack-state fix (N4)

An acknowledgement row with no matching message used to be read as a real
acknowledgement. Because `is_acked` is an exact-pair point lookup, such a row
suppressed a *live* message carrying the same `(msg_id, recipient_session_id)`
— silently, with no error and no warning. The fix ships in three units:

| Unit | What changed |
|---|---|
| 1 | Schema v2+: `pending_acks`, `message_tombstones`, `legacy_unknown_acks`, `recovery_audit`; a one-time transactional migration; read-only inventory and exact-pair, backup-gated recovery. |
| 2 | Ingress classification: `is_acked` believes only acks backed by a message, expiry purge tombstones the ids it retires, and `check_messages` reports every withheld arrival instead of dropping it. |
| 3 | This document, and `orphan-acks status` — the per-node completion check. |

The migration is additive and runs automatically the first time a current
`kioku-mesh` opens the inbox database. This document is about doing that
deliberately across a fleet, and about knowing when it is finished.

## What the database cannot tell you

An ack with no message is **ambiguous**. It may be residue from the old purge
bug, or it may be a legitimate acknowledgement that arrived before its message
— distributed delivery is not end-to-end FIFO. No stored column separates
those, and **age does not either**: no upper bound on delivery or replication
delay is specified anywhere, so "old" is not "stale".

Therefore the migration never deletes such a row. It moves it to
`legacy_unknown_acks`, where nothing reads it as an acknowledgement, and leaves
the judgement to an operator, one exact pair at a time. There is deliberately
no bulk delete and no age-based cleanup, and none should be added.

## Procedure

Run steps 2–5 on every node that writes to an inbox database.

### 1. Inventory the fleet

List the nodes, their `kioku-mesh` version, their inbox database path
(`<state dir>/messaging/inbox.db`), and every process that writes to it. A
writer that predates unit 1 will keep creating bare acks after the migration,
which is the one failure this rollout has to prevent.

### 2. Quiesce writers and take a backup

Stop the writers on the node. Then take a backup with SQLite's backup API
rather than a file copy — a byte copy of a database mid-write is not a
database:

```bash
sqlite3 "$STATE_DIR/messaging/inbox.db" \
  ".backup '/var/backups/inbox.$(date +%Y%m%dT%H%M%S).db'"
sqlite3 /var/backups/inbox.<timestamp>.db 'PRAGMA integrity_check;'
```

Record the backup's `integrity_check` result, size, and row counts. (Step 6's
`recover` takes its own fresh backup for each recovery; this one is the
pre-migration copy.)

### 3. Record the pre-migration inventory

```bash
kioku-mesh messaging orphan-acks list --format json > pre-migration.json
```

This is read-only: it opens the database in SQLite's read-only mode, so it
cannot change the file's contents or mtime. Safe to run against a live
deployment.

### 4. Migrate

Open the database once with a current `kioku-mesh` (any command that touches
messaging will do). The migration is a single transaction and idempotent: an
interrupted run leaves the original `acks` table exactly as it was, and a
second run is a no-op. Acks with a message stay authoritative; the rest move
into `legacy_unknown_acks` losslessly, with their original `acked_at`.

### 5. Check completion on the node

```bash
kioku-mesh messaging orphan-acks status
# exit 0 = complete on this node, 1 = something still blocks it, 2 = usage error
```

Three things block completion, and each means something different:

- **Schema below the current version.** The migration has not run here.
- **An ack with no message still in `acks`, outside the quarantine.** A writer
  that predates the fix is still writing to this database. Find it.
- **A pair quarantined *after* the migration pass** (`provenance` other than
  `migration`). Same cause, caught later: an old writer created a bare ack
  post-cutover.

**Unresolved quarantined rows do not block.** They are the pre-existing
ambiguity the design refuses to guess away, and leaving them unresolved
forever is a legitimate choice. Blocking on them would create pressure to
clear the quarantine, which is the reflex that caused this bug's original
"just delete the residue" proposals.

The fleet is done when **every** node exits 0 and reports the same
`writer_version`. `status` reads one database and cannot see other nodes; it
does not pretend otherwise.

### 6. Resolve individual pairs, if you choose to

For a pair you have external evidence about — delivery logs, the operator who
sent it, a matching live message:

```bash
kioku-mesh messaging orphan-acks recover \
  --msg-id <id> --session-id <session> --action release|promote \
  --backup /var/backups/inbox.<timestamp>.db --execute
```

- `release` — this is not an acknowledgement; the message may be presented
  again.
- `promote` — it really was an acknowledgement observed before its message.
  Requires the message to exist, re-checked under the write lock.

Both refuse wildcards, ranges, and `all`; both require a fresh backup path
that does not already exist; and both write a before image to
`recovery_audit`. Without `--execute` the command is a dry run.

## Verifying the behaviour end to end

After cutover, these probes should hold on a node:

| Probe | Expected |
|---|---|
| A live message on a quarantined pair | withheld, but reported as `legacy_ack_conflict` with its payload — never `count=0` with empty diagnostics |
| Re-put of a purged `msg_id`, envelope unchanged | reported as `duplicate_retired` |
| Re-put of a purged `msg_id` with a different envelope | reported as `protocol_violation` |
| A new `msg_id` | delivered normally |
| An ack observed before its message | held in `pending_acks`, promoted when the message arrives |

The last row is the reason `pending_acks` exists: an early ack is legitimate
and must not be confused with residue.

## Rollback

The schema is additive, so a binary rollback keeps the data and allows a later
re-upgrade. **Do not drop `message_tombstones`, `pending_acks` or
`legacy_unknown_acks`** — a garbage-collected tombstone stops the receiver from
rejecting reuse of a retired id, which is the enforcement unit 2 depends on.

If the migration transaction has not committed, it rolls back on its own. After
commit, use `recovery_audit` to reverse individual actions, or restore the
backup taken in step 2 — but back up the current database under a new name
first, since a restore discards every legitimate write made since the backup.
