# recall_context の予算と採用理由 (#337)

`recall_context` に、出力の予算 (`token_budget`) と、採用・除外の理由 (`explain`) を指定できる。
タスクに必要な記憶を予算内で選び、なぜ選んだか・なぜ落としたかを確かめるために使う。
新しいツールは作らない。`compile_context(query, task, token_budget)` のような使い方は
`recall_context(query=..., reference_prefix=<task>, token_budget=..., explain=True)` で表せる。

## 引数

| 引数 | 既定 | 意味 |
|---|---|---|
| `token_budget` | `0` | 出力の予算 (token)。0 なら従来どおり `RECALL_OUTPUT_MAX_BYTES` (20,000 バイト)。指定すると `token_budget × 3` バイトを上限にする (token ≈ UTF-8 バイト数 × 4/3 の逆算)。既定の上限は超えない |
| `explain` | `False` | True なら、各件に `why:` の 1 行を付け、末尾に `dropped:` の 1 行を付ける |

どちらも省略すれば、出力は従来と 1 バイトも変わらない。

## explain の出力

```
id: 0123...
state: live
why: matched=[watchd]; importance=4; rank=1/8
...
dropped: budget=7, superseded=0, expired=0
```

- `matched`: query の語のうち、subject / summary / content / tags / references / source_files に
  (大文字小文字を区別せず) 含まれていたもの。FTS の分かち書きを再現したものではない
- `rank`: 検索の順位 (importance → bm25 → 新しさ, ADR-0027) と、検索で返った件数
- `dropped`
  - `budget`: 予算に収まらず末尾から落とした件数
  - `superseded` / `expired`: 既定で隠れる置き換え済み・期限切れの件数。それぞれを含めて同じ検索を
    やり直した結果との差で数える。最大 100 件までしか数えない

## 通常の検索との比べ方

同じ query で、次の 3 つを呼び、件数とバイト数を表にする。

1. `search_memory(query=Q, project=P)`
2. `recall_context(query=Q, project=P)`
3. `recall_context(query=Q, project=P, token_budget=B, explain=True)`

そのうえで、3 の出力だけを読んでタスクを始められるかを人が判定する
(必要な記憶が `why:` 付きで入っているか、落ちた中に必要なものが無いか)。落ちた件数が多ければ
`token_budget` を上げるか、`memory_types` / `reference_prefix` で絞る。

2026-11-05 時点の実データ (1,900 件) で、`B=1500` (4,500 バイト) で比べた結果:

| query / project | search_memory 件数 / bytes | recall 既定 件数 / bytes | recall(B=1500, explain) 件数 / bytes | dropped |
|---|---|---|---|---|
| `watchd` / squad | 8 / 3,327 | 8 / 14,793 | 1 / 2,796 | budget=7, superseded=0, expired=0 |
| `references` | 28 / 9,904 | 10 / 18,795 | 2 / 4,343 | budget=18, superseded=6, expired=0 |
| `ADR` / kioku-mesh | 49 / 19,960 | 10 / 19,687 | 2 / 3,676 | budget=18, superseded=10, expired=0 |
| `VOICEVOX` | 1 / 430 | 1 / 1,823 | 1 / 1,933 | budget=0, superseded=1, expired=0 |

recall は content を全文出すので、1 件あたり 1〜2 KB になる。4,500 バイトでは 1〜2 件しか
入らない。予算の目安は「欲しい件数 × 700 token」程度。
