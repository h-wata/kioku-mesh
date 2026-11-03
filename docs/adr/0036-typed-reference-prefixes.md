# ADR-0036: typed prefixes in `references` record provenance without new fields

- Status: Accepted
- Date: 2026-11-03
- Supersedes: none
- Related: ADR-0012, ADR-0026, ADR-0028, Issue #336, h-wata/squad#58

## Context

An observation records *what* was learned but not reliably *where it came
from*. `references` is a free-form `list[str]`; the only typed convention so
far is the messaging bridge's `msg_id:<id>` / `correlation_id:<id>`. Squad
(h-wata/squad#58) needs to list every memory saved under one task or project,
and to walk from a memory back to the task and to the memory it was derived
from. `recall_context(references=[...])` matches exact values only, so "all
memories under project P's tasks" cannot be expressed.

## Decision

1. **Fixed vocabulary of typed prefixes inside the existing `references`
   field.** No new payload field.

   | prefix | meaning | example |
   |---|---|---|
   | `obs:<observation_id>` | derived from that memory | `obs:568953ec...` (32 hex) |
   | `squad:task:<project>/<task_id>` | the Squad task it came from | `squad:task:kioku-mesh/TASK-012` |
   | `squad:report:<report_id>` | the report it rests on | `squad:report:<uuid>` |
   | `msg_id:` / `correlation_id:` | existing, unchanged | |

   The vocabulary is this table. Adding a prefix means updating this ADR.
   Strings without a known prefix stay free-form.

2. **`obs:` is not `supersedes`.** `supersedes` replaces: the old entry is
   hidden once the new one is live (ADR-0026). `obs:` derives: both stay live,
   and the source is reachable with `get_memory`.

3. **`recall_context(reference_prefix=...)` matches by prefix.** It is AND-ed
   with every other filter, including the exact `references` filter. It uses
   `substr(value, 1, len) = prefix` rather than `LIKE`, so `%` and `_` in a
   project or task name match literally.

4. **Validation is warn-only.** `save_observation` returns
   `MALFORMED_REFERENCE` when a value starts with a known prefix but does not
   match its shape (`obs:` not followed by 32 hex, `squad:task:` without
   `<project>/<task_id>`, empty `squad:report:`). The save is never rejected.

## Rejected alternatives

- **Dedicated `derived_from` / `task_id` / `evidence_refs` fields.** Older
  nodes would keep them in `_extras` (ADR-0012), so nothing breaks, but each
  field needs an index column, FTS, a filter, an MCP argument and a CLI
  argument. Prefix matching covers the known need; revisit when it does not.
- **`reference_prefix` on `search_memory` and CLI `search` as well.** Squad
  only needs `recall_context`, and `search_memory` has no `references` filter
  today either. Add it when a caller needs it.

## Consequences

- Payload shape is unchanged: no schema version, migration, mesh replication
  change or index rebuild. Mixed old/new nodes round-trip `references` as
  before, and visibility tiers are unaffected.
- The prefix filter scans `json_each(references)` per row, the same cost as
  the existing exact filter.
- Old free-form entries such as `SQUAD-123` are not rewritten and are not
  found by `squad:task:` prefixes.
