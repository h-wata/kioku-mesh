# ADR-0019: Observation visibility tier による selective replication

- Status: Accepted
- Date: 2026-06-06 (Revised: 2026-06-12, 2026-06-29 (Phase D addendum))
- Supersedes: なし
- Related: ADR-0001, ADR-0004, ADR-0006, ADR-0007, ADR-0010, ADR-0014

> **Revision 2026-06-12**: 初版の tier 名 `priv / team / pub` を
> **`user / team / mesh`** に改め、「Zenoh に載せない local-only tier（旧
> priv）」を廃止した。主用途（個人の複数 PC mesh）では「プライベートなメモ
> こそ自分のマシン間で同期されるべき」であり、初版の priv（1 台から出ない）
> はその直感と逆だった。また個人 mesh のデータに pub（公開）というラベルが
> 付くのも誤解を招く。詳細は Alternatives の Alt 5 を参照。

## Context

kioku-mesh の現行 Zenoh key layout は単一 namespace である。

```text
mem/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
mem/tomb/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
```

この形では、同じ mesh に参加する peer は原則として同じ `mem/**` storage を
replicate する。個人利用では十分だが、小規模 team で mesh を共有し始めると
次の要求が出る。

- OSS project の知見は team や全 peer に共有したい。
- 個人の observation は **自分のマシン間でだけ** 同期したい。team 全体へは
  replicate したくない。
- E2E 暗号化はまだ無いので、Zenoh に put した時点で hub / router / storage owner
  への信頼が必要になる。

ADR-0010 は Zenoh storage を source of truth、SQLite local index を派生 cache と
定義した。visibility tier はこの原則を維持したまま、**replication の届く範囲**
を key prefix で出し分ける。

## Decision

Observation に `visibility` を導入し、replication scope を key prefix で分ける。
tier 名は「秘密度」ではなく **「どこまで届くか」** で命名する。

```text
mem/mesh/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
mem/mesh/tomb/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}

mem/team/{team_id}/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
mem/team/{team_id}/tomb/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}

mem/user/{user_id}/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
mem/user/{user_id}/tomb/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
```

```text
visibility = mesh
  source of truth: Zenoh storage under mem/mesh/**
  replication: mesh storage を持つ全 peer へ複製

visibility = team
  source of truth: Zenoh storage under mem/team/{team_id}/**
  replication: 該当 team storage を設定した peer へ複製

visibility = user
  source of truth: Zenoh storage under mem/user/{user_id}/**
  replication: 該当 user storage を設定した peer（= その人の各マシン）へ複製
```

3 tier すべてが Zenoh storage を source of truth とする。ADR-0010 の
「Zenoh が正、SQLite は派生キャッシュ」は **例外なく全 tier に適用**される
（初版にあった「priv は local SQLite が SoT」という例外は廃止）。

### user_id / team_id の解決

- `user_id` は **設定ファイル（config）に永続化した slug** とし、
  `kioku-mesh init --user <id>`（または config.yaml の `user_id`）で宣言する。
  未設定時は OS の `$USER` を初期値として提案するが、マシン間で OS ユーザー名が
  揃っている保証はないため、確定値は必ず config に書き出す。
- `user_id` / `team_id` は **MCP tool 引数にしない**。ADR-0004 の identity と
  同じ理由で、LLM に渡させると誤値で namespace が汚染される。server / CLI が
  config から解決する。LLM が指定するのは `visibility`（mesh / team / user の
  選択）のみとし、複数 team に参加している場合の `team_id` 選択のみ例外的に許す。
- `user_id` / `team_id` は namespace slug であり、**security principal ではない**。

### Per-directory default — プロジェクトローカル設定（2026-06-12 追記）

default visibility はディレクトリ（リポジトリ）単位で切り替えられるべきである。
「個人開発のリポジトリでは user、チーム開発のリポジトリでは team」が自然な
運用であり、グローバル config 一本ではこれを表現できない。

`.editorconfig` 方式の **プロジェクトローカル設定ファイル** を導入する:

- カレントディレクトリから上方に `.kioku-mesh.yaml` を探索し、最初に
  見つかったものを採用する。MCP server はクライアント（Claude Code 等）が
  プロジェクトディレクトリを cwd として起動するため、CLI / MCP の両方で
  同じ探索が機能する。
- 解決の優先順位: **環境変数 > プロジェクトの `.kioku-mesh.yaml` >
  グローバル `~/.config/kioku-mesh/config.yaml` > 未設定（legacy）**。
- プロジェクトファイルで設定できるのは **`default_visibility` と `team_id`
  のみ**。`user_id` は人に紐づく識別子であり、リポジトリにコミットされうる
  ファイルから設定できてはならない（他人の clone が user namespace を
  乗っ取る事故の防止）。

#### 信頼上の注意

