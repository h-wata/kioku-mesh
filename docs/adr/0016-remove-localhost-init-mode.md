# ADR 0016: `init --mode localhost` を廃止し、`local` / `mesh start` に集約

- **Status**: Accepted
- **Date**: 2026-06-01
- **Supersedes**: なし
- **Related**: [[0013-drop-tier-1-from-public-architecture]], [[0006-hub-and-spoke-mesh-topology]]

## Context

`kioku-mesh init` には 4 つの `--mode` があり、`localhost` がデフォルトだった：

| Mode | 持続性 | 必要なもの | 用途 |
|---|---|---|---|
| `localhost` (default) | **ephemeral** (zenohd 再起動でデータ消失) | zenohd binary | "does it run?" smoke test |
| `local` | persistent (SQLite) | なし | single-host 永続 |
| `hub` | persistent (RocksDB) | zenohd | mesh 中央 peer |
| `spoke` | persistent (RocksDB) | zenohd | mesh 末端 peer |

設計上の意図は「zenohd binary を入れた直後に wire-up を確認する最短コマンド」だった。実運用してみると 3 つの噛み合わせの悪さが見えた：

1. **デフォルトが最悪選択肢に着地する**。README §Modes は "`local` is the easiest starting point" と書いているのに、引数なし `kioku-mesh init` は `localhost` (ephemeral + zenohd 必須) を生成する。新規ユーザーの最初の体験で、ドキュメントとデフォルト挙動が矛盾する。
2. **`local` モードの上位互換が常に存在する**。単一ホスト用途で `localhost` を選ぶ合理性が無い (どうせ再起動で消えるならディスクに書く意味がない、書くなら `local` の方が安全)。`localhost` の正味のユースケースは「zenohd binary が動くかを 1 回確認する」だけ。
3. **その 1 回確認も `mesh start` でカバー済み**。ADR 0013 で残した `kioku-mesh mesh start` は in-process zenoh router を起動するので、zenohd binary すら無くても Zenoh が動くかをスモークテストできる。`localhost` モードの存在意義はここでさらに侵食される。

検討した選択肢：

- **A. ハード削除** (採用): `_INIT_MODES` から外し、デフォルトを `local` に変更、CHANGELOG に Breaking を記載
- **B. ソフト deprecate**: `localhost` を残しつつ help に deprecation 警告、デフォルトのみ `local` に変更
- **C. 据え置き**: 文書だけ書き換えてデフォルトの「不一致」は許容

B は ABI を保ったまま誘導できるメリットがあるが、0.x のうちは破壊的変更を出しても痛みが小さく、選択肢が 4 → 3 に減ること自体がドキュメント/help の読みやすさを直接改善する。C は問題の本丸 (デフォルトが矛盾している) を放置するため除外。

## Decision

`kioku-mesh init --mode localhost` を削除する。`--mode` の選択肢は `{local, hub, spoke}` の 3 つに減らし、デフォルトは `local` に変更する。`config/zenohd_localhost.json5` テンプレートも削除する。

スモークテスト用途は `kioku-mesh mesh start` (ephemeral Zenoh、zenohd binary 不要) に集約する。

## Consequences

- **良い点**:
  - デフォルトが README の "easiest starting point" 記述と一致するようになる
  - `init` の選択肢が 4 → 3 に減り、help / docs の説明面積が削減される
  - 「ephemeral か persistent か」を選ぶ軸が `mesh start` vs `init` の 2 コマンドに分かれ、両者の役割が読み取りやすくなる
  - `localhost` を選ぶ動機が消えるため、ユーザーが誤って ephemeral mode に着地するパスが無くなる
- **悪い点**:
  - Breaking change: `--mode localhost` 指定スクリプトは `--mode local` または `mesh start` に書き換える必要がある。0.x なので吸収するが、リリースノートで明示する責任は残る
  - 過去に `init --mode localhost` で生成された `~/.config/kioku-mesh/zenohd.json5` は引き続き動く (zenohd は config だけで動作するため) が、後から `kioku-mesh init` で再生成しようとすると別のテンプレートに置き換わる挙動になる
  - 「zenohd binary だけで完結する最小確認」のラッパが消えるため、本当に zenohd だけを検証したいケースでは手書きの json5 を書く必要が出る (実用上ほぼ存在しないユースケースだが、ゼロではない)
