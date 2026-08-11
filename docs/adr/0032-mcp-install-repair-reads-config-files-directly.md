# ADR-0032: mcp install --repair は CLI テキスト出力ではなく設定ファイルを直接読む

- Status: Accepted
- Date: 2026-08-11
- Supersedes: なし
- Related: ADR-0024, Issue #279, PR #287

## Context

`kioku-mesh mcp install --repair` は、既に登録済みの MCP エントリのうち、退役した
`MESH_MEM_*` identity env 変数（`MESH_MEM_AGENT_FAMILY` / `MESH_MEM_CLIENT_ID`）だけを
現行の `KIOKU_MESH_*` prefix に書き換える機能である（ADR-0024 の改名に伴う後始末）。
`--force` と違い、command / args / 他の env / このバージョンが知らないフィールドは
一切変更しない、というのが契約である。

初期実装は Claude Code の `claude mcp get` のテキスト出力を parse して既存エントリを
復元していた。この方式は cross-review を 3 巡しても収束しなかった:

1. 1 巡目: Args / Scope を復元しない、rollback 無し、TOML 全再生成 — blocking 6 件
2. 2 巡目: 「`Environment` 内で最初の env key の後にインデント付きの未知フィールドが
   来ると継続行と誤認する」で partial
3. 3 巡目: インデント付き drift は fail-closed できたが、今度は「桁 0 の non-KEY 行
   （bullet 等）を無条件に継続行扱いする」で partial。
   `KIOKU_MESH_AGENT_FAMILY=claude\n- bullet shaped drift` という独立 fixture で
   破損が再現された

「新しい drift パターンが見つかっては塞ぐ」の追いかけっこになっていたため、パターンを
1 つずつ潰すのをやめ、方式そのものを検証した（TASK-306）。判明した事実:

- **テキスト出力からの復元は原理的に非可逆。** 正当な multiline env 値の継続行は桁 0 に
  `- bullet shaped` や `Metadata: x` として現れ、drift 行と**バイト単位で同一**になる。
  入力が同一である以上、どんな判定規則でも両者を区別できない。3 巡の partial が続いた
  真因はこれであり、個別パターンの取りこぼしではなかった。
- **Args が空白で join されて出力される。** そのため `args=["--flag", "two words"]` の
  ような空白を含む引数は復元できない。当時の実装は、この形の登録を**エラーも警告も
  出さずに壊していた**。3 巡の cross-review でも検出されなかった実データ破壊経路である。
- `claude mcp get` に JSON 出力オプションは存在しない（`--help` で実測確認）。したがって
  「構造化出力に切り替える」という逃げ道も無い。

一方、設定の authoritative な保存先は実物で確認できた（隔離 HOME で往復検証済み）:

- user スコープ: `${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json` のトップレベル `.mcpServers`
- local スコープ: 同ファイルの `.projects["<cwd>"].mcpServers`
- project スコープ: `<repo>/.mcp.json`

JSON を直接書き換えると `claude mcp get` の出力にそのまま反映される。

## Decision

**`--repair` 経路から CLI テキスト出力の parse を完全に排除し、設定ファイル（JSON）を
直接 read-modify-write する。**

- エントリは JSON として lossless に読む。identity env の legacy prefix → current prefix の
  書き換え以外（command / args / 他の env / 未知フィールド / キー順序）は変更しない。
- 書き込みは、一時ファイルに書いて `os.replace` で atomic に置換する。置換前に
  バックアップを残し、置換後に再読込して意図どおりかを検証する。検証に失敗したら
  エラーで止める。
- 同名 server が複数 scope に見つかった場合は推測して選ばず **fail-closed** する。検出した
  scope を列挙し、どう解決すればよいかを示す actionable なエラーにする。
- Codex 側（`~/.codex/config.toml` の部分編集）は既にレビューで resolved 判定を得ているため
  変更しない。TOML の部分編集はこの ADR のスコープ外である。

## Consequences

- 良い点: 空白入り引数を静かに壊す経路が消える。JSON schema に依存するため、CLI の
  **表示書式**が変わっても影響を受けない。
- 良い点: 「復元できたかどうか」を判定するための発見的規則が不要になり、fail-closed の
  条件が「JSON として読めない」「同名 server が複数 scope にある」という明確なものだけに
  なる。
- 悪い点: 設定ファイルの**内部構造**（`.mcpServers` / `.projects[cwd].mcpServers` の
  レイアウト）に依存する。Claude Code が保存先やスキーマを変えたら追随が必要になる。
  テキスト書式への依存を、より安定だが別種の依存に置き換えたに過ぎない。
- 悪い点: 直接書き込みは、起動中の Claude Code セッションとの read-modify-write 競合を
  残す。ただしこれは従来の `claude mcp remove` / `add` 経路でも同じであり、本方式で
  新たに増えるリスクではないと判断した。
- 「`~/.claude.json` の直接編集は supported path ではない」という当時の docstring の前提は
  実測と食い違っていたため、実態に合わせて改めた。
- この ADR は**方式の決定**を記録するものであり、実装の完了を意味しない。本 ADR 作成時点で
  PR #287 には、fsync 失敗時にバックアップを消してしまう経路と、置換時に xattr / ACL が
  落ちる件の 2 件が blocking として残っている。

### 却下した選択肢

- **(B) テキスト parse を続け、受理する形式をホワイトリスト化して fail-closed を徹底する。**
  正当な継続行と drift 行がバイト単位で同一である以上、入力だけからは区別できない。
  ホワイトリストをどれだけ厳しくしても、正当な multiline env 値を誤って拒否するか、
  drift を誤って受理するかのどちらかになる。3 巡にわたり partial のまま収束しなかったのは
  実装の詰めの問題ではなく、この原理的な限界が原因である。
- **(C) 確実に復元できるケースだけを repair 対象とし、それ以外は手動対応を促す。**
  「確実に復元できるか」を判定するには、結局その非可逆な parse を通す必要がある。
  機能を縮小する代償を払っても、安全性は得られない。