`.kioku-mesh.yaml` は **リポジトリ由来のコンテンツが書き込み先を変える**
仕組みである。悪意ある（または単に設定ミスのある）リポジトリを clone して
作業すると、意図せず team / mesh スコープへ保存される可能性がある。緩和策:

- save の応答に **実効 visibility を必ず表示**する
  （例: `saved: <id> (visibility=team/kioku-mesh)`）。エージェント・人間の
  双方が保存のたびにスコープを確認できる。
- visibility を**広げる方向**（user → team / mesh）の値がプロジェクト
  ファイル由来である場合の警告表示は、運用で必要になった時点で検討する。

これは ADR-0019 Alt 2 で却下した「project **名** で複製範囲を決める」とは
別物である。Alt 2 の問題（rename が storage migration になる）はここでは
発生しない — ディレクトリ連動するのは書き込み時の **default の選択** だけで、
確定した visibility / scope_id は従来通り payload と key に焼き込まれる。

### Isolation model

この ADR で採用するのは **Soft isolation** である。

Soft isolation は「誤って同期しない」ための分離であり、confidentiality boundary
ではない。ACL なしの Zenoh mesh では、network 上到達できる peer が
`session.get("mem/user/{user_id}/**")` を直接実行すれば data を読める可能性がある。
また hub-and-spoke（ADR-0006）では、user scope のデータも hub を **経由**する
（hub が該当 storage を持たなければ保存はされないが、wire 上は通る）。

したがって:

- `user` / `team` は storage / replication 設定で同期範囲を狭めるが、秘密境界ではない。
- 個人 mesh（全 peer が自分のマシン）では、この Soft isolation で実用上十分。
- 機密性を要求する共有 mesh では、将来 ADR-0014 の mTLS に加えて Zenoh ACL を
  導入し、cert subject / ACL principal に権限を結びつける必要がある。
  その際も `user_id` / `team_id` だけで権限を判断してはならない。

### Zenoh storage configuration

Peer は参加する scope だけ storage を持つ。

```json5
storages: {
  mesh_store: {
    key_expr: "mem/mesh/**",
    strip_prefix: "mem/mesh",
    replication: { /* same across mesh peers */ },
    volume: { id: "rocksdb", dir: "mesh" },
  },
  user_hwata_store: {
    key_expr: "mem/user/hwata/**",
    strip_prefix: "mem/user/hwata",
    replication: { /* same across this user's machines */ },
    volume: { id: "rocksdb", dir: "user_hwata" },
  },
  team_kioku_mesh_store: {
    key_expr: "mem/team/kioku-mesh/**",
    strip_prefix: "mem/team/kioku-mesh",
    replication: { /* same across this team */ },
    volume: { id: "rocksdb", dir: "team_kioku_mesh" },
  },
}
```

Hub は必要な scope の storage だけを持つ。全 user / 全 team を集約する管理 hub を
除き、`mem/user/**` / `mem/team/**` の広い wildcard storage は避ける。
その scope に参加しない peer は該当 storage block を書かない。

### API and search behavior

`save_observation` / CLI save に visibility 指定を追加する。

```text
visibility: mesh | team | user
team_id: optional, required when visibility == team かつ複数 team に参加している場合
（user_id は引数にしない — config から解決）
```

個人 mesh での default visibility は config で指定可能とし、初期値は `user` を
推奨する（個人の作業メモが最も多いため）。

Search の default scope は `reachable` とする。

```text
reachable = user(自分) + joined teams + mesh
```

共有や export のように漏洩影響がある操作では、scope を明示させる。

### Legacy migration

既存 `mem/obs/**` / `mem/tomb/**` は legacy namespace として段階移行する。

1. Phase A: legacy read を継続する。subscriber / rebuild / fallback scan は
   `mem/obs/**` と新 namespace の両方を読む。
2. Phase B: 新規 write は visibility-aware key に分岐する。default visibility は
   config で指定可能にする。
3. Phase C: migration CLI を提供する。legacy data の移行先はユーザーが明示する。
4. Phase D: legacy write を廃止し、十分な猶予後に legacy read を optional fallback に落とす。

既存 legacy data を自動的に `mesh` 扱いにしない。現行 layout は全体共有だったが、
ユーザー意図として個人用の observation が混ざっている可能性があるため、
migration target は明示指定にする。

```text
kioku-mesh migrate-visibility --from legacy --to user
kioku-mesh migrate-visibility --from legacy --to mesh
kioku-mesh migrate-visibility --from legacy --to team/kioku-mesh
```

## Consequences

- 良い点: 個人マシン間のみ、全体共有、team 共有を同じ mesh 上で扱える。
  個人 2 台運用の「自分のメモは自分の全マシンに届く」という直感と tier 名が一致する。
