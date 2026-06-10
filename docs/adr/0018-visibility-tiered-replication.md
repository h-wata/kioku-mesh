# ADR-0018: Observation visibility tier による selective replication

- Status: Proposed
- Date: 2026-06-06
- Supersedes: なし
- Related: ADR-0001, ADR-0006, ADR-0007, ADR-0010, ADR-0014

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
- 個人開発 project の observation は同じ local hub には残したいが、team 全体へは
  replicate したくない。
- E2E 暗号化はまだ無いので、Zenoh に put した時点で hub / router / storage owner
  への信頼が必要になる。

ADR-0010 は Zenoh storage を source of truth、SQLite local index を派生 cache と
定義した。しかし、local-only の observation を扱うには「Zenoh に載せない永続状態」
を明示的に設計する必要がある。

## Decision

Observation に `visibility` を導入し、replication scope を key prefix で分ける。

```text
mem/pub/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
mem/pub/tomb/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}

mem/team/{team_id}/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
mem/team/{team_id}/tomb/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
```

`priv` observation は Zenoh key を持たず、Zenoh `put` / `delete` を呼ばない。
local SQLite backend にのみ保存する。

```text
visibility = pub
  source of truth: Zenoh storage under mem/pub/**
  replication: public storage を持つ peer へ複製

visibility = team
  source of truth: Zenoh storage under mem/team/{team_id}/**
  replication: 該当 team storage を設定した peer へ複製

visibility = priv
  source of truth: local SQLite
  replication: なし
```

ADR-0010 の「Zenoh が source of truth」は `pub` / `team` visibility に適用する。
`priv` は明示的な例外であり、local backend と同じく単一 host の local SQLite を
source of truth とする。

### Isolation model

この ADR で採用するのは **Soft isolation** である。

Soft isolation は「誤って同期しない」ための分離であり、confidentiality boundary
ではない。ACL なしの Zenoh mesh では、network 上到達できる peer が
`session.get("mem/team/{team_id}/**")` を直接実行すれば data を読める可能性がある。

したがって:

- `priv` は Zenoh に載せないため、Zenoh hub / router への流出を根本的に避ける。
- `team` は storage / replication 設定で同期範囲を狭めるが、秘密境界ではない。
- 機密性を要求する team sharing では、将来 ADR-0014 の mTLS に加えて Zenoh ACL を
  導入し、cert subject / ACL principal に権限を結びつける必要がある。

`team_id` は namespace slug であり、security principal ではない。将来 Hard
isolation を導入する場合も、`team_id` だけで権限を判断してはならない。

### Zenoh storage configuration

Peer は参加する scope だけ storage を持つ。

```json5
storages: {
  public_store: {
    key_expr: "mem/pub/**",
    strip_prefix: "mem/pub",
    replication: { /* same across public peers */ },
    volume: { id: "rocksdb", dir: "public" },
  },
  team_kioku_mesh_store: {
    key_expr: "mem/team/kioku-mesh/**",
    strip_prefix: "mem/team/kioku-mesh",
    replication: { /* same across this team */ },
    volume: { id: "rocksdb", dir: "team_kioku_mesh" },
  },
}
```

Hub は必要な `mem/pub/**` と `mem/team/{team_id}/**` storage だけを持つ。
全 team を集約する管理 hub を除き、`mem/team/**` の広い wildcard storage は避ける。
Team に参加しない peer は該当 team storage block を書かない。

### API and search behavior

`save_observation` / CLI save に visibility 指定を追加する。

```text
visibility: pub | priv | team
team_id: optional, required when visibility == team
```

User-facing shorthand として `team/kioku-mesh` のような表記を受け付けてもよいが、
内部 model では `visibility = "team"` と `team_id = "kioku-mesh"` に正規化する。

Search の default scope は `local-visible` とする。

```text
local-visible = priv + pub + joined teams
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

既存 legacy data を自動的に `pub` 扱いにしない。現行 layout は全体共有だったが、
ユーザー意図として private な observation が混ざっている可能性があるため、
migration target は明示指定にする。

```text
kioku-mesh migrate-visibility --from legacy --to priv
kioku-mesh migrate-visibility --from legacy --to pub
kioku-mesh migrate-visibility --from legacy --to team/kioku-mesh
```

## Consequences

- 良い点: 個人 local-only、全体共有、team 共有を同じ mesh 上で扱える。
- 良い点: `priv` は Zenoh に載せないため、E2E 暗号化が無い現状でも hub に保持されない。
- 良い点: `pub` / `team` は Zenoh key prefix と storage config の組み合わせで
  replication scope を制御できる。
- 良い点: 将来 Hard isolation を導入するとき、`team_id` namespace と ACL principal を
  別概念として扱える。
- 悪い点: `Observation.key_expr`、tombstone key、subscriber、rebuild、fallback scan、
  GC/delete、LocalIndex schema、search filter の広い範囲が visibility-aware になる。
- 悪い点: RocksDB directory が scope ごとに分かれ、disk 管理と config 管理が複雑になる。
- 悪い点: Soft isolation は漏洩防止ではない。ACL なしの shared network では team data を
  秘密として扱えない。
- 悪い点: `priv` は local SQLite が source of truth になるため、host loss / disk loss に弱い。
  backup は Zenoh replication ではなく local backup の責務になる。

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
