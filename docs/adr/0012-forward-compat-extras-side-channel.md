# ADR 0012: Forward-compat via _extras side-channel for unknown Observation fields

- **Status**: Accepted
- **Date**: 2026-05-21
- **Supersedes**: なし
- **Related**: [[0007-sqlite-local-index-sidecar]], [[0010-zenoh-as-source-of-truth]]

## Context

Rolling upgrade 中の silent data loss が観測された (Issue #75)。

1. 新リリースが `Observation` に field を追加 (例: PR #74 の `references`)
2. mesh 内に旧リリースの peer が残存
3. 旧 peer の subscriber が PUT 受信 → `Observation.from_json` →
   `_from_dict_compat` で `cls(**{k: v for k, v in data.items() if k in known})`
   により **未知 field を drop**
4. その Observation を `upsert` で SQLite に書き戻し、`payload_json` を
   新 field 抜きで上書き

結果として「数 ms だけ新 field が見え、その後消える」現象になり、外から見ると
新 field が永続化されていないように見える。

Zenoh-rocksdb (ADR-0010) には元 payload が残っているが、`search_memory` が
読むのは SQLite local index (ADR-0007) なのでユーザー視点ではデータロス。

複数ホストの rolling upgrade（home が v0.3、office が v0.2.6 等）では
ゾンビプロセス掃除では根治しない。

## Decision

`Observation` に dataclass field ではない `_extras: dict[str, Any]` を持たせ、
未知 field を side channel で保持する:

- `__post_init__` で `self._extras = {}` 初期化（`fields()` の対象外、公開
  スキーマ不変）
- `_from_dict_compat`: 既知 field を `cls(**known_data)` で構築後、残った
  unknown key を `inst._extras` に退避
- `to_json`: `{**self._extras, **asdict(self)}` でマージし、**dataclass field
  側を優先**
- 永続化境界は **`to_json` / `from_json` ペアに閉じる** 不変条件として扱う

### 採用しなかった代替案

1. **Schema 不一致を検知したら upsert skip**
   - 旧 peer の SQLite が古いまま残り、Phase 3 read acceleration (ADR-0007) が
     効かなくなる
2. **Payload に schema version header + version-aware subscriber**
   - 実装量が多く、結局全旧リリースが「新 payload に触らない」ロジックを
     内蔵する必要があり forward-compat にならない。`_extras` の方が小さい
3. **SQLite に `payload_json` を保存せず、毎回 Zenoh から取る**
   - ADR-0007 のレイテンシ要件に反する

## Consequences

- **良い点**:
  - 最小修正で rolling upgrade 中の silent data loss を防ぐ
  - 公開スキーマ (`asdict()` の出力) は不変、既存 API への影響なし
  - SQLite を source of truth から外さず ADR-0010 の精神を守る
  - 衝突時 dataclass field 優先で、将来 `_extras` の key が field 昇格しても
    意味論が安定（旧 payload の偶然の値に引きずられない）
- **悪い点 / トレードオフ**:
  - `dataclasses.replace()` 等の field-only clone API を横活させると `_extras`
    を落とすリスク。永続化境界を `to_json` / `from_json` に閉じる不変条件を
    遵守し続ける必要がある（コードコメントで明示）
  - 守備範囲は **unknown key の round-trip 保持** のみ。既存 field の enum/type
    drift (例: `memory_type` の新値) は対象外で、`from_json` の clamping logic
    で別途扱う