- 良い点: 全 tier が Zenoh-backed なので ADR-0010 の SoT 原則に例外がなくなり、
  「priv だけ SQLite が SoT」という初版の特殊扱い（とその backup 責務問題）が消える。
  user データも複数マシンに複製されるため host loss に強い。
- 良い点: `user` / `team` / `mesh` は Zenoh key prefix と storage config の組み合わせで
  replication scope を制御でき、新しい同期プリミティブを必要としない。
- 良い点: 将来 Hard isolation を導入するとき、`user_id` / `team_id` namespace と
  ACL principal を別概念として扱える。
- 悪い点: `Observation.key_expr`、tombstone key、subscriber、rebuild、fallback scan、
  GC/delete、LocalIndex schema、search filter の広い範囲が visibility-aware になる。
- 悪い点: RocksDB directory が scope ごとに分かれ、disk 管理と config 管理が複雑になる。
- 悪い点: Soft isolation は漏洩防止ではない。ACL なしの shared network では user / team
  data を秘密として扱えない（個人 mesh では実害なし、共有 mesh では ACL が前提）。
- 悪い点: 「Zenoh に一切載せない」tier が無くなったため、ホスト外に絶対に出したくない
  メモの受け皿がない。必要になった場合は machine-local tier を別途追加する
  （初版の priv 相当。現時点で具体的な需要がないため見送り）。
- 悪い点: `user_id` という新しい識別子が増え、init / config / ドキュメントの
  説明面積が広がる。マシン間で `user_id` を揃え忘れると「自分のメモが届かない」
  事故になるため、`doctor` での検査を実装時に入れる。

## Alternatives Considered

### Alt 1: 最初から mTLS + ACL の Hard isolation を必須にする

Security boundary としては最も明確だが、小規模 team / 個人 mesh の導入コストが高い。
ADR-0014 の mTLS は既に opt-in であり、ACL 設計・証明書 subject 設計・team enrollment
運用まで同時に要求すると、visibility 導入の実装範囲が大きくなりすぎる。

まず Soft isolation を入れ、必要になった段階で ACL を重ねる。

### Alt 2: project name で replication scope を決める

`project` は検索・分類用 metadata であり、storage / security namespace ではない。
同じ project 名が peer 間で衝突する可能性もある。Replication scope を project に
結びつけると、rename や分類変更が storage migration になってしまうため却下。

### Alt 3: 既存 `mem/obs/**` を維持し、payload field だけで visibility を持つ

Search filtering は簡単だが、Zenoh storage replication は key_expr に基づくため、
payload field だけでは selective replication できない。Hub に全 data が入る問題も
解決できないため却下。

### Alt 4: すべてを local-only にして export / import で共有する

Confidentiality は単純になるが、kioku-mesh の価値である eventual replication と
cross-agent shared memory を失う。Team sharing の本命 path ではなく、別機能として扱う。

### Alt 5: 初版の priv / team / pub（priv = Zenoh に載せない local-only）

初版（2026-06-06）の設計。`priv` は Zenoh に一切 put しないことで「E2E 暗号化が
無くても hub に残らない」性質を持っていたが、次の理由で改めた。

- 主用途である **個人の複数 PC mesh** では、「プライベートなメモ」こそ自分の
  マシン間で同期されてほしい。priv（1 台から出ない）はこの直感の逆を向いており、
  ユーザーは個人メモを `pub` に保存することになる。個人データに「public」という
  ラベルが付くのは命名として誤解を招く。
- priv だけ「local SQLite が source of truth」という ADR-0010 の例外になり、
  rebuild / reconcile / backup の責務が二系統に分裂する。
- 「ホスト外に絶対出さない」用途は現時点で具体的な需要が確認できていない。
  必要になれば machine-local tier を後から追加できる（Zenoh key を持たない設計の
  追加は、既存 tier に影響しない）。

「秘密度（priv/pub）」ではなく「届く範囲（user/team/mesh）」で命名し直し、
local-only tier は需要が出るまで見送る。

## Phase C Addendum: migrate-visibility CLI

- Status: Accepted addendum
- Date: 2026-06-26

### Context

ADR-0019 Phase A/B により新規書き込みは visibility-tiered namespace に入るが、
既存の legacy `mem/obs/...` / `mem/tomb/...` 鍵が残り続ける。これらを自動的に
mesh 名前空間へ移行することは安全ではない。legacy データには個人的なメモが含まれる
可能性があり、移行先 namespace の選択は所有者が明示的に指定すべきである。

### Decision

`kioku-mesh migrate-visibility --from legacy --to <target>` CLI を提供する。

移行アルゴリズム:
1. Zenoh を列挙元とする（SQLite local index は使用しない）
2. `_iter_ok_replies` でレコードを収集してから副作用を実行する
3. obs payload の `visibility` / `scope_id` フィールドを書き換えて新キーを生成する
4. tomb は identity セグメントから新キーを生成し、payload はそのまま保持する
5. 移行順序: backup 書き出し → PUT target → verify target → DELETE source (exact key)
   → repair PUT target → local index rebuild
