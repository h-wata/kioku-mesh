# mesh-mem

Cross-agent distributed memory over a mesh transport (currently Zenoh).

Multiple AI coding agents (Claude Code, Claude Desktop, Gemini CLI, Codex CLI, ChatGPT Desktop) share observations across PCs. Nodes form a mesh; the PoC transport is [Zenoh](https://zenoh.io/) 1.9 with the RocksDB storage backend for eventual-consistent persistence.

See [plan.md](./plan.md) for the full design.

## Status

PoC / design complete, implementation in progress. See `src/mesh_mem/`.

## Quick start (PoC)

```bash
# 1. two PCs (role names: `home` and `office`)
# 2. edit config/zenohd_*.json5 to replace 192.168.3.x / 192.168.3.y with real LAN IPs
# 3. on each PC:
export ZENOH_BACKEND_ROCKSDB_ROOT="$HOME/.local/share/mesh-mem"
mkdir -p "$ZENOH_BACKEND_ROCKSDB_ROOT"
zenohd -c config/zenohd_home.json5    # or zenohd_office.json5

# 4. install the Python package in each PC venv
python3 -m venv ~/.venv/mesh-mem
~/.venv/mesh-mem/bin/pip install -e '.[dev]'

# 5. exercise from the CLI
export MESH_MEM_AGENT_FAMILY=claude
export MESH_MEM_CLIENT_ID=claude-code
mesh-mem save "note content" --project demo --tags a,b
mesh-mem search "note"
mesh-mem status
```

For MCP integration, see the "MCP登録設定" section in `plan.md`.

## Requirements

- Python >= 3.10
- Zenoh 1.9.0 (`eclipse-zenoh` Python binding and `zenohd` + `zenoh-backend-rocksdb` router)
- `MESH_MEM_STATE_DIR` (default `~/.local/share/mesh-mem`) must be on a filesystem that supports POSIX hard links — ext4 / btrfs / xfs / tmpfs / NFSv3+ all qualify. FAT / exFAT and certain older SMB mounts do NOT, and `get_pc_id()` will fail on first run in that case.
