# scope enforcement への移行 (visibility enforcement cutover)

save は「宣言した scope の storage が稼働中の local zenohd に実在すること」を
publish のたびに確認するようになった (fail-closed)。同時に mesh storage は
pre-split の broad な `agent_mem` directory を離れ、`mem/mesh/**` だけを持つ
新しい clean directory へ移る。既存 host はこの 2 つを一つの maintenance
window で適用しないと **save が全部拒否される**。この文書はその window の
手順である。

| unit | 内容 |
|---|---|
| 1 | scope 契約 (`storage_scopes`)、fail-closed save preflight、drain / migration の同一 gate、read path selector、storage renderer (PR #316) |
| 2 | 二 node 統合テスト基盤 (PR #318) |
| 3 | `scope-migrate manifest` / `re-put`、`scope-inventory`、host-local `scope-purge` (PR #319) |
| 4 | この文書と CHANGELOG の breaking 告知 |

設計の根拠は ADR-0019 の Phase E Addendum とその追補 2 にある。ここは
「実際に何を、どの順で叩くか」だけを書く。

## この移行が前提にしていること

- **全 peer を一つの window で揃える。** key expression の違う storage は
  別の replica group になるので、旧 broad config のまま取り残された host は
  新しい `mem/mesh/**` group と alignment できない。live publication は届くが、
  遅れている間の差分は後から埋まらない (`__main__.py` の
  `_HALF_APPLIED_WARNING`)。片側だけ適用した状態で通常運用を再開してはならない。
- **旧 `agent_mem` directory は消さない。** final config から外れて unserved に
  なるだけで、rollback artifact として残す。`scope-purge` も対象にしない。
- **この移行は soft isolation であって秘密境界ではない。** 他 host に既に
  配られた copy は、その host の owner が host-local に purge しない限り残る。
  こちらから消す手段はないし、作らない。

## freeze の範囲と期間 (release gate)

**freeze = 「`mem/**` に PUT し得るプロセスを一つも動かさない」状態**であり、
次の両方を含む。

- MCP server process (Claude Code / Codex CLI などのクライアントごと落とす)
- `kioku-mesh save` / `mesh start` を含む CLI、および **raw Zenoh writer**
  (この repo を経由せず `mem/**` に直接 PUT するスクリプト・実験コード)

**期間は「手順 4 の manifest 生成の前」から「手順 7 の final verify 完了」まで。**
これを移行の release gate とする。freeze を確認できるまで手順 4 に進まない。

根拠: manifest は固定された snapshot である。manifest を取った後に raw writer が
旧 broad store へ `mem/mesh/...` を書くと、その key は

- manifest に無いので re-PUT されず、
- final config では旧 dir が unserved なので新 mesh storage にも存在せず、
- `verify_reput` は manifest key 集合との差分しか見ないため、`missing`
  (manifest にあって live に無い) にも `extra` (live にあって manifest に無い)
  にも現れない (`src/kioku_mesh/memory/scope_migration.py` の `verify_reput`)。

つまり **検出できないまま失われる**。verify が OK でも「freeze が守られていた」
ことの証明にはならないので、freeze は人間が保証するしかない。

## 手順

Home / Office の 2 台に対して実行する。**手順 3・5 は全 peer で行う**。
手順 4・6 は coordinator に決めた 1 台だけで行う。

zenohd の user unit 名は環境で異なる。先に実名を確認しておく:

```bash
systemctl --user list-units '*zenoh*'   # 例: kioku-mesh-zenohd
```

`Restart=on-failure` が付いている場合、`pkill` では respawn するので必ず
`systemctl --user stop <unit>` を使う。

### 1. 事前 gate: SQLite backup と legacy 0

全 host で SQLite index の backup を取る。SQLite の backup API を使う (稼働中の
byte copy は database ではない)。RocksDB directory の backup は手順 3 で zenohd を
止めたときに行う。

```bash
sqlite3 ~/.local/share/kioku-mesh/index.db \
  ".backup '$HOME/backup/index.$(date +%Y%m%dT%H%M%S).db'"
sqlite3 $HOME/backup/index.<timestamp>.db 'PRAGMA integrity_check;'
```

legacy `mem/{obs,tomb}/...` の移行は **storage 分割より前に**終わらせる。旧 broad
storage がまだ legacy key を serve している状態でなければ、0 件は「移行できた」
ではなく「見えていない」を意味する。

```bash
kioku-mesh doctor --check-legacy-namespace          # rc 0 = legacy 0 件
kioku-mesh migrate-visibility --from legacy --to <target scope> --dry-run
kioku-mesh migrate-visibility --from legacy --to <target scope> --yes
```

**gate**: 全 host で `doctor --check-legacy-namespace` が rc 0 (PASS)。rc 1 (WARN)
の間は先に進まない。

### 2. freeze

全 host で MCP クライアントを終了し、raw Zenoh writer を止める。zenohd は
**動かしたまま**にして replication を収束させる (manifest の source は Zenoh の
get であり、peer が答えられない key は manifest に入らない)。

**gate**: `mem/**` に書き得るプロセスが両 host で 0。以後、手順 7 の verify が
通るまで一つも起動しない。

### 3. transitional config を全 peer に適用

旧 broad `agent_mem` を `legacy_source_store` として読み取り用に残しつつ、
scope ごとの storage (新しい空の `mesh` dir を含む) を追加する。

```bash
kioku-mesh config render-storages --dry-run --transitional   # diff を確認
kioku-mesh config render-storages --apply --transitional
systemctl --user stop <unit>
cp -a ~/.local/share/kioku-mesh/agent_mem "$HOME/backup/agent_mem.$(date +%Y%m%dT%H%M%S)"
systemctl --user start <unit>
```

RocksDB directory の backup はここで取る (zenohd が止まっている間だけ整合する)。
root は既定 `~/.local/share/kioku-mesh`、`ZENOH_BACKEND_ROCKSDB_ROOT` があればそちら。

**gate**: 両 host で backup・apply・restart が完了していること。

この状態では `kioku-mesh doctor` の `storage_scopes` は **FAIL になるのが正しい**
(`broad/overlapping storage still present: legacy_source_store(mem/**)`)。
これは transitional 状態の想定どおりの表示であり、gate ではない。同じ理由で
save preflight も全部拒否するので、MCP は起動しない。

### 4. manifest を作る (coordinator 1 台)

```bash
kioku-mesh scope-migrate manifest --expected-peers 2 --dry-run
kioku-mesh scope-migrate manifest --expected-peers 2
```

`--expected-peers` は source get に答えるべき router の台数 (Home + Office = 2)。
答えなかった peer が持つ key は manifest に入らないまま消えるため、この不一致は
fail-stop になる。同じ key が違う payload digest で返ってきた場合も fail-stop で、
自動では解決しない。

manifest は immutable で、既に存在するパスには書かない。出力された manifest の
絶対パス、key 件数 (obs / tomb / other)、digest を記録する。

**gate**: `peers:` 行が期待値と一致し、`keys:` の内訳が事前 inventory と矛盾しない
こと。manifest ファイルが書かれたこと。

### 5. final config を全 peer に適用

`legacy_source_store` を外し、scope storage だけにする。

```bash
kioku-mesh config render-storages --dry-run     # --transitional なし
kioku-mesh config render-storages --apply
systemctl --user restart <unit>
kioku-mesh doctor
```

宣言 scope のうち SQLite index に observation があるのに storage が render されない
ものがあると、`--apply` は `--acknowledge-missing-scopes` なしでは拒否する。警告に
出た scope を `storage_scopes` に足すのが正しい対応で、acknowledge は
「その scope をこの host では保持しない」と決めたときだけ使う。

**gate**: 両 host で `doctor` の `storage_scopes` が PASS。ここで初めて re-PUT の
write gate が通る状態になる。

> 順序について: re-PUT は manifest の後、final config の後でなければならない。
> transitional config には `legacy_source_store` (`mem/**`) が残っており、write gate
> は「重複する broad storage が同じ key を受け取る」構成を必ず拒否する
> (`core/scope.py` の `_verdict_against_live`)。gate を緩める案は採らない。緩めれば
> broad store が同じ PUT を受け、誤った replica group への配布を許す。

### 6. re-PUT (coordinator 1 台)

```bash
kioku-mesh scope-migrate re-put --manifest <manifest path> --dry-run
kioku-mesh scope-migrate re-put --manifest <manifest path>
```

`--dry-run` は checkpoint と manifest の binding、key ごとの live storage gate、
そして最終 verify の予測まで、本番実行が止まる 3 つの判定を実際に走らせる。
**rc 0 であることが本番実行前の最後の砦**で、ここで落ちるものは本番でも落ちる。

本番実行は `re-put <件数>` の入力を求める (`--yes` で省略可)。途中で失敗しても
checkpoint から再開でき、同じ manifest に紐づかない checkpoint は merge せず拒否
する。

**gate**: `--dry-run` が rc 0。本番実行後に `verify: OK` が出ること。

### 7. final verify

```bash
kioku-mesh scope-inventory          # 全 host で
kioku-mesh doctor                   # 全 host で
```

- `re-put` の verify が `missing` / `payload digest mismatch` / `unexpected extra`
  すべて 0 (`verify: OK` の表示)。
- `scope-inventory` の Zenoh directory probe (selector `mem/**`) に、宣言していない
  scope の key が出ないこと。probe は storage の key expression ではなく directory
  の中身を列挙するので、これが clean dir の確認になる。
- `doctor` が両 host で PASS。

**gate**: 上記 3 つ。ここまで freeze を維持する。

### 8. freeze 解除

zenohd が両 host で final config で起動しており、手順 7 の gate が通ったことを
確認してから MCP クライアントを再開する。順序は
**zenohd 起動 → 検証 → MCP 再開**で、逆にしない。

### 9. (任意) host-local の stale copy 整理

宣言していない scope の copy がこの host に残っている場合のみ。

```bash
kioku-mesh scope-inventory
kioku-mesh scope-purge --dry-run
kioku-mesh scope-purge --yes
```

host-local 専用で、Zenoh delete は発行しない。RocksDB directory は削除ではなく
rename して退避する。`agent_mem` は rollback artifact なので purge 対象にならない。
legacy 行もここでは消さない (`migrate-visibility` の担当)。

## 新しい team scope を増やす日常運用

初回移行とは別の、以後ずっと使う手順。maintenance window も freeze も要らないが、
**その scope への最初の save より前に参加 host を全部揃える**。

1. 参加する host の `~/.config/kioku-mesh/config.yaml` の `storage_scopes` に
   `team/<new>` (または `user/<id>`) を足す。値は `mesh` / `user/<id>` /
   `team/<id>` のみで、wildcard と余分なセグメントは拒否される。`mesh` は必須。
2. `kioku-mesh config render-storages --dry-run` で diff と missing-scope 警告を
   確認する。
3. `kioku-mesh config render-storages --apply`。
4. `systemctl --user restart <unit>`。
5. `kioku-mesh doctor` が PASS することを確認する。長寿命の MCP process は
   preflight ごとに config と admin space を読み直すので、再起動は要らない。
6. その scope に参加する host すべてで 1-5 を行う。既存 scope しか持たない host を
   止める必要はない。

**gate**: 参加 host 全部で `doctor` PASS。1 台でも未適用のまま最初の save を行うと、
その host は同じ replica group に入っていないため、後から差分を埋められない。

## rollback

段階が 2 つあり、**別操作**である。片方を戻してももう片方は戻らない。

### 段階 1: read path の selector (flag)

`KIOKU_MESH_SCOPE_ISOLATION` は read path 専用。既定 (未設定) は従来どおりの global
selector で、`enforce` のときだけ subscriber / rebuild / fallback / purge の selector が
宣言 scope に絞られる。

- 戻し方: 環境変数を外して MCP process を再起動する。
- **この flag は write preflight を緩めない。** save が拒否される状態は flag では
  直らない。

### 段階 2: storage 構成 (config + 再起動)

- 戻し方: `config render-storages --apply --transitional` (旧 broad store を再び
  serve させる) か、backup した config を戻して zenohd を再起動する。**全 host で
  行う**。
- 旧 `agent_mem` dir を残してあるので、そこにあるデータは戻せる。ただし移行後に
  新 mesh dir へ入った書き込みは旧 dir には無い。
- `scope-purge` を実行した後は、rename して退避した directory を戻すか、手順 1 の
  backup から restore する必要がある。
- rollback 中も freeze は必要。broad store と scope store が同時に live な状態では
  save は拒否されるので、運用を再開する前に構成を確定させる。

## 限界

- **他 host に既に配られた copy は回収できない。** この enforcement は「これから
  配らない」ためのもので、過去に配布済みのデータは相手 host の owner が
  `scope-purge` を実行しない限り残る。こちらから消す API も権限もない。
- **freeze 違反は verify で検出できない。** 上記「freeze の範囲と期間」の根拠を
  参照。手順の遵守でしか担保できない。
- **doctor は peer 間の replication parameter の一致を検証しない。** Zenoh の admin
  space が replication 設定を公開しないため、doctor は自 host の config file を
  読んで表示するだけである。peer 間の一致は二 node harness (unit 2) で担保する。
- **これは配布先の soft isolation であり、confidentiality boundary ではない。**
  機密性が必要なら別 mesh、mTLS/ACL、または別の access-control ADR を採る。
