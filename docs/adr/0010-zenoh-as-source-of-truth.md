# ADR 0010: Zenoh storage を真実源、SQLite local index は派生キャッシュ

- **Status**: Accepted
- **Date**: 2026-05-18
- **Supersedes**: なし
- **Related**: ADR-0007 (sqlite-local-index-sidecar), ADR-0002 (existence-based tombstone), Issue #64, Issue #67, PR #50, PR #65

## Context

ADR-0007 で SQLite local index sidecar を read-path に導入し、Consequences で
「index と Zenoh の二重管理に整合性責任が生じる」と認識していた。ただし
**どちらが一次状態で、どちらが派生か** という関係性は ADR-0007 では明示されて
いなかった。Phase 4 で subscriber を張る運用は決めたが、片方向の reconcile
（add-only rebuild + PUT のみ吸う subscriber）しか書かれておらず、削除方向
の責務は宙に浮いていた。

実機 (Issue #64) で次の現象が観測された:

- `mesh-mem gc --by-pc-id ... --execute` を実行した peer の SQLite からは
  `execute_bulk_purge` (`store.py:1206-1228`) が `session.delete` と
  `idx.physical_delete` を併走させて消える。
- 同じ削除は他 peer の SQLite に伝わらず、bench 系プロジェクトだけで合計
  11.7 万件のゴースト行が `~/.local/share/mesh-mem/index.db` に滞留した。
- 切り分け結果、(a) subscriber が DELETE-kind sample の payload を JSON
  parse して空 payload を握り潰していた (PR #65 で修正)、(b) `rebuild_
  from_zenoh` が add-only で `existing - zenoh_set` を落とさない (Issue #67
  で議論中)、の二段の穴に起因していた。

並行して PR #50 で「Zenoh put が retryable に失敗したら `pending_puts.db`
にローカル queue し、後で replay する」フォールバックも導入していた。これは
「Zenoh に書き込めるまで保存は完了していない」という運用前提の自然な帰結
だったが、明文化されていなかった。

これらの整理を進める中で、オーナーから「基本的には Zenoh が正であるから、
SQLite はそれに追随すべき」という指針が明示された。本 ADR はその指針を
ADR-0007 の上に重ねて固定する。

## Decision

mesh-mem における状態の真実源 (source of truth) は **Zenoh storage 上の
`mem/obs/**` および `mem/tomb/**` 名前空間** とする。SQLite local index
sidecar は Zenoh state の派生キャッシュであり、独立した永続層ではない。

具体的な責務分担:

- **書き込み**: `put_observation` / `put_tombstone` は Zenoh put 成功を
  契約とする。Zenoh put が retryable に失敗した場合は `pending_puts.db`
  に queue し、SQLite index には書かない (`store.py:801-820`)。Zenoh put
  成功後にのみ sidecar を upsert する。
- **削除**: `session.delete` を実行した peer 自身は inline で `idx.
  physical_delete` を呼ぶ (`execute_bulk_purge`)。他 peer は subscriber
  の DELETE-kind 分岐 (PR #65) または rebuild 時の reconcile (#67 で
  議論中) を通じて反映する。
- **再構築 / 整合**: 起動時の `rebuild_from_zenoh` および subscriber
  の延長で、index は最終的に Zenoh の現状態と一致させる方向に倒す。
  index 単独で持っている state は「合法的な値」と扱わない。Zenoh が
  権威。
- **検索 read-path**: ADR-0007 の通り SQLite を 1st-hop で使う。これは
  ネットワーク往復削減のためのキャッシュ最適化であり、index と Zenoh が
  乖離したときは Zenoh に合わせる方が常に正しい。

本 ADR は ADR-0007 を supersede しない。ADR-0007 は index 導入の判断
そのものであり、本 ADR はその上に「index と Zenoh の階層関係」を追加で
固定する位置付け。

## Consequences

- **良い点**: 「index と Zenoh が乖離したらどちらに合わせるか」が ADR
  レベルで決まる。Issue #67 (rebuild の双方向化 / shadow-delete vs
  hard-delete) の議論は、この前提のもとで「Zenoh に存在しない obs は
  index 上もそう扱う」方向に倒す根拠ができる。
- **良い点**: pending_puts の再送 (PR #50) と subscriber の DELETE 分岐
  (PR #65) を「Zenoh への到達が write の契約」という共通原則のもとに
  位置づけられる。今後 transport-layer の他経路を足す際もこの契約を
  起点に設計できる。
- **悪い点**: 「Zenoh storage に存在しない」は「永久に消えた」と「一時的
  に見えない (peer 離脱 / storage 起動順序)」の両方を含む。reconcile を
  即時 hard-delete に振ると、transient な不在で index が誤って prune
  されるリスクが残る。この扱いは Issue #67 で個別設計する。
- **悪い点**: index が独立した state を保持しないため、Zenoh storage が
  永続化されていない構成 (memory peer のみ) では再起動で全消失する。
  運用上は永続 storage を 1 ノード以上立てることを前提にする。これは
  ADR-0006 (hub-and-spoke) と整合する。
- **トレードオフ**: read-path の latency 最適化のために sidecar を残す
  以上、書き込み / 削除 / reconcile すべての経路で「Zenoh が一次」を
  維持する責務が常に subscriber と rebuild に乗る。Phase 4 で subscriber
  を declare し損ねる起動シーケンスは事故の入口になりうる
  (ADR-0007 Consequences でも認識済み)。テストカバレッジで凍結する。