6. backup（JSONL manifest + payload ファイル）と checkpoint（atomic replace）は
   execute モードで必須。dry-run では一切書き込まない
7. exact key の個別削除のみ許可する（wildcard delete 禁止）

source DELETE 後に target repair PUT が必要な理由:
subscriber の DELETE コールバックは observation_id 単位で物理削除を行うため、
source DELETE が先に届くと新しい target キーの SQLite 行も消去される可能性がある。
repair PUT により local index は収束する。

--to user は user_id を config 解決する（KIOKU_MESH_USER_ID または config.yaml）。
user/<id> 形式での直接指定は ADR-0019 の禁則により拒否する。

### Consequences

- 移行は中断・再実行が安全（checkpoint + idempotent PUT）
- 移行中は legacy と tiered の両方の鍵が共存する（既存の read selector が両方をカバー）
- source 削除により SQLite sidecar が一時的に不整合になる可能性があるが、
  target repair PUT と最終 index rebuild により収束する
- long-lived peer は migration 後に再起動または `--rebuild` を推奨する

## Phase D Addendum: legacy write 廃止 + legacy read 格下げ

- Status: Accepted addendum
- Date: 2026-06-29

### Context

ADR-0019 Phase A/B により新規書き込みは visibility-tiered namespace に入り、
Phase C では `migrate-visibility` CLI により既存 legacy データの明示的な移行が可能になった。

しかし、**未設定の writer が依然として legacy layout に書き続けるリスク**が残る。
`config.resolve_write_visibility` が設定なしの場合に空文字列 `''` を返し、
`keyspace._namespace_prefix('', '')` が `mem` を返す経路がそのまま残っているためである。

また、**legacy read が常時有効な状態では migration 漏れが見えにくい**という問題もある。
`subscriber` / `rebuild` / `search` / `find-by-id` がすべて `mem/obs/**` と `mem/tomb/**` を
読み続けるため、移行が完了しているかどうかを運用者が判断しにくい。

Phase D はこの二つの残存リスクに対処し、legacy namespace への依存を段階的に解消する。

### Decision

#### 1. cut-off: v0.8

legacy write の廃止と legacy read のオプション化は **v0.8** で実施する。

- **v0.7 は too soon**: Phase C (`migrate-visibility` CLI) と同リリースになるため、
  ユーザーが移行を完了する猶予期間がない。
- **v1.0 は too late**: 0.x 全期間にわたって未設定 writer が legacy データを生成し続け、
  migration 対象が増え続ける。

v0.8 は Phase C が出荷されてから少なくとも 1 マイナーリリース分の猶予を確保しつつ、
v1.0 では互換シムの **削除** に集中できるバランス点である。

#### 2. legacy write: v0.8 で default 廃止

v0.8 以降、CLI save および MCP save は空 visibility を正常系の書き込み先として許可しない。
`config.resolve_write_visibility` は `''` を返す経路を廃止し、
設定が未解決の場合はアクションエラーを返す。

**v0.8.x 限定の escape hatch**: `KIOKU_MESH_LEGACY_WRITE_EMERGENCY=on`

- default は `off`
- 有効にすると legacy layout (`mem/obs/...`) への書き込みが一時的に復活する
- 起動時に warn ログを出力する（MCP は once-per-process、CLI は save ごと）
- v1.0 でこの環境変数は削除される
- 利用後は `kioku-mesh migrate-visibility` の再実行を推奨する

#### 3. legacy read: `KIOKU_MESH_LEGACY_READ_FALLBACK=on|off`

v0.8 cutoff 後の default は **`off`**。

- `on` にすると `subscriber` / `rebuild` / `search` / `find-by-id` が
  `mem/{obs,tomb}/...` (legacy namespace) を含むようになる
- fallback が有効になった際に **once-per-process で warn** を出す
- legacy データが実際にヒットした際にも **once-per-process で warn** を出す
  （レコード単位の warn は大量のログを生むため禁止）
- 設定の解決優先順位は既存の visibility 設定と同じ:
  **環境変数 → グローバル `config.yaml`**
  （プロジェクト `.kioku-mesh.yaml` は read-only 設定のみ対象であり、
  この環境依存フラグはグローバル設定で管理する）

#### 4. 未移行データ検知: doctor 拡張 (`check_legacy_namespace`)

新しい top-level コマンドは追加しない。既存の `doctor` に `check_legacy_namespace` を追加する。

