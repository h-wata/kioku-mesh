# Messaging ack timeout / 再送ポリシー設計提案 — Issue #201 (Phase 1.5)

Date: 2026-08-11
Author: W1 / Claude
Status: Proposal（未 accept。accept 後に ADR 化する想定 — 「ADR 化の判断」節参照）

## 概要

Issue #201 は `docs/design/0185-messaging-mvp-design.md` の「未解決事項」から分離された設計課題である。
MVP (#185) は「sender retry は best-effort、`requires_ack=true` でも ack が無いことを自動再送の条件に
しない」と明記して Phase 1.5 送りにした。本メモはその Phase 1.5 のポリシーを決めるための提案である。

結論を先に書く。**「ack が無いから自動再送する」は、storage-backed spool を前提とする現行設計では
ほぼ無意味であり、採用しない。** 同じ `msg_id` の再 put は同じ key への上書きで、受信されていない
原因が「receiver が読みに来ていない」である限り状況を一切変えないからである。Phase 1.5 で作るべきは
再送ループではなく、**(1) sender 側の durable outbox + transport-level retry と (2) ack を実際に読む
delivery receipt 経路**の 2 つである。詳細は「推奨案」節に書く。

## 1. 現状の挙動（コード上の事実）

推測ではなくコードで確認した事実のみを書く。行番号は本メモ作成時点 (`10a2ed4`) のもの。

### 1.1 ack timeout は存在しない

`src/kioku_mesh/messaging/` 配下に ack の期限を表す変数・設定・比較は一切無い。
timeout という語が出るのは Zenoh get の transport timeout (`mcp_server.py:907`, `purge.py:80` の
`timeout=3.0`) と tmux subprocess の `timeout=5` (`tmux_adapter.py:31,39`) のみで、いずれも
ack とは無関係である。

### 1.2 ack key に読み手が居ない

ack key は `keyspace.ack_key()` (`messaging/keyspace.py:54-56`) で
`msg/{scope}/ack/{msg_id}/{recipient_session_id}` として組み立てられる。
書き込みは 2 箇所:

- `mcp_server.ack_message()` → `mcp_server.py:1048-1052`
- `ZenohBridge.put_ack()` → `messaging/zenoh_bridge.py:95-103`

**この key を get / subscribe しているコードは `src/` 内に存在しない。**
したがって sender は現在、ack を観測する手段を持たない。ack key は write-only の痕跡でしかない。

### 1.3 `requires_ack` はどこからも読まれない

`Message.requires_ack` は `messaging/models.py:76` で定義され、`models.py:97` で JSON に載る。
**読み出し側は `src/` にも `tests/` にも無い。** 送信も受信も、このフラグで挙動を変えない。

さらに default 値が **`False`** (`models.py:76`) であり、設計 memo の
「`requires_ack` | bool | yes | MVP は default `true`」(`0185-messaging-mvp-design.md:153`) と食い違う。

### 1.4 sender 側の再送機構が無い

- `messaging/spool.py:63-69` の `send_message()` は in-process dict へ `put` するだけで、
  Zenoh にも触らず、失敗の概念自体が無い。
- `ZenohBridge.put_message()` (`messaging/zenoh_bridge.py:53-87`) は素の `self._session.put()`
  (`zenoh_bridge.py:86`) を 1 回呼ぶだけ。例外は呼び出し元へ素通しされ、キューにも積まれない。
- memory 層が使う `core.transport.with_retry` (`memory/purge.py:71` 等) は
  **`messaging/` 配下では一度も使われていない**。
- memory 層の `memory/pending_queue.py` 相当の messaging 版は存在しない。設計 memo が Phase 1 実装項目
  として挙げた「pending sender retry ... modeled after memory pending queue but separate DB」
  (`0185-messaging-mvp-design.md:522`) は未実装のままである。

messaging 層に唯一存在する retry は tmux 注入の 0.5 秒後 1 回リトライ→drop
(`messaging/tmux_adapter.py:98-111`) だが、これは local delivery adapter の注入リトライであって
message レベルの再送ではなく、注入成功は ack ではないと明記されている (`tmux_adapter.py:60-62`)。

### 1.5 そもそも production の送信経路が無い

MCP tool は `save_observation` / `search_memory` / `get_memory` / `recall_context` / `delete_memory` /
`get_memory_status` / `drain_pending_puts` / `check_messages` / `ack_message` /
`purge_expired_messages` の 10 個で、**送信 tool は無い**（`mcp_server.py` の `@mcp.tool()` 一覧）。
`__main__.py` にも messaging サブコマンドは無い（`grep -n messaging src/kioku_mesh/__main__.py` は 0 件）。
`ZenohBridge` の呼び出し元も `tests/test_messaging_zenoh_bridge.py` のみである。

つまり現在「ack を待つ sender」は production 上に存在しない。これは Phase 1.5 のスコープに
直接効く（「6. Phase 1.5 スコープ境界」参照）。

### 1.6 sender 側の delivery state ストアが無い

`LocalMessageIndex` (`messaging/local_index.py:48-156`) の `messages` 行を挿入するのは
`mcp_server.check_messages()` の `index.register(msg, session_id)` (`mcp_server.py:934`) のみで、
`session_id` は自分自身 (`get_session_id()`, `mcp_server.py:879`)。すなわちこの index は
**受信側専用**であり、送信側の状態は 1 バイトも保存されない。

### 1.7 TTL / expiry は受信側かつ破壊的

- 期限判定は `models.is_expired()` (`models.py:124-140`)。優先順位は
  `expires_at` > `ttl_sec + created_at` > 期限なし。境界演算子は **`now >= exp`** (`models.py:134,139`)。
- `Message` の default は `expires_at=None, ttl_sec=None` (`models.py:65-66`) なので、
  明示指定しない message は**永久に期限切れにならない**。memo の「`ttl_sec` required / default 900」
  (`0185-messaging-mvp-design.md:147`) とはここも食い違う。
- `check_messages` は読み取りついでに期限切れ key を Zenoh から削除する (`mcp_server.py:924-930`)。
- `purge_expired_msgs()` (`messaging/purge.py:47-110`) は `msg/**` 全体を走査して削除する
  (`purge.py:44`, `purge.py:97-101`)。自分宛に限定されない。
- ローカル側 purge は `messages` テーブルのみを削除し (`local_index.py:150-156`)、
  **`acks` テーブルは決して purge されない**。

1.7 の最後の 2 点は、後述する「同一 msg_id 再送の落とし穴」(4.2) の直接の原因である。

### 1.8 ack payload に時刻が無い

`mcp_server.py:1049` が put する ack payload は `{msg_id, recipient_session_id, status}` の 3 field で、
memo の ack schema (`0185-messaging-mvp-design.md:162-171`) にある `acked_at` を含まない。
将来 ack reader を実装しても、Zenoh 上の ack payload だけからは ack 時刻（= 配送レイテンシ）を復元できない。

### 1.9 テストで固定されている挙動

`tests/test_messaging_*.py` が仕様として固定しているのは、key 文字列の形
(`test_messaging_keyspace.py:66-94`)、spool の idempotent put と TTL フィルタ
(`test_messaging_spool.py`)、64 KiB body 上限と put/ack の key・payload 形
(`test_messaging_zenoh_bridge.py:48-158`)、purge の走査/削除挙動 (`test_messaging_purge.py`)、
tmux ガード (`test_messaging_tmux_adapter.py`) である。
**ack timeout・再送・delivery state に関するテストは 1 件も無い**ので、
Phase 1.5 はこの領域について後方互換の制約をほぼ受けない。

## 2. ack timeout の定義

### 選択肢

**A1: `expires_at` をそのまま ack deadline とする**（追加 field なし）

- 長所: schema 追加ゼロ。判定は既存の `is_expired()` (`models.py:124`) をそのまま流用できる。
  「読める期間」と「sender が待つ期間」が一致するので、状態が 1 つの時刻で説明できる。
- 短所: default TTL 15 分 (`0185:198`) を採るなら、sender は 15 分待たないと「未読のまま」を
  知れない。逆に早く知りたくて TTL を縮めると、receiver が受け取れる窓まで縮む。
  2 つの独立した関心事が 1 つのノブに束ねられる。

**A2: `ack_deadline_sec` を message schema に追加する**（推奨）

- 長所: 「いつまで読めるか (`ttl_sec`)」と「いつまでに ack が無ければ諦めるか (`ack_deadline_sec`)」を
  分離できる。sender は 60 秒で「未読」を検知しつつ、message 自体は 15 分後まで receiver が拾える。
- 長所: 未指定時の default を `ttl_sec` と同じにすれば、挙動は A1 と完全一致する。
  つまり A2 は A1 の上位互換で、移行コストが無い。
- 短所: field が 1 つ増える。不変条件 `ack_deadline_sec <= ttl_sec` を検証する責務が増える。

**A3: sender プロセスのローカル設定だけで持つ**（wire に載せない）

- 長所: schema 変更ゼロ。
- 短所: deadline が message に紐付かないため、同じ message を別プロセス / 再起動後に評価すると
  結論が変わりうる。observability ツールや receiver 側から deadline を説明できない。

### 決定: A2 を採用（A1 を default 挙動として内包）

- `Message` に `ack_deadline_sec: int | None = None` を追加する。
- 実効 deadline は `ack_deadline_at = created_at + ack_deadline_sec`、
  未指定なら `ack_deadline_at = expires_at`、`expires_at` も無ければ **deadline 無し**（現状維持）。
- 不変条件: `ack_deadline_at <= expires_at`。違反する送信要求は `ValueError` で拒否する
  （`put_message` が body size 超過を拒否するのと同じ扱い、`zenoh_bridge.py:66-69`）。
- **計時源**: `ack_deadline_at` / `expires_at` は wall clock UTC ISO 8601 で持つ。
  ホストを跨いで同じ文字列を比較する必要があるためで、`expires_at` の既存扱い
  (`local_index.py:22-26`, `models.py:24-28`) を踏襲する。
  一方、後述する retry backoff の**プロセス内スケジューリングだけは monotonic clock** を使う
  （NTP 補正で backoff が飛ぶのを防ぐため）。永続化する `next_attempt_at` は wall clock で書く。
- **境界演算子**: timeout 判定は `now >= ack_deadline_at`。
  既存の `is_expired()` の `now >= exp` (`models.py:134`) と揃え、文書全体で `>=` に統一する。

A3 を却下する理由は、`msg_id` が唯一の同一性の根拠である以上、その message に関する判断根拠は
message 自身に載っているべきだからである。A1 単独を却下する理由は、TTL を配送窓と feedback 遅延の
両方に使い回すと運用上どちらかを必ず妥協することになるため。

## 3. 再送回数・バックオフ方針

ここが本メモの中心である。**「再送」を 1 つの概念として扱ってはいけない。** 現行実装では失敗の
性質が 2 種類あり、有効な対処が正反対になる。

| 失敗の種類 | 何が起きたか | 同じ key への再 put は有効か |
|---|---|---|
| **transport 失敗** | `session.put()` が例外を返し、Zenoh に message が載っていない | **有効**。載るまで再試行する意味がある |
| **ack 不着** | put は成功し、message は Zenoh storage に載っている。receiver が読みに来ないか、読んで ack しない | **無効**。同じ key に同じ payload を上書きしても storage の内容は変わらない |

### 選択肢

**B1: ack 不着を条件に同一 `msg_id` を自動再 put する**

- 長所: 実装が最も単純（タイマーで再 put するだけ）。
- 短所: 上表のとおり **storage の状態を一切変えない**。receiver が offline なら message は既に
  そこにあり、online になれば `check_messages` (`mcp_server.py:900-937`) が拾う。再 put は Zenoh に
  無駄な書き込みを流すだけである。**却下。**

**B2: ack 不着を条件に新しい `msg_id` で再送する**（memo `0185:229` が示唆した方向）

- 長所: 「TTL 切れ即破棄。必要なら sender は新しい `msg_id` で再送する」という MVP 方針に沿う。
- 短所: receiver 側の dedup は `msg_id` 基準（`local_index.py:37` の PK `(msg_id, recipient_session_id)`、
  `spool.py:28-31` の idempotent put）なので、新 `msg_id` は**別メッセージとして必ず二重表示される**。
  自動でこれをやると、offline から復帰した receiver が同一内容を N 通受け取る。
  **自動実行としては却下**。人間 / エージェントが明示的に再送する経路としては残す。

**B3: transport 失敗にのみ retry を効かせ、ack 不着は「観測して報告する」**（推奨）

- 長所: 有効な対処にだけコストを払う。ack 不着に対して sender が本当に欲しいのは再送ではなく
  「未読のまま期限切れになった」という事実の通知であり、これは Issue #201 の検討項目
  「delivery receipt を UX として出すか」そのものである。
- 長所: transport retry は memory 層に既存資産がある（`core.transport.with_retry`、
  `memory/pending_queue.py`）ので、思想を流用できる。
- 短所: 「再送ポリシー」という Issue タイトルに対する答えが「自動再送はしない」になるため、
  期待とのギャップを文書で明示する必要がある（本メモがその役割を負う）。

### 決定: B3 を採用

**transport retry のパラメータ:**

- 対象は retryable な Zenoh 例外のみ。非 retryable は即 `failed` に落とす
  （分類は `core.transport` の既存 `_RETRYABLE_EXC` 相当を再利用する）。
- backoff は指数: 1s, 2s, 4s, 8s, 16s, 32s。上限 60s、**最大 6 回**。
- 各遅延に **±20% の jitter** を掛ける（複数 worker が同時に切断復帰したときの同期を崩すため）。
- **打ち切り条件（優先順位つき）**:
  1. `now >= expires_at` → 以後の試行を行わず `expired` にする。**この条件が retry 回数より優先する**
     （期限切れ message を storage に載せても誰も読めないため）。
  2. 試行回数が 6 回に達した → `failed`。
  3. 非 retryable 例外 → 即 `failed`。
- backoff の待ち自体にも上限を持たせる: 個々の `session.put()` 呼び出しは Zenoh 側の既定 timeout に
  委ねるが、outbox の 1 メッセージが `queued` に留まれる上限は `expires_at` である（無限待ちは無い）。

**ack 観測（timeout 検知）のパラメータ:**

- ack reader は `msg/{scope}/ack/{msg_id}/**` を **get** する（`timeout=3.0` 秒。
  `mcp_server.py:907` / `purge.py:80` と同じ値に揃える）。subscribe ではなく get を採るのは、
  sender プロセスが常駐でない（MCP server は request 駆動）ためである。
- 観測タイミングは (a) sender が明示的に状態照会したとき、(b) outbox の drain 実行時 の 2 つ。
  常駐ポーリングスレッドは持たない（`purge.py:15-18` が periodic background GC を却下したのと同じ理由 —
  MCP server は他が stateless なのに lifecycle 管理だけ増えるため）。

## 4. 冪等性 / 重複配信

### 4.1 現状で効いている冪等性

- **storage レベル**: key に `msg_id` が入る (`keyspace.py:35-51`) ので、同一 `msg_id` の再 put は
  同じ key への上書きになり、message は増えない。
- **spool レベル**: `MessageSpool.put()` は既知 `msg_id` を黙って捨てる (`spool.py:28-31`)。
- **index レベル**: `messages` の PK が `(msg_id, recipient_session_id)` (`local_index.py:37`)、
  `register()` は `IntegrityError` を握って `False` を返す (`local_index.py:84-85`)。
- **表示レベル**: `check_messages` は複数 selector を跨いで `seen_ids` で dedup する
  (`mcp_server.py:916-919`)。

したがって「同じ `msg_id` を何度 put しても receiver には 1 通」は既に成立している。

### 4.2 落とし穴: purge の非対称性（Phase 1.5 で直す）

`LocalMessageIndex.purge_expired()` は `messages` だけを削除し、`acks` を残す
(`local_index.py:150-156`)。この状態で同じ `msg_id` を再 put すると:

1. `register()` は `messages` 行が消えているので新規挿入に成功する (`local_index.py:71-83`)。
2. しかし `check_messages` のフィルタ `index.is_acked(msg.msg_id, session_id)` (`mcp_server.py:951`) は
   生き残った `acks` 行を見て **True** を返す。
3. 結果、再送された message は**エラーも警告も無く receiver に表示されない**。

Phase 1.5 では `purge_expired()` を `messages` と `acks` の両方に効かせる（同一トランザクションで
`DELETE FROM acks WHERE (msg_id, recipient_session_id) IN (削除対象)`）。
併せて「**expiry-purge 済みの `msg_id` を再利用してはならない**」を規約として明文化する。

### 4.3 明示再送のルール

自動再送はしないが、人間 / エージェントが同じ内容を送り直したい場合はある。そのときは:

- **新しい `msg_id` を採番する**（4.2 の罠を踏まないため、および receiver の dedup を尊重するため）。
- 元の message を `correlation_id` (`models.py:78`) で紐付ける。
- 受信側は「同じ `correlation_id` の既読 message がある」ことを表示できるが、
  自動抑制はしない（抑制すると「本当に届いていない」ケースを潰すため）。

## 5. ack を返さない receiver の扱い

ack が来ない原因は少なくとも 3 つあり、sender にとって意味が違う。

| 原因 | presence の見え方 | 妥当な扱い |
|---|---|---|
| receiver session がそもそも存在しない（未起動 / 終了済み） | 対象 `agent_id` の presence key 無し | `no_recipient` — 宛先誤り or タイミングの問題。sender に即時に返す価値が最も高い |
| receiver は生きているが turn 中 / まだ poll していない | presence あり (TTL 90s, `0185:295`) | 通常の待ち。deadline まで待つ |
| receiver は読んだが ack を呼ばなかった | presence あり | 区別不能。`unacked` として扱う |

**決定:**

- deadline 到達時に一度だけ presence を lookup し (`messaging/presence.py`)、
  `no_recipient_present` / `unacked` のどちらであったかを **receipt に記録する**。
  この分類は診断情報であり、再送の可否を分岐させない。
- **送信時点での presence 事前チェックはしない**。presence は宛先解決用の soft state であって
  認可でも到達保証でもない (`0185:251`, `0185:303`)。「presence が無いから送らない」にすると、
  これから起動する agent 宛の spool という MVP の中心価値 (ADR-0022 の「短期 inbox spool」) を壊す。
- **他 adapter への自動エスカレーション（tmux 注入へのフォールバック等）はしない。**
  tmux 注入は remote input injection に相当し既定 off (`0185:399`, `tmux_adapter.py:64-65`)。
  ack 不着という弱い信号でセキュリティ既定値を覆すのは筋が悪い。Phase 1.5 スコープ外とする。

## 6. delivery state machine（最小設計）

sender 側の状態のみを定義する。receiver 側は既存の「登録済み / acked / purged」で変更しない。

```text
              put 失敗(retryable)
        ┌──────────────────────────┐
        v                          │
   [queued] ──put 成功──> [sent] ──┴── ack key 観測 ──> [acked]   (terminal)
        │                    │
        │                    └── now >= ack_deadline_at ──> [timed_out] ──> [expired] (terminal)
        │                                                        │
        │                                     (deadline < expires_at のとき、
        │                                      expires_at までは受信自体は可能)
        │
        └── 非retryable / 6回到達 ──> [failed]   (terminal)
```

| state | 意味 | 遷移条件 |
|---|---|---|
| `queued` | outbox に入ったが Zenoh に載っていない | 初期状態、または retryable put 失敗後 |
| `sent` | `session.put()` が 1 回以上成功した | put 成功 |
| `timed_out` | `now >= ack_deadline_at` かつ ack 未観測。まだ `expires_at` 前なので受信の可能性は残る | deadline 到達 |
| `acked` | `msg/{scope}/ack/{msg_id}/**` に 1 件以上の ack key を観測 | ack 観測（**いつでも**、`timed_out` / `expired` からも遷移可） |
| `expired` | `now >= expires_at` かつ ack 未観測。配送は放棄された。**error ではない** (`0185:229`) | expiry 到達 |
| `failed` | 非 retryable 失敗、または retry 6 回到達。message は Zenoh に載っていない可能性が高い。**error である** | put 失敗の打ち切り |

**同時成立時の優先順位（タイブレーク）** — 複数条件が同時に真になりうるため明示する。
上から順に評価し、最初に真になったものを採る:

1. `acked` — ack を観測したら他の何よりも優先する。`expires_at` を過ぎた後に観測した ack も
   `acked` として扱う（ack は「実際に処理された」という最も強い証拠であるため）。
2. `failed` — 非 retryable 失敗が確定している場合。
3. `expired` — `now >= expires_at`。
4. `timed_out` — `now >= ack_deadline_at`。
5. `sent` / `queued` — put 成功の有無。

**明示的に state に含めないもの:**

- `delivered` / `injected`: receiver が message を読んだこと自体は観測できない
  （ack key しか wire に出ない）。tmux 注入成功はローカルにしか記録されず、publish もされないし
  semantic ack でもない (`tmux_adapter.py:60-62`)。観測できない状態を state machine に置かない。

**永続化先:** `state_dir()/messaging/outbox.db`（`inbox.db` (`mcp_server.py:180`) とは別ファイル）。
inbox と分けるのは、purge の対象・寿命・所有者が違うためである
（inbox は受信した他人の message、outbox は自分が送った message）。

## 7. Phase 1.5 スコープ境界

### 含める

1. **production 送信経路**（前提条件）。現状 send tool も CLI も無い (1.5) ため、
   ack timeout を議論する主体そのものが存在しない。`send_message` の MCP tool 化 or CLI 化が
   Phase 1.5 の最初の作業になる。
2. **sender outbox** (`outbox.db`) と 6 節の delivery state machine。
3. **transport-level retry**（3 節のパラメータ）。`core.transport.with_retry` の思想を messaging に
   持ち込み、`ZenohBridge.put_message` (`zenoh_bridge.py:86`) の裸の put を置き換える。
4. **ack reader**: `msg/{scope}/ack/{msg_id}/**` の get（timeout 3.0s）。
5. **`ack_deadline_sec` field** と不変条件 `ack_deadline_at <= expires_at` の検証。
6. **delivery receipt の提示面**: 状態照会 API（`get_message_status(msg_id)` 相当）。
   `timed_out` / `expired` を返すときに 5 節の分類 (`no_recipient_present` / `unacked`) を添える。
7. **`requires_ack` を実際に読む**。`False` の message は ack 待ちをせず、put 成功で `sent` 終端にする。
   併せて default を memo (`0185:153`) に合わせて `True` にするか、memo 側を実装に合わせるかを
   ここで確定する（本メモの推奨は **実装を memo に合わせて `True` にする**。
   `requires_ack` を送信者が意識せず送った message が receipt 対象外になるのは驚き最小則に反する）。
8. **`acks` テーブルの purge** (4.2) と ack payload への `acked_at` 追加 (1.8)。

### 明示的に含めない

- **ack 不着を条件とする自動再送**（3 節 B1/B2 の却下理由による）。
- **receiver 側 NAK**（`0185:226-227` の MVP 判断を踏襲。missing detection と ordering state が要る）。
- **adapter エスカレーション**（tmux 等への自動フォールバック。5 節の理由による）。
- **broadcast / topic 宛の ack 集約**。ADR-0022 の初期スコープが direct のみであるため。
- **ホストを跨ぐ outbox 同期**。outbox は送信プロセスのローカル状態に留める。
- **#193 の ack 集約 UX**（次節）。

## 8. Issue #193（multi-session ack 集約）との関係整理

#193 は「同じ `agent_id` の複数 session が並行起動しているとき、`inbox/agent/{agent_id}/**` への ack を
どう扱うか」という受信側 UX の課題である。

**重なる部分:** Phase 1.5 の ack reader は `msg/{scope}/ack/{msg_id}/**` を wildcard で読む以上、
「何件の ack が、どの session から来たか」を必然的に手に入れる。したがって sender 側は
「acked と見なす述語」を必ず 1 つ選ばざるを得ない。

**決定（#201 のスコープに含める最小限）:** sender 側の `acked` 判定は
**「1 件以上の ack key が存在すること」**とする。agent 宛 message で 3 session が起動していても、
1 session が ack すれば sender にとっては配送成功である。

- 理由: sender の関心は「誰かが受け取って処理したか」であり、全 session の既読管理ではない。
- 理由: 「全 session の ack を待つ」を選ぶと、いつ起動するか分からない session の数を
  sender が知る必要があり、presence の soft state に配送判定を依存させることになる（5 節で却下した依存）。

**#193 に残す部分:**

- 「誰が読んだか」の可視化 UX。
- 「いずれか 1 session が ack すれば**受信側でも**全 session で既読扱いにする」かどうか
  （現状は session ごとに独立 ack — `local_index.py:37` の PK と `mcp_server.py:1025` の
  `session_id = get_session_id()`）。
- ack 集約レイヤーを bridge / messaging のどちらに置くか。

本メモの決定は sender 側の述語を 1 つ固定するだけで、受信側の集約方針を先取りしない。
#193 がどう決着しても、sender 側の「1 件以上で acked」は成立し続ける。

## 9. 推奨案（1 つに絞った結論）

> **ack 不着を条件とする自動再送は実装しない。** 代わりに Phase 1.5 では
> **sender outbox (`outbox.db`) + transport-level retry（指数 backoff、最大 6 回、`expires_at` で強制打ち切り）
> + ack key reader による delivery receipt** を実装し、ack timeout は
> `ack_deadline_sec`（未指定時は `expires_at` にフォールバック）で定義する。
> `timed_out` / `expired` は sender への**通知**であって再送のトリガーではない。

**採用理由（3 点）:**

1. **有効性**: 同一 key への再 put は storage の内容を変えないため、ack 不着に対する再送は
   物理的に無意味である (3 節)。新 `msg_id` での自動再送は receiver に重複を作る (`local_index.py:37`)。
2. **既存方針との整合**: MVP は「TTL 切れ即破棄は delivery abandoned であり error ではない」
   (`0185:229`) と決めている。receipt はこの方針を変えずに、sender が abandonment を
   **能動的に知れない**という Issue #201 の問題だけを解く。
3. **実装コスト**: transport retry は memory 層の `with_retry` / `pending_queue` の思想を流用でき、
   ack reader は既存の ack key 空間 (`keyspace.py:54`) をそのまま読むだけで、
   **wire format の破壊的変更を伴わない**（追加は `ack_deadline_sec` と ack payload の `acked_at` のみ、
   どちらも `_extras` による forward compat 機構 (`models.py:109-111`) の範囲内）。

**却下した案と却下理由（再掲）:**

| 却下案 | 却下理由 |
|---|---|
| B1: 同一 `msg_id` の自動再 put | storage 状態が変わらず無意味 (3 節の表) |
| B2: 新 `msg_id` の自動再送 | receiver dedup が `msg_id` 基準のため必ず重複表示される |
| A1 単独: `expires_at` を ack deadline に固定 | 配送窓と feedback 遅延が 1 つのノブに束ねられる |
| A3: deadline を sender ローカル設定のみで持つ | 同一 message の判定がプロセス / 再起動で揺れる |
| receiver 側 NAK | missing detection + ordering state が必要。MVP 判断 (`0185:226`) を維持 |
| 送信前 presence チェック | 未起動 agent 宛 spool という ADR-0022 の中心価値を壊す |
| ack 不着時の tmux 自動エスカレーション | 弱い信号でセキュリティ既定値 (`tmux_adapter.py:64-65`) を覆すため |

## 10. ADR 化の判断

本メモは **ADR にしない**。理由:

- 本リポジトリの ADR (`docs/adr/0022`, `0029` 等) は `Status: Accepted` の**確定した意思決定**を記録する
  形式であり、レビュー前の提案を置く場所ではない。
- Issue #201 は設計課題の起票であって、運用者による採否がまだ無い。
- 先例として、messaging MVP そのものも ADR-0022（決定）と `0185-messaging-mvp-design.md`（設計 memo）に
  分離されている。本メモは後者と同じ層に属する。

**ADR 化する条件:** 本提案（特に「自動再送しない」という否定形の決定と、6 節の state machine）が
レビューで accept されたら、`docs/adr/0031-messaging-delivery-receipt-policy.md` として
ADR 化する。ADR-0022 の Related に追加し、本メモを設計根拠として参照する形にする。

## 11. 実装時の注意（Phase 1.5 引継ぎ）

- ADR-0023 の layering を維持する: `messaging` は `memory` を import しない。
  `core.transport` の retry は `core` 経由なので利用可 (`memory/purge.py:53` と同じ形)。
- `outbox.db` は `inbox.db` (`mcp_server.py:180`) と別ファイルにする。
- `purge_expired_messages` (`mcp_server.py:1061`) は `msg/**` 全体を掃く (`purge.py:44`) ので、
  他 agent の未読 message も消える。outbox の state 判定がこの sweep と競合しないよう、
  「key が消えている」ことを `failed` の根拠にしてはならない（`expired` と区別できないため）。
- 1.3 / 1.7 で挙げた memo と実装の食い違い（`requires_ack` の default、`ttl_sec` の必須性）は
  Phase 1.5 で必ずどちらかに寄せる。放置すると receipt の対象範囲が実装依存になる。

## Related

- Issue #201, Issue #185, Issue #193
- ADR-0022 (`docs/adr/0022-zenoh-agent-messaging-flow-layer.md`)
- ADR-0023 (core / memory / messaging layering)
- `docs/design/0185-messaging-mvp-design.md`（「再送戦略」「未解決事項」）
