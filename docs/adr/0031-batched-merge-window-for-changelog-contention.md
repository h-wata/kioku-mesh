# ADR-0031: CHANGELOG 競合を前提とした merge window 方式

- Status: Accepted
- Date: 2026-08-11
- Supersedes: なし
- Related: ADR-0028, ADR-0030, PR #291, PR #292, PR #294

## Context

2026-08-11 時点で open PR が 11 本（#287, #291, #292, #293, #294, #295, #296,
#298, #300, #301, #302）あり、うち 10 本が `CHANGELOG.md` の `[Unreleased]` 節に
追記している（追記しないのは docs-only の #296 のみ）。

各 PR の diff を実測したところ、複数の PR が **同一の既存段落の直後** に新規箇条書きを
挿入していた。挿入位置（anchor）でクラスタ化すると次のようになる:

- クラスタ A（#278 の project alias エントリの直後）: #291, #293, #287, #295, #300 の 5 本
- クラスタ B（`get_memory_status` のスキップ件数エントリの直後）: #292, #298 の 2 本

同一クラスタ内では add/add 衝突になるため、3-way merge で自動解決できない。1 本を
merge した時点で、同クラスタの残り全 PR が `mergeable=CONFLICTING` に落ちる。

これは仮定ではなく、実際に起きた。PR #291 の cross-review（`TASK-291-rereview`）は、
前回 blocking B1〜B4 の**意味上の不具合をすべて resolved と判定した上で**、新規 blocking
N1「現在の origin/main と `CHANGELOG.md` が競合し、PR が merge 不能」を理由に
`request_changes` を返している（head `58bf52a`、base `c514d22`、
`mergeable=CONFLICTING` / `mergeStateStatus=DIRTY`、CI は SUCCESS）。
`mcp_server.py` と `tests/test_mcp_server.py` は仮想 merge で自動統合できており、
**衝突していたのは `CHANGELOG.md` 1 ファイルだけ**だった。

さらに、rebase すると head SHA が変わる。cross-review は `review_head_sha` を記録して
レビュー結果をその SHA に紐づけているため、rebase のたびにレビュー結果が失効し、
再レビューが必要になる。PR #291 はこの経路で実際に rereview に回された。

つまり「approve が出た PR から順に個別 merge する」運用を続けると、内容が完成している
PR が、他 PR の merge に起因する CHANGELOG 衝突だけで差し戻され、rebase → CI 再実行 →
再レビューのサイクルが連鎖する。

## Decision

approve のたびに個別に merge しない。**レビュー列が捌けた時点で merge window を開き、
1 名の担当が「rebase → CI 確認 → merge」を PR ごとに順に実行する。**

運用条件:

1. merge window に入る前に、対象 PR の `mergeable` / `mergeStateStatus` /
   `reviewDecision` / CI 結果 / head SHA を全件集計し、クラスタ構成と衝突の連鎖範囲を
   洗い出す。
2. 同一クラスタ内は 1 本ずつ直列に merge し、**各 merge の直後に後続 PR を rebase する**
   手順を明示的に踏む。クラスタをまたぐ PR 同士は互いに影響しないため、並べ替えても
   衝突数は変わらない。
3. stacked PR（base が親 PR の head branch であるもの。本日時点では
   #298 → #301 → #302）は親から順に merge する。親 merge 時に GitHub が子の base を
   自動で `main` に付け替えるので、付け替わったことを毎回確認する。
4. **人間 approve が無い PR は merge しない。** merge window は衝突を減らすための
   運用であって、レビューゲートを緩めるものではない。

## Consequences

- 良い点: 「内容は完成しているのに CHANGELOG 衝突だけで差し戻る」という、レビュー内容と
  無関係な理由での request_changes が減る。rebase 起因の head SHA 変更による再レビューも
  減る。
- 良い点: merge の順序と rebase の必要箇所が事前に確定するため、担当が 1 名でも手順を
  機械的に実行できる。
- 悪い点: merge が window までホールドされるので、完成済みの変更が `main` に入るまでの
  待ち時間が伸びる。本日の PR #291 は、merge 可能な状態のまま window までホールドされた。
- 悪い点: window 中に新規 PR が open されると、その PR は次の window まで待つか、window を
  中断して取り込むかの判断が要る。
- この ADR は衝突の**運用上の回避**を決めたものであり、衝突の**構造的な原因**（同一 anchor
  への並行追記）は解消していない。fragment ファイル方式（towncrier 等）への移行は将来の
  選択肢として残る。

### 却下した選択肢

- **個別 merge を続ける。** クラスタ A の 5 本では最悪 4 回の追加 rebase と CI 再実行が
  発生する。rebase のたびに head SHA が変わって cross-review の結果が失効するため、
  レビューコストが merge 本数に対して線形以上に増える。PR #291 がこの経路で
  rereview に回された実例がある。
- **`.gitattributes` の union merge driver で CHANGELOG を自動結合する。** union は
  行を機械的に連結するだけで、`### Added` / `### Fixed` / `### Changed` の節構成を
  保てない。実際 #294 は「同一 `[Unreleased]` 内に 2 つ目の `### Added` 見出しを作った」
  という指摘を cross-review で受けており（non-blocking）、自動 union はこの種の構造崩れを
  検出せずに通してしまう。衝突が消える代わりに、壊れた CHANGELOG が静かに main に入る。
- **CHANGELOG への追記を PR から外し、リリース時にまとめて書く。** 変更の理由を書ける
  唯一のタイミングは、その変更を実装している最中である。後追いで書くと why が失われ、
  「raw な記録を source of truth とし、派生ビューは再構築可能にする」という ADR-0028 の
  方針とも整合しない。
