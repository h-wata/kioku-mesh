# ADR 0023: messaging は別リポジトリに分けず、単一リポジトリ内で core / memory / messaging に層化する

- **Status**: Accepted
- **Date**: 2026-06-23
- **Supersedes**: なし
- **Related**: [[0022-zenoh-agent-messaging-flow-layer]], [[0001-zenoh-rocksdb-transport]], [[0004-identity-env-and-persistent-file]], [[0014-mtls-via-csr-private-ca]], [[0017-dual-hub-spoke-topology]], [[0019-visibility-tiered-replication]]

## Context

ADR-0022 で、長期記憶（ストック）と並立する **メッセージング（フロー）レイヤー** を Zenoh pub/sub 上に新設すると決めた。これを受けて「この機能は kioku-mesh に混ぜてよいのか、別リポジトリに分けるべきか」という構成上の問いが生じた。

懸念の正体は **「記憶（memory）のクリーンなスコープに messaging を溶かし込みたくない」** という関心の分離欲求であり、これ自体は妥当。一方で技術的な重力は共有基盤にある:

- messaging は Zenoh セッション・hub-spoke トポロジ（ADR-0001 / 0017）、mTLS・CA エンロールメント（ADR-0014 / 0015）、identity 解決（ADR-0004）、visibility tier（ADR-0019）を **memory とまるごと共有** する。
- ADR-0022 の核である「会話フローを任意で記憶へ昇格する」ブリッジは、両者が **同じ identity・同じ Zenoh セッション** を共有してこそ安く実装できる。

別リポジトリに分けると、この共有基盤を (a) 重複実装するか、(b) 第3の共有コアライブラリを切り出して両者が依存するか、の二択になる。(b) が理想形だが、現規模で3リポジトリ分割は重く、設計コストの相当部分が「リポジトリ境界の管理」に食われる。

## Decision

**当面リポジトリは分けない。** kioku-mesh 単一リポジトリ内で、論理的な層を切る:

```
kioku-mesh/
  core/        # Zenoh セッション・mTLS・identity・presence（共有基盤）
  memory/      # 記憶（ストック）— 既存
  messaging/   # 会話（フロー）— ADR-0022 で新設
  bridge/      # messaging → memory 昇格のみを担う薄い接着剤
```

- `memory` と `messaging` は **`core` の2人の消費者** とし、互いに直接依存させない。
- 物理的には「同居（co-locate）」させるが、論理的には分離した状態を作る。「混ぜる」のではない。
- **リポジトリ分割の判断は将来に保留** する。分割を正当化する唯一のシグナルは **「memory 抜きで messaging だけを欲しいユーザーが現れる」** こと。それまでは単一リポジトリを維持する。
- **名前（"kioku" = 記憶）の再ポジションは後回し** とする。messaging が co-equal になると名前が実態を過小評価しうるが、リネームは安く、アーキの分割は高いので、名前整理を分割判断の理由にはしない。

## Consequences

- **良い点**:
  - 共有基盤（Zenoh / mTLS / identity / presence）を重複実装せずに済む。
  - 「会話フロー → 記憶昇格」のブリッジが同一プロセス内・同一 identity で安く書ける。
  - `core` を切り出しておくことで、将来リポジトリ分割が必要になっても **`core` をライブラリ昇格するだけ** で済み、分割コストが低い「縫い目」を今のうちに用意できる。
  - デプロイ単位が1つ（ホストあたり1デーモンが複製と配送の両方を担う）。
- **悪い点 / トレードオフ**:
  - リポジトリの関心が「記憶」だけでなくなり、外形上のスコープが広がる（名前との齟齬が生じうる）。
  - `core` / `memory` / `messaging` の層境界を規律として守り続ける必要がある（横着すると memory と messaging が直接結合しうる）。
  - messaging のセキュリティ表面（tmux send-keys = RCE 相当、ADR-0022）が memory コアと同じリポジトリに同居するため、信頼境界の設計を `core` レベルで一貫させる責任が増す。