- `scan_legacy_visibility` のセレクターを流用し、`obs` / `tomb` の件数を別個に集計する
- サンプルキーとスコープ要約を JSON / テキストで出力する
- **cutoff 前**: WARN（移行を促すヒントを表示）
- **cutoff 後かつ fallback off**: WARN-with-cutoff（見えていないデータがある旨を強調）
- JSON 出力: `{legacy_obs, legacy_tomb, samples, fallback_enabled}`
- テキスト出力のヒント: `kioku-mesh migrate-visibility --from legacy --to <user|team|mesh>`

#### 5. README 更新は PR(2) で実施

`KIOKU_MESH_DEFAULT_VISIBILITY` の「unset = legacy layout」という記述の差し替えは
この memo PR では行わない。PR(2) で `resolve_write_visibility` を変更する際に合わせて対応する。

#### 6. ADR-0028: invariant test fixtures は PR(4) で更新

通常の `rebuild` / `shadow` テストは `visibility='mesh'` の Observation を使う。
legacy 互換テスト (`test_visibility_write.py` 等) は別ファイルに残す。

#### PR 分割案 (依存順)

| # | タイトル | スコープ | 依存 |
|---|----------|----------|------|
| (1) | Phase D preflight: doctor legacy namespace check | `doctor` に `check_legacy_namespace` 追加、additive のみ | なし |
| (2) | Phase D: stop default legacy writes | `resolve_write_visibility` 変更、CLI/MCP save 修正、テスト更新 | (1) |
| (3) | Phase D: make legacy reads opt-in fallback | `KIOKU_MESH_LEGACY_READ_FALLBACK` 導入、`selector`/`subscriber`/`rebuild`/`search` 分岐 | (2) |
| (4) | Phase D docs/tests cleanup | README 更新、ADR-0028 fixture tiered 化、cleanup | (3) |

実装の詳細（影響ファイル・行番号・シンボル）は各 PR の commit message / PR description に記載する。
本 memo PR には要旨のみ載せる。

#### 影響範囲 (要旨)

- `config.resolve_write_visibility` / `config.get_default_visibility`:
  空 visibility を返す経路を廃止
- `keyspace._namespace_prefix` / `keyspace.obs_key` / `keyspace.tomb_key`:
  空 visibility を新規書き込みバリデーションから除外
- `Observation.visibility` 空文字デフォルト:
  新規書き込みパスでは非空 visibility を要求する（旧ペイロード読み込みパスは維持）
- 詳細な file:line リストは TASK-234 設計レポート (`worker4_design_234.yaml`) を参照

#### ロールバック

重大な書き込み回帰が発生した場合は v0.7.x へのバージョンピンを推奨する。
v0.8.x 限定で `KIOKU_MESH_LEGACY_WRITE_EMERGENCY=on` を使う場合は、
復旧後に `kioku-mesh migrate-visibility` を再実行すること。
v1.0 でこの escape hatch は削除される。

#### Migration guide (cutoff 前にやること)

1. `kioku-mesh doctor --check-legacy-namespace` で legacy namespace の残存データを確認する
2. `kioku-mesh migrate-visibility --from legacy --to <user|team|mesh>` を実行する
3. `kioku-mesh doctor` で `check_legacy_namespace` が 0 件になることを確認する (cutoff 確認)
4. v0.8 以降は `KIOKU_MESH_LEGACY_WRITE_EMERGENCY` に頼らずに運用する

### Consequences

- 新インストールおよび移行済み mesh が legacy データを蓄積しなくなる。
- `fallback off` により未移行データが見えなくなるリスクは `doctor` / `migrate-visibility` /
  `KIOKU_MESH_LEGACY_READ_FALLBACK=on` の三段構えで緩和する。
- 実装タッチポイントは `config` / `keyspace` / `subscriber` / `rebuild` / `search` /
  CLI/MCP save / README / test fixtures と広範。詳細は Phase D split PR 4 本の
  各 PR description に記載する。

## Phase D Implementation Status

**Completed** (2026-06-30)

| PR | タイトル | ステータス |
|---|---|---|
| #254 | Phase D PR(1): doctor preflight (`check_legacy_namespace` 追加) | merged |
| #256 | Phase D PR(2): BREAKING — stop default legacy writes | merged |
| #257 | Phase D PR(3): make legacy reads opt-in fallback (`KIOKU_MESH_LEGACY_READ_FALLBACK`) | merged |
| #TBD | Phase D PR(4): docs/tests cleanup (Closes #220) | this PR |

Issue #220 はこの PR(4) のマージをもって close される。

## Phase E Addendum: storage / subscriber scope enforcement と clean mesh dir への移行

- Status: Accepted addendum
- Date: 2026-08-17

### Context

本体は tier ごとの key prefix と storage 設定で replication scope を分けると決めたが、
「この host がどの scope を保持するか」の宣言と、稼働中の zenohd が実際に持つ storage を
突き合わせる仕組みが無かった。結果として次の失敗が起きうる。

