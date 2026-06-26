# ADR-0019: Observation visibility tier による selective replication

- Status: Accepted
- Date: 2026-06-06 (Revised: 2026-06-12)
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
