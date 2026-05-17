#!/usr/bin/env bash
set -eu

PROJECT=$(basename "$PWD")
SINCE=$(
  python3 - 2>/dev/null <<'PY'
from datetime import datetime
from datetime import timedelta
from datetime import timezone

since = datetime.now(timezone.utc) - timedelta(days=7)
print(since.strftime('%Y-%m-%dT%H:%M:%S.%fZ'))
PY
)

OUTPUT=$(
  mesh-mem search \
    --project "$PROJECT" \
    --since "$SINCE" \
    --limit 10 \
    --format markdown 2>/dev/null
)

if [ -z "$OUTPUT" ]; then
  exit 0
fi

printf '## Recent mesh-mem context (project: %s, last 7d)\n%s\n' "$PROJECT" "$OUTPUT"