- config が user / team scope への書き込みを解決しても、その scope の storage が
  render / restart されていなければ、save は成功を返しながらどこにも永続化されない。
- read path（subscriber / rebuild scan / purge sweep）が宣言外の namespace を読み、
  この host が保持しない scope の key を local index に取り込む。

加えて、本体の Zenoh storage configuration 節にある `mesh_store` の例には、実機検証で
判明した二つの落とし穴がある。

1. `strip_prefix: "mem/mesh"` は、既存の on-disk key（`strip_prefix: "mem"` の broad
   storage が `mesh/obs/...` として保存したもの）を `mem/mesh/mesh/obs/...` として
   再構成する。移行前の key では読めなくなる。
2. **Zenoh replication alignment は storage の key_expr 外であっても、その directory に
   既にある key を peer へ配布し得る。** したがって scope 分割に既存 dir を再利用して
   はならない。既存の broad な `agent_mem` dir を mesh storage に流用すると、key_expr が
   `mem/mesh/**` だけであっても、その dir に残る user / team / legacy key が mesh
   replica group の全 peer へ配られる。

**本 addendum は、本体の Zenoh storage configuration 節にある `mesh_store` の例
（`strip_prefix: "mem/mesh"`, `dir: "mesh"`）を置き換える。既存 mesh を運用中の host で
その本体例を適用してはならない。**

### Decision

#### 1. host-global `storage_scopes` を単一の導出元にする

各 host は保持・購読する scope を、project cwd から導出した単一 `team_id` ではなく
host-global config の明示リストで宣言する。

```yaml
storage_scopes:
  - mesh
  - user/hwata
  - team/sbgisen
```

許可値は `mesh` / `user/<user_id>` / `team/<team_id>` のみ。wildcard・legacy（un-tiered）・
余分なセグメントは正規化せず拒否する（typo が保持範囲を黙って広げてはならない）。`mesh` は
必須とする。zenohd storage 設定の render、read path の selector、save preflight は
すべてこの一つの契約から導出し、互いに drift しない。project config は書き込み先 scope を
決めてよいが、保持されるかどうかを決めるのは `storage_scopes` である。

#### 2. save preflight は fail-closed

publish 前に、書き込み先 scope が (a) `storage_scopes` に宣言され、かつ (b) **稼働中の
local zenohd** が exact な storage（`key_expr` と `strip_prefix` が一致し、重複する broad
storage が無い）で実際に serve していることを Zenoh admin space で毎回確認する。宣言だけでは
根拠にならない。render されていない config 編集、render したが restart していない zenohd は
どちらも「永続化先が無い」状態である。

- 確認は self-scoped selector（`@/<self_zid>/...`）で行う。wildcard `@/*` は到達可能な
  remote peer の生死に待たされるため save path には置かない（実測 median 8.0 ms /
  max 221.5 ms 対 0.1 ms）。
- キャッシュしない。長寿命 MCP process が config 編集・renderer apply・zenohd 再起動を
  即座に反映できることを優先する。
- WARN で通す flag は用意しない。warn して通した save は、まさにこの設計が消そうとして
  いる「保存されたように見えて永続化されていない」状態である。
- 判定は `session.put()` / SQLite upsert / pending-puts enqueue の前に行い、拒否された
  save は痕跡を残さない。`drain_pending_puts` も同じ判定を通し、通らない entry は削除せず
  queue に残して doctor に見せる（queue は 4 つ目の write sink であり、そこを素通りさせると
  受け皿の無い put を発行して pending の記録だけが消える）。
- `migrate-visibility` の target PUT / repair PUT も同じ live target scope の契約を通す
  （5 つ目の write sink）。ただし migration では下記 Tier 1 例外を**認めない**。migration は
  target PUT の直後に legacy source key を DELETE するため、永続 storage の無い target を
  受理すると唯一の copy を失う。判定は batch の先頭で target key ごとに一度行い、拒否時は
  source key・target key・checkpoint のいずれにも触れない。
- ZENOH_CONNECT が local router を指していない場合は、admin space が "self" として返す
  storage が他 host のものである可能性があるため拒否する。

#### 3. read path も同じ scope 集合から導出する

subscriber / rebuild scan / purge sweep の selector を `storage_scopes` から導出する。
初回リリースの既定は `KIOKU_MESH_SCOPE_ISOLATION` 未設定 = 従来どおりの global selector で、
`KIOKU_MESH_SCOPE_ISOLATION=enforce` で宣言 scope に絞る opt-in とする。この flag は read
path 専用であり、上記の write preflight を緩めない。

#### 4. mesh storage は新規の clean directory に置き、re-PUT で移す

