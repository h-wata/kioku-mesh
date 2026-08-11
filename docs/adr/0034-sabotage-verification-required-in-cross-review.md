# ADR-0034: cross-review でサボタージュ検証を必須手順とする

- Status: Accepted
- Date: 2026-08-11
- Supersedes: なし
- Related: ADR-0031, PR #287, PR #292, PR #294, PR #295, PR #302

## Context

kioku-mesh の開発では、実装した worker 自身の verify ゲート（テストを実走して
acceptance criteria と照合する）を通し、GitHub Actions の CI（`lint-and-test`）も green に
なった状態で PR を出し、それを別 agent が cross-review する体制を取っている。

しかしこの 2 つのゲートは「テストが通ること」しか見ておらず、「そのテストが実際に何かを
押さえているか」を見ていない。2026-08-11 に cross-review した 7 本（#287, #292, #293, #294,
#296, #301, #302）のうち、次の 2 件は差分の読解では検出できず、production を意図的に壊す
検証でのみ検出された:

- **#292**: `tests/test_cli_init.py::test_init_install_systemd_writes_unit` は
  `shutil.which` に `/usr/bin/zenohd` を返させていたが、これは
  `_SYSTEMD_ZENOHD_FALLBACK` 定数と**同じ値**だった。production の PATH 探索を `None` に
  置き換えてもこのテストは green のままで、「有効な PATH ヒットを無視する」回帰を
  検出できない。
- **#294**: `test_since_until_excludes_out_of_range_query_matching_obs` は `since_iso` しか
  渡しておらず、非 cursor 経路の `until` 上限を一度も通っていなかった。production の
  `elif obs_dt > until_dt` フィルタを無効化しても `tests/test_store_errors.py` の 35 件は
  全部 green のままだった。

どちらも「テストが対象行を実行してはいるが、その振る舞いを assert していない」形であり、
カバレッジ上は緑になる。#292 に至っては fixture の値と定数の値がたまたま一致しているだけ
なので、diff を読むだけでは気づけない。

## Decision

**cross-review では「production 側を意図的に壊してテストが red になるか」を必須手順とする。**

- 契約ごとに 1 つの mutation を入れる。まとめて壊さない（どの層が拾ったのか分からなくなる
  ため）。
- 何を壊し、何件が red になったかを review YAML に記録する。red にならなかった場合、
  その契約は未検証として blocking finding にする。
- mutation は必ず revert し、**revert 後に green を再確認して** review YAML の cleanup 節に
  記録する。レビューで生成した cache / index / 一時展開も含めて worktree を clean に戻す。
- サボタージュが green のままだった場合、それ自体が「テストが有効でない」ことの証拠として
  扱う。逆に red になれば、そのテストが当該契約を押さえていることの確認になる。

## Consequences

- 良い点: 2026-08-11 に review した 7 本のうち 5 本（#287, #292, #294, #296, #302）で
  blocking を検出した。うち #292 と #294 はこの手法でしか検出できなかった。
- 良い点: 「テストが red になった」という具体的な証拠が review YAML に残るため、
  author 側が指摘を再現・確認できる。
- 悪い点: レビューコストが上がる。契約ごとに production を壊して pytest を回すため、
  1 本あたりの所要時間とトークン消費が増える。
- 悪い点: production を一時的に壊すため、revert 漏れが worktree を汚染するリスクがある。
  cleanup の記録を必須にすることで緩和しているが、リスクが消えるわけではない。
- 注意: 全件で blocking が出るわけではない。同日の 7 本のうち #293 は
  `approve_with_comments`（blocking 0 件）、#301 は `approve`（findings 空）だった。
  サボタージュ検証は欠陥を作り出すのではなく、有無を判定する手順である。

### 却下した選択肢

- **カバレッジ率で代替する。** 上記 2 件はいずれも当該行が実行されており、カバレッジ上は
  緑になる。行が実行されることと、その振る舞いが assert されていることは別の性質であり、
  カバレッジは後者を測れない。
- **レビュアの目視だけで判断する。** #292 は「fixture の返り値が定数と同値」という一致で
  あり、diff を読むだけでは気づけない。目視主体だと巡回数も増える（#287 は 3 巡、#291 は
  初回 blocking 9 件）。
- **author 自身にサボタージュさせて済ませる。** 自己採点になる。author は自分のテストが
  何を押さえている「つもり」かを知っているため、押さえていない側の mutation を思いつき
  にくい。実際 PR #295 では author のサボタージュ 4 件中 3 件が green のままで、層が互いを
  マスクしていたことを後から発見している。author 自身のサボタージュは有用だが、独立した
  第三者による検証の代わりにはならない。
