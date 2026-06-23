# ADR 0024: Python パッケージを mesh_mem から kioku_mesh にリネームする

- **Status**: Accepted
- **Date**: 2026-06-23
- **Supersedes**: なし
- **Related**: [[0009-mcp-server-instructions-protocol]], [[0023-single-repo-core-memory-messaging-layering]], [[0004-identity-env-and-persistent-file]]

## Context

プロダクト名は **kioku-mesh** だが、Python パッケージは **`src/mesh_mem/`**（旧名 mesh-mem 由来）のまま残っている。この命名の二重性が実害を生んでいる:

- MCP tool の docstring に "shared mesh memory" のような旧名表記が残り、**エージェントがプロダクト名を "mesh memory" と取り違える**事象が実際に発生した。
- import パス（`mesh_mem.*`）、エントリポイント、MCP 登録名・コマンド名、設定キー／環境変数 prefix、skills（`mesh-mem-worklog` 等）に旧名が散在し、新規参加者・エージェント双方の混乱要因になる。

直近の docstring 表記統一（PR #183）は**対症療法**であり、根治はパッケージそのもののリネーム。ADR-0023 で `core / memory / messaging` への層化を決めたタイミングは、パッケージ構造に手を入れる好機でもある。

## Decision

**`src/mesh_mem/` を `src/kioku_mesh/` にリネームする。**

移行計画（実装 Issue で詳細化する）:

- **import パス**: `mesh_mem.*` → `kioku_mesh.*` を全面置換。
- **パッケージ設定**: `pyproject.toml` の package 指定・`console_scripts` / エントリポイントを更新。
- **後方互換 import shim**: 移行期間中は `mesh_mem` を残し `kioku_mesh` を re-export して `DeprecationWarning` を出す（既存ユーザーの MCP 登録・スクリプトを即座に壊さないため）。撤去時期は別途。
- **MCP 登録名 / コマンド名**: 既存の MCP server 登録（コマンド名）が変わる場合は後方互換の要否を判断し、変更時はユーザーに告知。
- **設定・環境変数**: `MESH_MEM_*` 等の prefix があれば新 prefix への移行と互換読み込みを設計。
- **バージョニング**: 破壊的変更を含むため、次の節目で minor / major を bump（現 v0.5.0、SemVer 上 1.0 前なので minor bump 可だが CHANGELOG で明示告知）。
- **スコープ外**: ドキュメント・skills（`mesh-mem-worklog` 等）の参照更新は本リネームの直接スコープに含めず、追従作業として別途扱う。

## Consequences

- **良い点**:
  - 命名がプロダクト名 kioku-mesh に単一化され、取り違えの根本原因が消える。
  - docstring の対症療法（PR #183）的な表記合わせが将来不要になる。
  - ADR-0023 の `core / memory / messaging` 層化と同時に整理でき、新パッケージ構造を最初から正しい名前で始められる。
- **悪い点 / トレードオフ**:
  - import を持つ全ファイル・設定・MCP 登録・既存ユーザー環境に波及する大きな差分で、レビューが重い。
  - 後方互換 shim を置かないと既存の MCP 登録やスクリプトが壊れる。shim 維持・撤去のコストが発生する。
  - 公開 PyPI 配布が始まる前に実施するほど安い。タイミングを誤ると外部利用者の移行コストが増える。