mesh storage は `key_expr: "mem/mesh/**"`、`strip_prefix: "mem"`、**新規の空 RocksDB
directory**（`mesh`）で構成する。`strip_prefix` を `mem` に留めるのは既存の `mem/mesh/...`
key が on-disk で `mesh/...` の形を保つためである。既存 `agent_mem` の `mem/mesh/**` key は
write freeze 下で manifest 化し、key と payload を新 dir へ re-PUT する。旧 dir の
user / team / legacy key を新 dir に移してはならない。

新 dir + re-PUT を採ったのは、Context の 2 の性質があるためである。既存 dir を再利用すると
key_expr の外にある user / team / legacy key が mesh replica group 経由で配布され続け、
host-local な purge も相手側の alignment で書き戻される。dir を新規にすれば汚染経路そのものが
消え、新 dir に入るのは `mem/mesh/**` に限定された re-PUT だけになる。

同一 scope の replica は `key_expr` / `strip_prefix` / 全 replication parameter を同一にする。
通常 host に wildcard の `mem/user/**` / `mem/team/**` storage は置かない。

**半端な cutover は自己修復しない。** key_expr が異なる storage は別の replica group に
なるため、旧 broad config のまま取り残された host は新 `mem/mesh/**` group と align できない。
live publication は届くが、遅れている間の差分は後から埋まらない。全 peer に対して一つの
maintenance window 内で適用し、部分変換のまま通常運用を再開しない。

有効化手順は、全 MCP process 停止（save freeze）→ 全 peer の zenohd を transitional config →
re-PUT → final config の順に揃え、digest と clean-dir inventory を検証してから MCP を起動する。
MCP の selector flag と storage config の rollback は別操作であり、local purge 後の rollback には
backup restore が必要である。

#### 5. Tier 1（`kioku-mesh mesh start`）の非永続例外

`mesh start` が開く in-process router は storage_manager plugin を読めず、admin space も
storage も持たない。この router に対しては、**`storage_scopes` が `mesh` のみ、かつ接続先
endpoint が local** のときに限り mesh scope の save を受理し、「peer には live に届くが
storage は保持しない」ことを process 内で一度 log に出す。user / team scope の書き込みと、
storage が render されていない実 zenohd に対する書き込みは従来どおり拒否する。remote endpoint
はこの例外の対象外で、write は拒否される。doctor はこの状態を（local endpoint の Tier 1 に
限って）WARN として「durable に保存されない」と明示し、それ以外で宣言と live storage が
一致しない場合は FAIL とする。

#### 6. doctor による照合

doctor は宣言 `storage_scopes` と self の admin-space storage 定義（`key_expr` /
`strip_prefix` / volume dir）を照合し、不一致・重複・宣言外の broad storage・scope preflight で
止まっている queued put を FAIL として報告する。replication parameter は admin space が
公開しないため、doctor は renderer が書いた config file 側と突き合わせ、peer 間の一致は
two-node harness で担保する（doctor で見えると書いたまま実装すると、検出できない不一致が
検出済みのつもりになる）。

#### 7. legacy 移行と stale copy の扱い

legacy `mem/{obs,tomb}/...` の visibility migration は storage 分割の**前に**、owner が
target scope を明示して copy → verify → exact delete → repair PUT → checkpoint で完了させる。
legacy scan の 0 件は、旧 storage がまだ legacy key を serve している時点で確認する。
stale copy の cleanup は local inventory と backup、明示確認付きの host-local purge に限り、
Zenoh key delete を publish してはならない。

### Consequences

- 良い点: 「保存できたが永続化されていない」が構造的に起きなくなる。書き込み先 storage の
  存在が publish の前提条件になる。
- 良い点: storage 設定・read selector・write gate が一つの宣言から導出されるため、config と
  実挙動の drift が doctor で検出可能になる。
- 悪い点: zenohd が停止している間は save が一切通らない。従来は SQLite + pending_puts で
  受理されていたため、利用者から見た挙動変化は大きい（breaking change として告知する）。
- 悪い点: storage cutover を終えていない host では全 save が拒否される。cutover は全 peer で
  一つの window 内に完了させる必要があり、部分適用は自己修復しない。
- 悪い点: mesh dir を作り直すため、移行には write freeze と re-PUT、および旧 dir を
  rollback artifact として保持する運用が必要になる。
- 限界: **他 host に既に配られた copy は、その host の owner が purge しない限り残る。**
  本 addendum の enforcement は「これから配らない」ためのものであり、過去に配布済みの
  データを回収しない。
- 限界: **本決定は soft isolation のままであり、confidentiality boundary ではない。**
  本体 Isolation model 節の記述はそのまま有効で、enforcement を入れても user / team scope が
  秘密境界になるわけではない。機密性が必要なら別 mesh、mTLS/ACL、または別の
  access-control ADR を採用する。

### Phase E Implementation Status

**Partially implemented** (2026-08-17)

