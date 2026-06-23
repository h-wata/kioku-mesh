# ADR 0022: Zenoh 上にエージェント間メッセージング（フロー）レイヤーを新設する

- **Status**: Accepted
- **Date**: 2026-06-23
- **Supersedes**: なし
- **Related**: [[0001-zenoh-rocksdb-transport]], [[0010-zenoh-as-source-of-truth]], [[0017-dual-hub-spoke-topology]], [[0019-visibility-tiered-replication]], [[0014-mtls-via-csr-private-ca]]

## Context

kioku-mesh はこれまで **長期記憶（ストック）** の共有に専念してきた。Zenoh + SQLite で observation を永続化・複製し（ADR-0001 / 0010 / 0017）、visibility tier で可視範囲を制御する（ADR-0019）。

2026 年前半の市況調査で2点が分かった:

1. **memory は主戦場**（Mem0 / Zep / Letta 等が乱立、専用ベンチも整備）だが、ほぼ**中央サーバまたは単一マシン前提**。
2. **multi-agent messaging も急拡大**したが（Claude Code Agent Teams の `SendMessage`、agmsg、Overstory）、いずれも**単一マシン内**の実装（worktree 間の WAL SQLite mail）で、**ホストをまたがない**。

つまり「**クロスホスト・local-first な分散メッセージング**」は空白象限であり、kioku-mesh は既に Zenoh pub/sub を持つため、この会話（フロー）レイヤーを足すコストが最も低い。

具体的な動機: **複数 pane で開いている Claude に宛先付きでメッセージを送り、人間のコピペ中継を排したい**。

## Decision

記憶（ストック）レイヤーと**並立する「メッセージング（フロー）レイヤー」を Zenoh pub/sub 上に新設する**。記憶層は置き換えない。

- **宛先モデル**: 3 形態を提供する。
  - **direct (1:1)**: agent / session ID 宛の直接送信。
  - **broadcast**: mesh / team / host スコープへの一斉送信（ADR-0019 の tier を流用）。
  - **topic / channel**: named channel を subscribe しているエージェントが受け取る pub/sub 純正形。
- **永続化**: 長期記憶とは分離した **TTL 付き短期 inbox spool** を既定とする。完全な揮発 pub/sub だけにはせず、agent が turn 中・未起動・一時切断中でも短時間は取りに行けるようにする。任意で `save_observation` 経由で記憶層へ昇格できるブリッジを用意し、**記憶とメッセージングの責務は分離**する。
- **配送と表面化の分離**: トランスポート（Zenoh subscriber）と delivery adapter を分ける。adapter は差し替え可能:
  - **tmux send-keys adapter（opt-in / multi-pane 向け）**: 各ホストの subscriber が受信メッセージを対象 pane に `tmux send-keys` で注入する。便利だが入力注入に近いため、明示的に有効化した pane / host に限定する。
  - **MCP poll tool（既定）**: エージェントが `check_messages` 等を呼んで取りに行く（既存 MCP 流儀に整合）。
  - **hook injection**: SessionStart 等のフックでコンテキストに注入。
- **キー空間**: 記憶用 namespace と分離した messaging 専用 prefix を切る（ADR-0019 のキー空間設計を踏襲）。
- **presence / 宛先レジストリ**: 宛先解決のため「どの agent がどの pane / host にいるか」を登録・公告する presence 機構を併設する。

### 初期実装スコープ

この ADR は「汎用チャット基盤」ではなく、**local-first agent coordination の制御面**
として進める。最初の価値は「今どの agent がどこにいて、そこへ安全に短い依頼や状況を
渡せる」ことであり、メッセージング機能全体を一度に作らない。

初期 MVP は次に絞る:

- **direct のみ**: `agent_id` / `session_id` 宛の 1:1 配送を先に実装する。
  `broadcast` と `topic / channel` は direct の運用上の不足が見えてから追加する。
- **MCP poll tool を正経路にする**: 受信 agent が `check_messages` / `ack_message`
  のような tool で取りに行く pull 型を最初の既定にする。これは既存 MCP の操作モデルと
  合い、turn 実行中の割り込みを避けやすい。
- **tmux send-keys は opt-in adapter**: tmux pane への入力注入は便利だが、実質的には
  リモート入力注入に近い。既定の配送経路にはせず、明示的に有効化した pane / host に
  限って使う。
- **短期 inbox spool を持つ**: 長期記憶とは分離したまま、TTL 付きの短期キューを持つ。
  完全な揮発 pub/sub だけだと、agent が turn 中・未起動・一時切断中のときに実用上の
  取りこぼしが多い。永続化が必要な内容だけ `save_observation` へ昇格する。
- **presence は最小 schema から始める**: `agent_id`, `session_id`, `host`,
  `last_seen`, `capabilities`, `delivery_adapters` 程度に留め、tmux pane ID などは
  adapter-specific metadata に閉じ込める。

この切り方により、kioku-mesh の既存価値である memory（ストック）を肥大化させず、
agent 間の coordination（フロー）だけを薄く追加できる。

## Consequences

- **良い点**:
  - 既存の memory 系・単一マシン messaging が踏み込めていない「クロスホスト分散メッセージング」象限を取れる。
  - Zenoh を再利用するため実装コストが小さい。
  - 記憶と分離した疎結合設計で、どちらも単独で進化できる。
  - MCP poll により turn 実行中の割り込みを避けつつ、必要な agent が短期 inbox から取りに行ける。
- **悪い点 / トレードオフ**:
  - presence / 宛先レジストリという新たな状態管理が必要になる。
  - tmux send-keys adapter は **tmux 上のエージェント限定**（GUI 不可）で、**turn 実行中の割り込み挙動の制御が難しい**（入力はキューされる前提で設計する）。
  - 短期 inbox spool の TTL、ack、重複配送、再送の扱いを設計する必要がある。
  - 配送順序保証はトランスポート層では持たないため、必要ならプロトコル層で担保する。
  - **セキュリティ**: 任意 pane への入力注入は実質リモート実行に近い。mesh / team スコープと mTLS（ADR-0014）を前提とし、信頼境界の外からは送れないようにする。
