#!/usr/bin/env bash
# DR 24h test writer: 1 obs/min × 1440 = 24h
set -euo pipefail

LOG_FILE="${DR_LOG:-/tmp/dr_writer.log}"
TOTAL="${DR_TOTAL:-1440}"
INTERVAL="${DR_INTERVAL:-60}"
MESH_MEM="${MESH_MEM_BIN:-/home/gisen/.local/bin/mesh-mem}"

export MESH_MEM_AGENT_FAMILY="${MESH_MEM_AGENT_FAMILY:-dr-test}"
export MESH_MEM_CLIENT_ID="${MESH_MEM_CLIENT_ID:-dr-writer}"

echo "[$(date -Iseconds)] DR writer start: total=$TOTAL interval=${INTERVAL}s" >> "$LOG_FILE"
echo "[$(date -Iseconds)] agent=$MESH_MEM_AGENT_FAMILY/$MESH_MEM_CLIENT_ID" >> "$LOG_FILE"

for i in $(seq 1 "$TOTAL"); do
  TIMESTAMP=$(date -Iseconds)
  if "$MESH_MEM" save "dr-test obs $i $TIMESTAMP" \
      --project dr-test --tags dr-test >> "$LOG_FILE" 2>&1; then
    echo "[$TIMESTAMP] obs $i/$TOTAL saved" >> "$LOG_FILE"
  else
    echo "[$TIMESTAMP] obs $i/$TOTAL FAILED (continuing)" >> "$LOG_FILE"
  fi
  sleep "$INTERVAL"
done

echo "[$(date -Iseconds)] writer completed: $TOTAL obs" >> "$LOG_FILE"