| task | 内容 | ステータス |
|---|---|---|
| 1 | scope 解決 API (`core/scope.py`)、fail-closed save preflight、drain / migration の同一 gate | implemented (PR #316) |
| 2 | read path selector の scope 導出 (`KIOKU_MESH_SCOPE_ISOLATION`) | implemented (PR #316) |
| 3 | zenohd storage renderer (`core/storage_render.py`, `config render-storages`) | implemented (PR #316) |
| 4 | 二 node 統合テスト基盤（clean mesh dir への alignment 検証、replication parameter 一致） | 未実装 |
| 5 | migration / host-local purge ツール（obs/tomb 別集計、backup、dry-run-first） | 未実装 |
| 6 | release / runbook と breaking change 告知 | 未実装 |

task 1+2+3 は main（merge commit `9eb57d6`）に入っている。上記のうち実機の cutover 手順・
purge ツール・利用者告知（task 4/5/6）は未実装であり、この addendum は決定の記録であって
「移行が完了した」ことを意味しない。

## Phase E Addendum 2: cutover 順序の訂正と verify の検出限界

- Status: Accepted follow-up
- Date: 2026-08-18
- 対象: 上記 Phase E Addendum の Decision 4 と Phase E Implementation Status
- 本追補は既存の記述を書き換えない。下記の 1 は Decision 4 末尾の手順段落を
  **置き換える**（ADR は append-only なので、原文はそのまま残る）。

### 1. 有効化手順の順序訂正

Phase E Addendum の Decision 4 末尾にある「有効化手順は、全 MCP process 停止
（save freeze）→ 全 peer の zenohd を transitional config → re-PUT → final config
の順に揃え、…」の一段落は、以下に置き換える。

> 有効化手順は、legacy migration 完了（legacy count 0）→ 全 MCP process と raw
> Zenoh writer の停止（save freeze）→ 全 peer の zenohd を transitional config →
> **manifest 固定** → 全 peer に final config を適用して再起動 → **re-PUT** →
> digest と clean-dir inventory の検証、の順に行う。検証が通るまで freeze を解かず、
> その後に MCP を起動する。

訂正の理由は、実装した write gate と transitional config が両立しないためである。
transitional config は旧 broad `agent_mem` を `legacy_source_store`（`mem/**`,
`strip_prefix: mem`）として残す。一方 save preflight は「宣言 scope の exact storage
が live で、かつ**同じ key を受け取る重複した broad storage が無い**こと」を要求する
（`core/scope.py` の `_verdict_against_live`）。re-PUT も同じ gate を通るため、
transitional config が live な間は re-PUT が必ず拒否される。したがって re-PUT は
final config を適用した後にしか実行できず、その source となる manifest は
transitional config の下で先に固定しておく必要がある。

gate を re-PUT に対してだけ緩める案は採らない。緩めれば旧 broad store が同じ PUT を
受け取り、`mem/mesh/**` の値が broad な replica group を通じて配布される。これは
Decision 4 が新 dir を導入して断とうとした汚染経路そのものである。手順の順序を変える
ほうが、gate に例外を作るより安全である。

### 2. freeze 違反は verify では検出できない

manifest 固定後に raw Zenoh writer が旧 broad store へ `mem/mesh/...` を書いた場合、
その key は失われ、しかも **`verify_reput` では検出できない**。

- manifest は snapshot なので、その key は manifest に無い。
- final config では旧 dir が unserved になるので、live query にも現れない。
- `verify_reput` は manifest key 集合と live key 集合の差分しか見ない。上記の key は
  `missing`（manifest にあって live に無い）にも `extra`（live にあって manifest に
  無い）にも入らない（`src/kioku_mesh/memory/scope_migration.py`）。

これは Zenoh storage の性質と snapshot 方式の組み合わせから来る構造的な限界であり、
実装で埋められない（埋めるには「freeze 中に書かれた key」を知る必要があり、それは
freeze が守られていれば存在しないものである）。したがって **freeze の範囲に raw
Zenoh writer を含めること、および freeze を manifest 生成前から final verify 完了まで
維持することは、運用上の release gate として扱う**。`verify: OK` は「freeze が守られた」
ことの証明にはならない。

### 3. Implementation Status の更新

上記 Phase E Implementation Status の表は task 4/5/6 を未実装として記録している。
2026-08-18 時点では以下のとおり全 task が main に入っている。

| task | ステータス |
|---|---|
| 1-3 | implemented (PR #316, merge commit `9eb57d6`) |
| 4 | implemented (PR #318, merge commit `2139d09`) — 二 node 統合テスト基盤 |
| 5 | implemented (PR #319, merge commit `8dd0e91`) — `scope-migrate` / `scope-inventory` / `scope-purge` |
| 6 | implemented — `docs/scope-enforcement-cutover.md` と CHANGELOG の breaking 告知 |

実機の cutover 自体はまだ行っていない。実行手順は
`docs/scope-enforcement-cutover.md` にある。
