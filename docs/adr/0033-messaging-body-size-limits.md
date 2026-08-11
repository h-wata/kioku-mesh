# ADR-0033: messaging body サイズ上限は受信側 context で決める

- Status: Accepted
- Date: 2026-08-11
- Supersedes: なし
- Related: ADR-0022, Issue #185, Issue #202, PR #291, PR #295

## Context

ADR-0022 の messaging MVP 設計 memo（`docs/design/0185-messaging-mvp-design.md`）は、
MCP poll body 64 KiB / tmux 注入 8 KiB を**推奨値**として置いたまま、「正確な上限は Zenoh と
client UX に対して検証すべき」と保留していた。上限超過時の挙動（reject / truncate / split）も
未定義だった。Issue #202 はこれを実測で確定させる課題である。

実測した（単一ホスト・loopback、`zenohd` 1.9.0 + eclipse-zenoh Python 1.9.0、専用 router 1 台、
memory volume storage、`put` → 0.2s → `get`、各サイズ 5 試行）:

| body | wire | 結果 |
| --- | --- | --- |
| 1 KiB | 1,458 B | 5/5 byte 一致 |
| 64 KiB | 65,970 B | 5/5 byte 一致 |
| 1 MiB | 1,049,010 B | 5/5 byte 一致 |
| 16 MiB | 16,777,650 B | 5/5 byte 一致 |
| 64 MiB | 67,109,298 B | 5/5 byte 一致 |

envelope overhead はどのサイズでも一定 434 バイトだった。**Zenoh は 64 MiB を無傷で運ぶ。
すなわち transport は制約要因ではない。**

制約は受信側にある。`check_messages` は body を inline で返すため、そのまま LLM の context を
消費する。実測した応答サイズ（1 メッセージあたり metadata 447 B）:

| body | 1 message | 20 messages（default `limit`） |
| --- | --- | --- |
| 8 KiB | 8.6 KB | 168 KB |
| 64 KiB | 64 KB | 1.26 MB |

64 KiB の body 1 通で受信側におよそ 16k tokens を要求する計算になる（4 bytes/token の概算。
実トークナイザでの実測ではない）。64 KiB という数字を決めているのは transport ではなく、
この受信側 context である。

初回の実装は上限を**送信側にしか**適用しておらず、このバージョンが送らないものは何も
縛れていなかった。cross-review で 2 つの迂回経路が指摘された:

- `check_messages` は `body` が falsy のとき `payload` を返すため、`body=''` と約 100 KiB の
  legacy payload の組み合わせが inline で LLM に届いた（送信側検査は body 0 バイト・
  envelope 192 KiB 未満で通過する）
- MCP poll 経路も push subscriber も `Message.from_json` の後にサイズを再検査しておらず、
  旧バージョンの peer や外部 publisher が上限超過メッセージを直接 inbox key に置けた

さらに、body を withhold しても `delivery_adapters` などの非 body metadata は無条件に返って
おり、`body_rejected: true` のまま約 197 KB の応答が返る経路が残っていた。

## Decision

上限を次の値で確定し、送信側と受信側の両方で強制する。

| 経路 | 上限 | 単位 | 超過時 |
| --- | --- | --- | --- |
| MCP poll (`body`) | 64 KiB = 65536 | UTF-8 バイト | reject（`MessageBodyTooLarge`） |
| serialized envelope | 192 KiB = 196608 | バイト | reject |
| tmux 注入 | 8 KiB = 8192 | UTF-8 バイト | drop + WARNING |

- サイズは UTF-8 バイトで数える。ちょうど上限は受理し、上限 + 1 は拒否する。境界を
  またぐマルチバイト文字は、切らずにメッセージ全体を拒否する。
- 64 KiB は `body` そのものに適用する（従来は serialized message 全体に適用していたため、
  64 KiB の body が約 434 バイトの envelope のせいで拒否されていた）。`payload` や metadata が
  上限を迂回して内容を持ち込むことは、別枠の 192 KiB envelope 上限で防ぐ。
- **受信側でも再検査する。** deserialize 後に、legacy `payload` フォールバックを含む
  *実効* body に対して body 上限を、受信バイト列に対して envelope 上限を適用する。
  - `check_messages`（MCP poll）: **withhold して、そう言う。** `body` を実サイズ・上限・
    次の一手を明示した notice に差し替え、`body_rejected: true` を立て、`subject` を
    inline の第 2 経路として使われないようクリアする。メッセージ自体は `msg_id` /
    `sender` / タイムスタンプ付きで一覧に残す。
  - push subscriber: tmux adapter と同じく drop + WARNING。
- 非 body metadata は**フィールドごとに**上限を持つ。identity 系は 1 KiB、`subject` は 4 KiB、
  `delivery_adapters` は 16 件。上限超過 envelope では最小の identity と notice から item を
  再構築する。最後に item を実際に encode し、72 KiB（64 KiB の body 予算 + 8 KiB の
  headroom）を超えていれば body と残りの metadata を落とす。落としたフィールドは
  `withheld_fields` と notice の両方に出す。

## Consequences

- 良い点: 上限が推奨値から**実測に裏付けられた確定値**になり、根拠（受信側 context であって
  transport ではない）が記録された。
- 良い点: 応答サイズが `limit` × 72 KiB で bounded になる。上限超過 envelope が受信側の
  context を溢れさせる経路が塞がった。
- 良い点: 送信側だけでなく受信側でも検査するため、旧バージョンの peer や外部 publisher に
  対しても上限が効く。
- 悪い点: 64 KiB を超える内容は messaging では運べない。運用上は `save_observation` して
  短いポインタを送る形に誘導することになり、エラーメッセージにもそう書いてある。
- 悪い点: フィールドごとの上限は、応答全体の共有バジェットより実装と検証の点数が多い。
  各層が互いをマスクする（1 層を壊しても別の層が拾う）ため、層ごとに固有の観測点を持つ
  テストが必要になった。
- 未実測の範囲は明示しておく: mTLS / LAN / マルチホップ / rocksdb volume の挙動、64 MiB を
  超えるサイズ、実トークナイザによるトークン数。上の token 値は 4 bytes/token の概算である。

### 却下した選択肢

- **truncate（超過分を切り詰める）。** body は agent がそれを読んで行動する指示である。
  黙って切ると指示の意味が変わる。バイト単位の切断は UTF-8 シーケンスも割る。送信側は
  原文を保持しているので、送信時に失敗させても失うものは無い。
- **split（分割して送る）。** 再組立には順序が要るが、ADR-0022 の MVP は `sender_seq` を
  best-effort とし、順序を保証しないと明記している。MVP のスコープ外。
- **応答全体で 1 つのバジェットを共有する。** バジェットは sort 順に消費されるため、
  1 件の巨大メッセージが他のメッセージの内容を黙って押し出し、メッセージ B の見え方が
  メッセージ A に依存してしまう。これは PR #291 で blocking になった「部分結果が確定値に
  見える」失敗モードと同じである。フィールド単位なら保証が message ローカルに閉じ、
  identity は必ず残り、役割ごとに違う上限を付けられる。
- **poll 経路でも drop する。** `check_messages` は受信者にとって inbox の唯一の view で
  ある。そこで落とすと、メッセージが届いたことすら伝わらないまま内容が消える。
