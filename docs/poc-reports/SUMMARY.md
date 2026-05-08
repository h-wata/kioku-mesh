# PoC Reports — Summary

短い検証ノートを下に追記していく。詳細YAMLは `raw/` 配下に置く想定（必要時に追加）。

## Issue #33 — Office host CLI upgrade v0.1.0 → v0.2.3 (2026-05-09)

- 対象: Office host (192.168.128.12) `/home/gisen/.venv/mesh-mem`
- 手順: `pip install -e /home/gisen/work/mesh-mem`（main = v0.2.3）
- 検証:
  - `mesh-mem --version` → `mesh-mem 0.2.3`（v0.2.1+ 充足）
  - `mesh-mem get-memory <id>` → 既存共有 obs (`e4a3204c…`) を完全取得 OK
  - `mesh-mem gc --project <name> --retention-days 36500` → `0 件を物理削除しました`、`--project` フィルタ動作確認
  - `mesh-mem status` → `mesh-mem version: 0.2.3` 表示、件数は family `claude` で上限到達 (10000件)
- 観察: full-scan 中に `Observation.from_json: clamping unknown memory_type 'fact' to "note"` 警告が大量発生。v0.2.3 のクローズドenum検証 (commit 508c49a) と既存ストアのレガシー値 (`'fact'` 等) の不整合。clamping 自体は正しく動作 (フォールバック `note`)、別 Issue で集約警告化または DEBUG 降格を検討予定（#31 と同パターン）。
- zenohd: v1.9.0 のまま据え置き、再起動なし。
- データ移行: 不要（SQLite local index は初回 `--rebuild` 時に zenohd RocksDB から再構築）。
