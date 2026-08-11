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
- `purge_expired_msgs()` (`messaging/purge.py:47-110`) は `msg/**` 全体を**走査**する
  (`purge.py:44`)。自分宛に限定されない。ただし**削除するのは `Message.from_json` に成功し、
  かつ期限切れの entry だけ**で、parse に失敗した entry は「skipping malformed payload」として
  読み飛ばす (`purge.py:83-89`, `purge.py:97-101`)。したがって走査範囲と削除範囲は一致しない
  — この差が ack key の扱いに効く（6.1 (a)）。
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
- 長所: 未指定時の default を実効期限（後述）と同じにすれば、挙動は A1 と完全一致する。
  つまり A2 は A1 の上位互換で、移行コストが無い。
- 短所: field が 1 つ増える。不変条件 `ack_deadline_at <= effective_expires_at` を検証する責務が増える。

**A3: sender プロセスのローカル設定だけで持つ**（wire に載せない）

- 長所: schema 変更ゼロ。
- 短所: deadline が message に紐付かないため、同じ message を別プロセス / 再起動後に評価すると
  結論が変わりうる。observability ツールや receiver 側から deadline を説明できない。

### 決定: A2 を採用（A1 を default 挙動として内包）

- `Message` に `ack_deadline_sec: int | None = None` を追加する。

#### 実効期限 (effective expiry) の定義

`expires_at` と `ttl_sec` のどちらで指定されたかに依らず、**1 つの時刻 `effective_expires_at` に畳む**。
優先順位は既存の `models.is_expired()` (`models.py:124-140`) と同一にする（実装が 2 箇所で別々の
期限概念を持つと、`is_expired()` が真でも deadline 検証は通る、といった食い違いが生じるため）。

| 条件 | `effective_expires_at` |
|---|---|
| `expires_at` あり | `expires_at`（`ttl_sec` が併記されていても `expires_at` が勝つ） |
| `expires_at` 無し・`ttl_sec` あり | `created_at + ttl_sec` |
| どちらも無し | **無期限**（`None`）。拒否はしない — `models.py:65-66` の現行 default をそのまま許容する（**決定事項**。12.3 の旧 U2） |

#### 実効 ack deadline の定義

| 条件 | `ack_deadline_at` |
|---|---|
| `ack_deadline_sec` あり | `created_at + ack_deadline_sec` |
| 未指定・`effective_expires_at` あり | `effective_expires_at`（= A1 と完全に同じ挙動） |
| 未指定・`effective_expires_at` も無し | **deadline 無し**。`timed_out` へは決して遷移しない |

#### 送信時バリデーション

`send` 経路で、Zenoh に put する**前に**評価する（`put_message` が body size 超過を拒否するのと
同じ位置・同じ扱い、`zenoh_bridge.py:66-69`）。違反はいずれも `ValueError` で拒否する。

1. **`ack_deadline_sec` は 1 以上の有限な整数**であること。`0` / 負値 / 非整数 / 非有限値は拒否する。
   `0` を許すと「送信と同時に `timed_out`」という、ack を観測する機会が構造的に存在しない状態を
   作れてしまうため。上限は設けない（`effective_expires_at` 側の不変条件 2. が実質的な上限になる）。
2. `effective_expires_at` が定義されている場合、**`ack_deadline_at <= effective_expires_at`**。
   等号は許す（未指定時の default がちょうど等号になるため）。違反は拒否し、
   **暗黙のクランプはしない** — sender が指定した deadline を黙って書き換えると、
   返す receipt の意味が sender の期待とずれるため。
3. `effective_expires_at` が**無期限**の message に `ack_deadline_sec` を指定することは
   **許可する**。この場合 2. は上限が無いので自動的に成立する。許可する理由は、
   「いつまで読めるか」と「いつまでに feedback が欲しいか」を分離するのが A2 の目的そのもので、
   receipt だけが欲しい sender に TTL 設定を強制すると A1 の短所（2 つの関心事が 1 つのノブに
   束ねられる）が戻ってくるため。この message は `timed_out` には遷移するが
   `expired` には決して遷移しない。

なお `ttl_sec` そのものの範囲（`0185:147` の min 30 / max 86400）と、`ttl_sec` を schema 上
required にするか否かは、本メモの決定事項ではなく 0185 の既存規定に従う（11 節の引継ぎ事項）。
実装は現状 `ttl_sec` を必須にも範囲検証もしていない (1.7)。
**これは「無期限を許すか」とは別の論点である**: 本メモは「両方未指定 = 無期限の message を
拒否しない」ことを決定事項として扱い (上表)、0185 側が `ttl_sec` を required 化した場合は
その枝に到達する message が作られなくなるだけで、本メモの畳み込み規則も state machine も
変わらない。

#### 計時源と境界演算子

- **計時源**: `ack_deadline_at` / `expires_at` は wall clock UTC ISO 8601 で持つ。
  ホストを跨いで同じ文字列を比較する必要があるためで、`expires_at` の既存扱い
  (`local_index.py:22-26`, `models.py:24-28`) を踏襲する。
  一方、後述する retry backoff の**プロセス内スケジューリングだけは monotonic clock** を使う
  （NTP 補正で backoff が飛ぶのを防ぐため）。永続化する `next_attempt_at` は wall clock で書く。
- **境界演算子**: 時刻の到達判定はすべて `>=` に統一する
  （`now >= ack_deadline_at`、`now >= effective_expires_at`、`now >= next_attempt_at`）。
  既存の `is_expired()` の `now >= exp` (`models.py:134`) に揃えたもので、文書全体で例外を作らない。
  一方、**`ack_deadline_at <= effective_expires_at` は時刻の到達判定ではなく 2 つの時刻の
  順序に関する不変条件**であり、こちらは等号を含む `<=` を使う（別物なので混同しないこと）。

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
  `spool.py:28-31` の idempotent put）なので、**新 `msg_id` は dedup を素通りする**。
  実際に重複表示になるかは receiver の観測履歴に依存し、2 通りに割れる:
  - 元の message を既に `check_messages` で観測していた receiver → **同一内容が 2 通**見える。
  - 期限切れになるまで一度も poll しなかった offline receiver → resend だけが見える（重複しない）。

  問題は、**sender がどちらになるかを知り得ない**ことである（ack が無いという情報だけでは
  「読まれていない」と「読んだが ack しなかった」を区別できない — 5 節の表）。
  内容の同一性を見る意味的 dedup は receiver 側にも wire 上にも無いため、この
  **回避不能な重複リスク**を receiver 側で吸収することもできない。自動で N 回繰り返せば
  リスクも N 倍になる。**自動実行としては却下**。
  人間 / エージェントが「重複してでも届けたい」と明示的に判断する再送経路としては残す
  （4.3 のルールに従う）。

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

**試行の数え方（用語を先に固定する）:**

「6 回」が initial attempt を含むのか retry だけを指すのかで総試行数も遅延の段数も変わるため、
数え方を先に一つに決める。

- **initial attempt（初回試行）を 1 回目として数える。** 「retry」は 2 回目以降の試行を指す。
  以降、本メモで「N 回」と書いたら常に **initial attempt を含む総試行回数**である。
- 永続化する `attempt_count` は **「これまでに戻り値または例外が確定した `session.put()` 呼び出しの
  回数」**（0 始まり、outbox 行の初期値は `0` = まだ 1 度も put していない `queued` 状態）。
  **各 put 呼び出しが戻った直後に、成功・失敗のどちらでも +1 する。** 次の遅延を計算する前に加算する。
  したがって「今まさに実行している試行の番号」は `attempt_count + 1`（1-indexed）になる。
- **合計 6 回で打ち切る**（initial attempt 1 回 + retry 5 回）。
- 総試行が 6 回なので、attempt 間の遅延は **5 段階**: `1s, 2s, 4s, 8s, 16s`。
  遅延の合計は公称 31 秒（jitter 込みの最悪値 37.2 秒）。

  **実装契約（評価順を手順で固定する）**: 打ち切り判定は「次の試行がいつになるか」を
  参照するため、**判定より前に候補値を計算し終えていなければならない**。1 回の put について
  以下の順序で実行し、この順序から外れた実装を認めない:

  1. `session.put()` の呼び出しが戻る（成功・例外のいずれでも）。
  2. `attempt_count += 1` する（成功・失敗のどちらでも加算する）。
     成功したか例外だったかを `last_put_succeeded`（bool）として保持する。
  3. **`now` を 1 度だけ読む。** ここで読んだ `now` は 4. の打ち切り判定でもそのまま使う
     （判定の途中で時計を読み直さない。読み直すと候補値の計算基準と判定基準がずれる）。
     続いて `candidate_next_attempt_at` を計算するが、**計算するのは
     「put が失敗し、かつ `attempt_count < 6`」のときだけ**である:
     - **put が成功した場合は `candidate_next_attempt_at` を計算しない（未定義とする）。**
       成功した put の後に次の試行は存在しないので、候補時刻という概念自体が存在しない。
       存在しない試行の時刻を期限と比較してはならない。
     - put が失敗し `attempt_count < 6` のとき、公称遅延 `2^(attempt_count - 1)` 秒に
       jitter を掛けた実効遅延を求め、`candidate_next_attempt_at = now + 実効遅延`
       （wall clock）とする。**jitter はこの時点で 1 度だけ引き、以降の判定と永続化で
       同じ値を使う**（判定用と永続化用で別々に引くと、判定した時刻と実際に待つ時刻がずれる）。
     - put が失敗し `attempt_count >= 6`（次の試行が存在しない）のときは
       **`candidate_next_attempt_at` は未定義**とする。

     いずれの未定義ケースでも、後述の打ち切り条件 2. の「次の試行予定時刻」節は評価しない。
  4. **打ち切り判定を 1 回だけ実行する**（後述）。条件 2. の期限比較には、
     行に永続化されている `next_attempt_at`（= 前回値）ではなく、
     **必ず 3. で計算した `candidate_next_attempt_at` を使う**。
  5. 続行が確定した場合に**限り**、`candidate_next_attempt_at` を
     `next_attempt_at` として outbox 行に永続化する。打ち切った場合と put が成功した場合は
     `next_attempt_at` を書き換えない（`NULL` のまま、または前回値のまま残す。
     打ち切った場合は `termination_reason` が、put 成功の場合は `last_put_succeeded` が
     記録済みなので、6.4 の reducer はどちらの場合も `next_attempt_at` を読まない）。

  `attempt_count` は完了回数（初期値 0）であって「今の試行番号」ではない、という点だけ
  取り違えないこと（今まさに実行している試行の番号は `attempt_count + 1`）。

  | 試行番号 (`attempt_count + 1`) | 直前に待つ遅延 | 待ち終えた時点の累積経過（公称） |
  |---|---|---|
  | 1（initial attempt） | なし | 0s |
  | 2（retry 1） | 1s | 1s |
  | 3（retry 2） | 2s | 3s |
  | 4（retry 3） | 4s | 7s |
  | 5（retry 4） | 8s | 15s |
  | 6（retry 5、最後） | 16s | 31s |

- **6 回・5 段階を選んだ根拠**: (i) 遅延合計 31 秒は既定 TTL 900 秒 (`0185:147`) の約 3.5% で、
  期限内に十分収まる（retry が TTL を食い潰して受信窓を削る事態を避ける）。
  (ii) Zenoh セッションの再接続は秒オーダーで完了するため、最終遅延 16 秒を超えても回復しない
  障害は「一時的」とは見なさない、という線引き。(iii) memory 層の `core.transport.with_retry` と
  同じ指数 backoff の形を保ったまま、段階数だけを messaging の用途（対話的な依頼の配送。
  分単位で待たせる価値が無い）に合わせて短くした。
  **6 回・5 段階・遅延列 `1,2,4,8,16s` は初期実装値として決定済み**である。
  運用計測の結果として値を見直せるチューニングパラメータでもあるが、
  「後で変えられる」ことは「まだ決まっていない」ことを意味しない — 実装はこの値で書く
  (12.2)。設計上の不変条件は「上限が有限であること」だけである。
- 遅延そのものに対する別途の上限（「最大 60 秒」等のクランプ）は**設けない**。
  遅延列は `16s` で終わる有限列なのでクランプが働く余地が無く、上限秒を書くと
  7 回目以降の試行が存在するかのように読めてしまうため。
- 各遅延に **±20% の jitter** を掛ける（複数 worker が同時に切断復帰したときの同期を崩すため）。
  実効遅延 = 公称遅延 × `uniform(0.8, 1.2)`。**jitter は打ち切り条件の構造を変えない**
  （打ち切り条件の集合は回数と `effective_expires_at` だけで決まり、jitter が新しい条件を
  足すことはない）。ただし jitter 済みの `candidate_next_attempt_at` が条件 2. の比較対象なので、
  **put が失敗したときに限り、期限直前では jitter の引き値によって「あと 1 回試すか、
  ここで `expired` にするか」が分岐しうる**。これは意図した挙動である（期限を跨ぐ待ちを
  挟まないことが条件 2. の目的で、跨ぐか否かは実際に待つ長さで判定するのが正しい）。
  上の実装契約 3. で jitter を 1 度だけ引き、判定と永続化で同じ値を使うと定めているのは、
  この分岐を決定論的にするためである。**put が成功した場合は jitter を引かない**
  （候補時刻を計算しないため）ので、成功経路の結果が jitter で揺れることはない。
- **計時源**: 待ちのスケジューリングは monotonic clock、永続化する `next_attempt_at` は wall clock
  （2 節「計時源」と同じ理由）。プロセスが再起動した場合は `now >= next_attempt_at` で再開判定する。
- **打ち切り判定**は、各 put が戻って `attempt_count` を加算し、
  `candidate_next_attempt_at` を計算し終えた直後に 1 回だけ実行する（上の実装契約 4.）。
  **`now` は実装契約 3. で読んだ値をそのまま使い、以下のすべての条件で同じ値を使う**
  （条件ごとに時計を読み直さない。6.4 の reducer と同じ理由）。上から評価し、**最初に真になったものを
  `termination_reason` として outbox 行に 1 つだけ記録する**
  （複数は記録しない。これが state の一意性の根拠になる）:
  1. **`last_put_succeeded` が真（今回の put が成功した）→ 終了理由は記録せず `sent` へ。**
     以降の条件は評価しない。**この条件が expiry より先に評価される**のは、
     成功した put は「配送を放棄した」ことではないからである。put が成功した時点で
     message は storage に載っており、`expired`（6.3 の定義: 配送は放棄された）を
     記録するのは事実に反する。期限に達した後にその行をどう報告するかは
     6.4 の reducer が `now >= effective_expires_at` から導出する（`requires_ack = true` の
     場合のみ。`requires_ack = false` の `sent` は quiescent なので遷移しない、6.1 / 6.2）。
     なお実装契約 3. により、put 成功時は `candidate_next_attempt_at` が
     そもそも未定義なので、2. の 2 つ目の節は評価対象にならない。
  2. `effective_expires_at` が定義されていて、次のいずれかが成り立つ
     → `termination_reason = expired`。
     - `now >= effective_expires_at`（既に期限に達している）
     - `candidate_next_attempt_at` が定義されていて
       `candidate_next_attempt_at >= effective_expires_at`
       （次の試行が期限以降になる。**比較するのは今回計算した候補値であって、
       行に残っている前回の `next_attempt_at` ではない**）

     **この条件が回数上限より優先する**（期限切れ message を storage に載せても誰も読めないため。
     また、結果が確定している待ちを挟まないため）。**ただし 1. には劣後する** — ここに
     到達している時点で今回の put は失敗している。
  3. 直前の例外が非 retryable → `termination_reason = nonretryable`。
  4. `attempt_count >= 6` → `termination_reason = attempts_exhausted`
     （このとき `candidate_next_attempt_at` は未定義なので、2. の 2 つ目の節は評価されていない）。
  5. いずれでもない → 終了理由を記録せず、`candidate_next_attempt_at` を
     `next_attempt_at` として永続化し（実装契約 5.）、その時刻まで待って次の試行を実行する。
- **1 回の put で複数条件が同時に真になっても、記録されるのは最初の 1 つだけ**である。
  例: 「6 回目の put が失敗し、その put の実行中に `effective_expires_at` を跨いだ」場合は
  2. が先に真になるので `expired` が記録され、`attempts_exhausted` は記録されない。
  この一意性が無いと、同じ行を `expired` とも `failed` とも読める状態が生まれる。
- **put 成功と expiry が同時に成り立つ場合も同様に 1 つだけ**である。
  例: 「`effective_expires_at` の 0.5 秒前に put が成功した」場合、1. で `sent` が確定し、
  `termination_reason` は記録されない。次の試行が存在しない以上
  「次の試行が期限以降になる」という比較には意味が無いためである。この行は
  `requires_ack = false` なら `sent` のまま quiescent、`requires_ack = true` なら
  6.4 の reducer が期限到達後に `expired` を導出する。
- **この評価順は 6.4 の state 判定順とは意図的に異なる。** ここは「次に何をするか」を決める順序で、
  失敗が続いているときに期限切れ後の無駄な put を積まないために expiry を早い段階で見る。
  6.4 は「記録済みの事実にどの名前を付けるか」を決める順序である。
  **両者が食い違わないのは、6.4 が生の `attempt_count` や
  `now` ではなく、ここで記録した `termination_reason` を読むから**である。
  たとえば `candidate_next_attempt_at >= effective_expires_at` で打ち切った行は、まだ
  `now < effective_expires_at` の時点でも `termination_reason = expired` を根拠に `expired` と
  報告される（`now` を見て `queued` に戻ることはない）。
  なお put 成功を最優先に置いたことで、**この評価順と 6.4 の評価順は「成功した配送を
  失敗として記録しない」という点では一致する**（6.4 も条件 1. で `acked` を、
  `requires_ack = false` では条件 2. の `termination_reason` を見るが、成功した put の行には
  `termination_reason` が無いので `sent` に落ち着く）。
- `effective_expires_at` が無期限の message では条件 2. が決して真にならず、打ち切りは回数上限
  (4.) だけで決まる。**待ちが無限になる経路は存在しない**（最長でも 6 回・公称 31 秒で確定する）。
- 個々の `session.put()` 呼び出し自体の時間上限は Zenoh 側の既定 timeout に委ねる。

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

### 6.1 terminal の定義

用語を先に固定する。曖昧さの元は「terminal」を「もう送信側が何もしない」の意味と
「もう二度と state が変わらない」の意味で混用することにある。本メモは**後者だけ**を terminal と呼ぶ。

- **terminal（終端）= 出ていく遷移が 1 本も無い state。** `acked` **のみ**。
- **quiescent（静止）= sender 側の能動的な作業（put / 待ち）はもう無い state。**
  quiescent は `(state, requires_ack)` の組に対して定まる分類であって state 単独の属性ではない
  ため、集合として次のように定義する:
  - `timed_out` / `expired` / `failed` — `requires_ack` の値に依らず quiescent。
    ack を観測すればまだ `acked` へ遷移しうる。
  - **`requires_ack = false` の `sent`** — quiescent。put が成功した時点で sender の作業は
    終わっており、ack も deadline も見ない以上、ここから出ていく遷移は 1 本も無い
    （6.2 の注記、6.3 の表、6.4 の reducer と同じ扱い）。**遷移が無いという意味では
    `acked` と同じだが terminal とは呼ばない。** terminal は「配送が確認できた終わり方」を
    指す語として `acked` に予約しており、`requires_ack = false` の `sent` は
    「確認しないと決めた終わり方」で意味が違うためである。
  - `requires_ack = true` の `sent` は quiescent では**ない**（ack 観測待ちが残っている）。
- 以降、本メモで「quiescent の 3 state」のような state だけの数え方はしない。
  quiescent か否かを判定するときは必ず `requires_ack` と併せて見ること。

**決定: `expired` は terminal ではなく quiescent とする。**
理由は、ack reader が状態照会時にしか動かない（3 節）以上、「`expires_at` を過ぎた後に初めて
ack key を観測する」は正常系として起こるためである。ここで `expired` を terminal にすると、
ack key が目の前にあるのに `expired`（= 未配送）と報告することになり、receipt が事実に反する。
逆に `acked` は 4 つの中で唯一「receiver が実際に処理した」という直接証拠を持つので、
後から確定しても上書きしてよい。

**決定: `failed` も terminal ではなく quiescent とする。**
put の失敗は「client 側がそう観測した」という事実でしかなく、`session.put()` が例外を返しても
storage には載っていた、という曖昧な失敗 (ambiguous failure) が起こりうる。その場合 receiver は
普通に message を読んで ack する。したがって `failed` から `acked` への遷移は実際に発生しうるので、
図と表の両方でこれを許す。**terminal は `acked` ただ 1 つ**であり、
「もう sender は何もしない」を意味するのは quiescent の方である。

#### late ack を観測できる窓（2 つの独立した窓を分けて定義する）

late ack（`timed_out` / `expired` / `failed` から `acked` への遷移。`requires_ack = false` の
`sent` は ack を見ないのでここには含まれない）が実際に起こるには、**ack key が Zenoh 上に残っている**
ことと、**その ack を突き合わせる outbox 行が残っている**ことの両方が必要である。この 2 つは
別々の機構で消えるので、別々の窓として定義する。**片方をもう片方の根拠にしてはならない。**

**(a) ack-key retention — ack key 自体が Zenoh 上に残る期間**

ここは実装を読んで確定させた。結論は **「ack key の retention は、そもそも `msg/**` を持つ
storage が構成されているかどうかで 2 通りに割れ、どちらの場合も現行実装に ack key を
削除する経路は無い」** である。以下の 2 点はいずれも実測で確認した（TASK-344）。

- **(a-1) 現行の同梱 config には `msg/**` を受け持つ storage が無い。**
  `config/zenohd_home.json5:40` / `zenohd_office.json5:40` / `zenohd.docker.json5:48` /
  `zenohd_repro_{a,b}.json5:32`、および `tests/conftest.py:108` の storage はすべて
  `key_expr: "mem/**"` だけを宣言している。`msg/**` を宣言した storage はリポジトリ内に存在しない。
  同梱 config と同じ storage 構成（`mem/**` のみ）で router を立てて `msg/{scope}/ack/...` を
  put し、**別セッションから get し直すと 0 件**である（`msg/**` での走査も 0 件）。
  `msg/**` を受け持つ storage を足した構成では同じ手順で 1 件返る。
  すなわちこの構成では ack key は購読中の subscriber に配送されるだけで retain されず、
  **後から get する sender には最初から見えない**（retention は実質 0）。
  これは ack key に限らず message body 側も同じなので、store-and-forward を前提にした
  観測（`get_message_status` の late ack 報告を含む）は `msg/**` storage の構成を前提とする。
- **(a-2) `msg/**` storage を構成した場合、ack key は誰にも削除されない。**
  `purge_expired_msgs` は `msg/**` を get したあと各 entry に対して
  `Message.from_json(payload)` を実行し、失敗した entry は
  「skipping malformed payload」として `continue` する (`purge.py:83-89`)。
  ack key の payload は `put_ack` (`zenoh_bridge.py:95-101`) と `mcp_server.py:1172-1175` が書く
  `{"msg_id":..., "recipient_session_id":..., "status":"acknowledged"}` であり、
  `Message` が必須とする `sender_id` / `scope` / `payload` を持たない。実際に
  `Message.from_json` に通すと `TypeError: Message.__init__() missing 3 required positional
  arguments: 'sender_id', 'scope', and 'payload'` になる。したがって
  **ack key は必ず malformed 扱いで skip され、sweep は ack key を 1 件も削除しない**。
  もう一方の delete 経路（`mcp_server.py:1051` の `check_messages` 内 lazy delete）も
  同じ `Message.from_json` の成功を前提とし、かつ selector が inbox 側なので ack key に届かない。
  production 側で `session.delete` を呼ぶ箇所は messaging 層ではこの 2 つだけである。
- **帰結（本メモが依拠してよい事実）**:
  - **「sweep が走れば ack key が消える」は成り立たない。** したがって
    「送信直後に sweep が走れば ack key はすぐ消えるので下限が無い」という以前の説明は誤りである。
  - `msg/**` storage が無い構成では retention は実質 0、ある構成では**無制限**（GC 経路が無い）。
    どちらであっても **ack-key retention は設計上の保証にならない**。「ack key 側が先に閉じる」とも
    「後に閉じる」とも仮定してはならない、という結論だけが (a) から引き出せる。
  - `msg/**` storage を構成した場合、ack key は削除されないまま増え続ける。これは本メモの
    スコープ外だが、Phase 1.5 で ack reader を実装する前提として認識しておく必要がある
    （12.1 の U5 で未決事項として立てている）。

**(b) observable receipt window — `get_message_status` が late ack を報告できる期間**

- 上限は **outbox 行の保持上限（6.4 の 24 時間）** である。行が GC された後の
  `get_message_status(msg_id)` は `unknown` を返す (7 節の項目 6) ので、たとえ ack key が
  Zenoh 上に残っていても late ack は報告されない。
- したがって **現在の実装契約における observable late-ack window は、行が quiescent に入った
  時刻 (`quiesced_at`) から最大 24 時間**である。これは決定事項であり、運用に依存しない。
- 24 時間はあくまで**上限**である。ack key 側が先に閉じれば、実際に観測できる窓はそこで閉じる。
  すなわち **実効窓 = min(24h, ack key が読める期間)** である。(a) で確定させたとおり
  第 2 項は sweep では決まらず、**`msg/**` を受け持つ storage が構成されているか否か**で決まる:
  - `msg/**` storage が無い構成（同梱 config はすべてこれ）: ack key は retain されないので
    第 2 項は実質 0 であり、**実効窓も実質 0**。late ack は原理的に観測できない。
  - `msg/**` storage がある構成: 第 2 項は ∞（削除経路が無い）なので **実効窓は 24h 側で決まる**。
- したがって **下限は保証されない**（構成次第で 0 でありうる）。これは以前の版が書いていた
  「sweep がいつ走るか分からないから下限が無い」とは別の理由である。**上限が 24 時間である
  という決定事項だけは、どちらの構成でも変わらない。**

**設計上の保証はこの 1 文に集約される: late ack が観測される保証は無く、
観測されうる期間の上限だけが 24 時間として決まっている。** ack key が先に消えたか、
そもそも ack が来ていないかを sender が区別する手段は無い（ack key の不在は
`failed` の根拠にしてはならない、という 11 節の注意と同じ理由）。

### 6.2 状態遷移図

```mermaid
stateDiagram-v2
    [*] --> queued: outbox 行を作成

    queued --> queued: put 失敗(retryable) かつ 終了理由が記録されない<br/>(attempt_count += 1, candidate_next_attempt_at を<br/>next_attempt_at として永続化)
    queued --> sent: put 成功【最優先】
    queued --> failed: put 失敗 かつ<br/>termination_reason = nonretryable / attempts_exhausted
    queued --> expired: put 失敗 かつ termination_reason = expired<br/>(now >= effective_expires_at or<br/>candidate_next_attempt_at >= effective_expires_at)

    sent --> acked: ack key を 1 件以上観測
    sent --> timed_out: now >= ack_deadline_at かつ ack 未観測<br/>(ack_deadline_at < effective_expires_at のとき)
    sent --> expired: now >= effective_expires_at かつ ack 未観測<br/>(deadline 無し、または deadline == 期限のとき)

    timed_out --> acked: ack key を 1 件以上観測 (late ack)
    timed_out --> expired: now >= effective_expires_at

    expired --> acked: ack key を 1 件以上観測 (late ack)<br/>※6.1 の観測窓の内側に限る
    failed --> acked: ack key を 1 件以上観測<br/>(ambiguous failure。6.1 参照)

    acked --> [*]

    note right of expired
        quiescent: sender の作業は無いが
        ack 観測でのみ acked へ遷移しうる
    end note
    note right of timed_out
        quiescent（同上）
    end note
    note right of failed
        quiescent（同上）
    end note
    note left of queued
        queued からの 3 本は §3 の打ち切り判定の
        条件順（1. put 成功 → 2. expired →
        3. nonretryable → 4. attempts_exhausted）に従う。
        put が成功した put では他の 2 本は評価されない。
    end note
```

**`queued` から出る 3 本の優先関係**（図だけを見る実装者のために、§3 を読まなくても
一意に決まるようにここに再掲する）: **put が成功したら常に `sent`** であり、
`expired` / `failed` は**その put が失敗した場合にのみ**到達しうる。
`expired` と `failed` が同時に成り立つ場合は `expired` が優先する（§3 の条件 2. が 3./4. より上）。
この優先関係は §3 の打ち切り判定の条件順そのものであり、両者が食い違うことはない。

図に無い遷移は存在しない。特に:

- **retryable な put 失敗は `queued` の自己ループ**である。retry は `sent` に到達する**前**に
  起きる。`sent` は put が成功した後の state なので、**`sent` から `queued` へ戻る遷移は存在しない**
  （retry を `sent -> queued` と描くと、put 成功後にもう一度 put する経路があるように読める）。
- 同じ理由で `sent` から `failed` への遷移も無い（put が成功した以上、transport は成功している）。
- `requires_ack = false` の message は ack 待ちをしない（7 節の項目 7）。この場合
  `sent` が quiescent であり（6.1 の quiescent 定義に含まれる）、
  `timed_out` / `expired` / `acked` へは遷移しない。図中の `sent` から出ている 3 本の辺は
  すべて `requires_ack = true` の場合のものである。

### 6.3 state 一覧

| state | 種別 | 意味 | 入る条件 | 出る先 |
|---|---|---|---|---|
| `queued` | 進行中 | outbox に入ったが Zenoh に載っていない | 初期状態、または retryable put 失敗後 | `queued` / `sent` / `failed` / `expired` |
| `sent` | `requires_ack=true`: 進行中<br/>`requires_ack=false`: **quiescent**（6.1） | `session.put()` が 1 回以上成功した | put 成功 | `requires_ack=true`: `acked` / `timed_out` / `expired`<br/>`requires_ack=false`: — |
| `timed_out` | quiescent | `now >= ack_deadline_at` かつ ack 未観測。まだ `effective_expires_at` 前なので受信の可能性は残る | deadline 到達 | `acked` / `expired` |
| `expired` | quiescent | 配送は放棄された。**error ではない** (`0185:229`) | `termination_reason = expired`、または `now >= effective_expires_at` かつ ack 未観測 | `acked`（late ack のみ） |
| `failed` | quiescent | 非 retryable 失敗、または総試行 6 回到達。message は Zenoh に載っていない可能性が高い。**error である** | `termination_reason` が `nonretryable` / `attempts_exhausted` | `acked`（ambiguous failure のみ、6.1） |
| `acked` | **terminal** | `msg/{scope}/ack/{msg_id}/**` に 1 件以上の ack key を観測 | ack 観測（`sent` / `timed_out` / `expired` / `failed` のいずれからでも） | — |

`no_recipient_present` / `unacked`（5 節）は state ではなく、`timed_out` / `expired` に付随する
**診断ラベル**である。state 機械の遷移には影響しない。

### 6.4 同時成立時の優先順位（タイブレーク）

複数条件が同時に真になりうるため、**記録済みの事実から「今この行を何と呼ぶか」を決める順序**を
明示する。これは outbox 行を入力として state を返す単一の純関数（reducer）として実装する。

**前提: 評価の冒頭で `now` を 1 度だけ読み、以下のすべての条件で同じ値を使う。**
条件ごとに時計を読み直すと、同じ 1 回の照会の中で `now >= ack_deadline_at` と
`now >= effective_expires_at` の判定が別々の時刻に基づいてしまい、境界をまたぐ瞬間に
矛盾した結果を返しうるため。

上から順に評価し、最初に真になったものを採る:

1. `acked` — ack key を 1 件以上観測している。他の何よりも優先する。
   `effective_expires_at` を過ぎた後に観測した ack も、`failed` を記録した後に観測した ack も
   `acked` として扱う（ack は「実際に処理された」という最も強い証拠であるため。6.1 参照）。
2. `termination_reason` が記録されていれば、その写像を採る（`now` は見ない）:
   `nonretryable` / `attempts_exhausted` → `failed`、`expired` → `expired`。
3. `expired` — `effective_expires_at` が定義されていて `now >= effective_expires_at`。
4. `timed_out` — `ack_deadline_at` が定義されていて `now >= ack_deadline_at`。
5. `sent` — 成功した put が 1 回以上ある。
6. `queued` — 上のいずれでもない。

**`requires_ack = false` の行は 1. / 3. / 4. を評価しない**（2. → 5. → 6. の順で評価する）。
ack も deadline も見ないので、put が成功した行は `sent` のまま quiescent になる（6.2 の注記）。

`ack_deadline_at == effective_expires_at`（`ack_deadline_sec` 未指定時の default）のときは
3. が先に真になるので、**`timed_out` は観測されずに直接 `expired` になる**。
6.2 の図で `sent --> expired` を別の辺として描いてあるのはこのケースである。

**この順序は 3 節の「打ち切り判定」の評価順（put 成功 → expiry → 非 retryable → 回数）とは
意図的に異なる。** 3 節は「次に put するか否か」を決める順序、ここは「確定済みの事実に
どの名前を付けるか」を決める順序である。2. が生の `attempt_count` / `now` ではなく
`termination_reason` を読むことで、両者は必ず同じ結論に到達する（3 節末尾参照）。
両者に共通する不変条件は **「成功した put の行に `termination_reason` は記録されない」**
（3 節の条件 1.）であり、これがあるために「put は成功したのに `expired` と報告される」行は
生じない。`requires_ack = true` の行が期限到達後に `expired` と報告されるのは、
ここの 3. が `now` から導出する場合**だけ**である。

**outbox 行の保持上限:** quiescent または terminal に入った行は、その時刻 (`quiesced_at`) から
wall clock で **24 時間**保持し、以後 GC する。GC 後の `get_message_status(msg_id)` は
`unknown` を返す。`requires_ack = false` の行は put 成功時点で quiescent なので (6.1)、
`quiesced_at` はその put が成功した時刻であり、以後 ack を待たずに 24 時間で GC される。

この 24 時間が **observable receipt window の上限そのもの**である（6.1 (b)）。
ack key 側の retention (6.1 (a)) は `msg/**` storage の構成に依存し、無い構成では実質 0、
ある構成では削除経路が無いため無制限であって、いずれにせよ設計上の保証が無いため、
**どちらが先に閉じるかを根拠にはできない**。24 時間はあくまで「outbox 側が単独で保証できる上限」として決めた値で、
根拠は (i) 人間が翌日に「あの依頼どうなったか」を確認できる長さであること、
(ii) quiescent 行が無限に貯まらないこと、の 2 点である。
**初期値は決定事項であり、運用計測後に変更可能なチューニングパラメータでもある**
（12 節「チューニングパラメータ」参照）。設計上の不変条件は「有限の上限が存在すること」だけである。

この上限があるため、**どの state からも「無期限に待ち続ける」経路は存在しない**
（無期限 message の `sent` も、24 時間後に GC されて `unknown` になる）。

**明示的に state に含めないもの:**

- `delivered` / `injected`: receiver が message を読んだこと自体は観測できない
  （ack key しか wire に出ない）。tmux 注入成功はローカルにしか記録されず、publish もされないし
  semantic ack でもない (`tmux_adapter.py:60-62`)。観測できない状態を state machine に置かない。

**outbox 行が持つべき最小の field**（state は保存せず、下記から 6.4 の reducer で導出する）:
`msg_id`、`created_at`、`effective_expires_at`（畳み込み済み、無期限なら NULL）、
`ack_deadline_at`（無ければ NULL）、`attempt_count`（完了した put 回数、初期 0）、
`last_put_succeeded`（bool）、`next_attempt_at`（wall clock、無ければ NULL）、
`termination_reason`（`expired` / `nonretryable` / `attempts_exhausted` のいずれか、未確定なら NULL）、
`acked_at`（ack 観測時刻、未観測なら NULL）、`quiesced_at`（6.4 の保持上限の起点）。

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
5. **`ack_deadline_sec` field** と 2 節のバリデーション（実効期限の畳み込み、
   `ack_deadline_sec >= 1` の有限整数チェック、不変条件 `ack_deadline_at <= effective_expires_at`）。
6. **delivery receipt の提示面**: 状態照会 API（`get_message_status(msg_id)` 相当）。
   `timed_out` / `expired` を返すときに 5 節の分類 (`no_recipient_present` / `unacked`) を添える。
   GC 済み (6.4) の `msg_id` には `unknown` を返す。
7. **`requires_ack` を実際に読む**。`False` の message は ack 待ちをせず、put 成功時点で
   `sent` を quiescent 扱いにする（`timed_out` / `expired` / `acked` へは遷移しない。6.2 参照）。
   **default は実装側の `False` で確定している**（12.3 の旧 U1）ので、`models.py:76` は変更しない。
   receipt が欲しい送信者が `requires_ack=True` を明示する形になる。
   食い違っている `0185:153`（「MVP は default `true`」）の記述を実装に合わせて訂正するのは
   0185 側の修正であり、本メモのスコープ外（11 節）。
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
> **sender outbox (`outbox.db`) + transport-level retry（指数 backoff、initial attempt 込みで合計 6 回、
> `effective_expires_at` で強制打ち切り）+ ack key reader による delivery receipt** を実装し、
> ack timeout は `ack_deadline_sec`（未指定時は `effective_expires_at` にフォールバック）で定義する。
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
| B2: 新 `msg_id` の自動再送 | receiver dedup が `msg_id` 基準なので新 id は素通りし、元 message を既に観測した receiver には重複表示される。誰がそうなるかを sender は判別できず、意味的 dedup も無いため回避不能な重複リスクを負う (3 節 B2) |
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
レビューで accept されたら、`docs/adr/NNNN-messaging-delivery-receipt-policy.md` として
ADR 化する。ADR-0022 の Related に追加し、本メモを設計根拠として参照する形にする。

**番号は accept 時に採番する（本メモでは確定させない）。** 初版は `0031` を予約していたが、
**`0031` は PR #303 で別内容（CHANGELOG 競合回避のための batched merge window 方式）に
既に使われている**。ADR 番号は先に merge された側が勝つため、本メモの ADR 化に着手する時点で
`docs/adr/` の最大番号 + 1 を採り直すこと（PR #303 が採った採番規則と同じ）。
`0031` を参照している箇所は本節のみなので、振り直しても他節への影響は無い。

## 11. 実装時の注意（Phase 1.5 引継ぎ）

- ADR-0023 の layering を維持する: `messaging` は `memory` を import しない。
  `core.transport` の retry は `core` 経由なので利用可 (`memory/purge.py:53` と同じ形)。
- `outbox.db` は `inbox.db` (`mcp_server.py:180`) と別ファイルにする。
- `purge_expired_messages` (`mcp_server.py:1061`) は `msg/**` 全体を掃く (`purge.py:44`) ので、
  他 agent の未読 message も消える（ただし削除対象は `Message` として parse できた期限切れ entry
  だけで、ack key は含まれない。6.1 (a)）。outbox の state 判定がこの sweep と競合しないよう、
  「key が消えている」ことを `failed` の根拠にしてはならない（`expired` と区別できないため）。
- 1.3 で挙げた memo と実装の食い違い（`requires_ack` の default）は **実装側の `False` に寄せると
  決定した**（12.3 の旧 U1）。したがって Phase 1.5 では `models.py:76` を変更せず、
  `0185:153` の「MVP は default `true`」という記述を実装に合わせて訂正する。
  訂正そのものは 0185 側の修正なので本メモの変更範囲には含まれない（別途行う）。
- `ttl_sec` を schema 上 required にするか（1.7 / `0185:147` の食い違い）は本メモの決定事項では
  なく、0185 側の schema 規定に従う。**ただし「両方未指定 = 無期限を本メモが拒否しない」ことは
  決定事項である**（2 節、12.3 の旧 U2）。required 化を選んだ場合も本メモの
  `effective_expires_at` 畳み込み規則は変わらず、無期限の枝が到達不能になるだけである。

## 12. 未決事項とチューニングパラメータ

**この 2 つは別物なので分けて書く。**

- **未決事項 (12.1)** = 本メモが決めていないこと。実装に入る前に、表の「決定 gate」を通して
  誰かが決める必要がある。**未決事項の記述は本文中でも断定調にしない。**
- **チューニングパラメータ (12.2)** = 本メモが**初期実装値として決定した**こと。
  実装はこの値をそのまま書けばよい。運用計測の結果として後から値を変えられるが、
  値を変えても設計（state machine・不変条件）は変わらない。**「後で変えられる」ことは
  「まだ決まっていない」ことを意味しない。**

12.1 に挙げた項目以外の記述は、本文・12.2 を含めてすべて決定事項である。

### 12.1 未決事項（実装前に決定 gate が必要）

| id | 未決の内容 | 決めるのに必要なもの | 決定 gate（誰が・いつ・何を根拠に） |
|---|---|---|---|
| U5 | **ack-key retention** (6.1 (a)) を設計上の保証にするか。6.1 (a) で確定させたとおり、現状は (i) 同梱 config に `msg/**` storage が無いため ack key はそもそも retain されず、(ii) storage を足しても `purge_expired_msgs` は ack payload を `Message` として parse できず skip するため削除経路が無い。保証するには **`msg/**` storage を前提条件として明文化すること**と、**purge 側に ack key を扱う削除経路を追加すること**の両方が要る（sweep の定期化だけでは解決しない）。※**observable receipt window の上限 24h (6.1 (b), 6.4) は決定事項であり、これとは別物** | (1) messaging が `msg/**` storage を前提とするかの構成方針、(2) ack key の削除条件（何をもって不要とみなすか。message body と違い ack には expiry が無い） | 実装者が、**7 節の項目 4（ack reader）の実装に着手する時点**で、その時点の storage 構成を確認して決める。ack reader は ack key を読む唯一の機能なので、ここが決定に必要な情報が揃う最初の地点である。それまで本メモは「ack-key retention に保証は無い」を前提に読むこと（この前提は上記 (i)(ii) のどちらの構成でも成り立つ） |
| U6 | `no_recipient_present` / `unacked`（5 節）を receipt でどう提示するか（文言・UX） | delivery receipt の提示面 (7 節の項目 6) の UI 設計 | 実装者が、**7 節の項目 6 の実装時**に決める。state と診断ラベルの集合自体は決定済みなので、決めるのは表示のみ |

### 12.2 決定済みだが運用計測で見直しうるチューニングパラメータ

以下は**初期実装値として決定済み**である。実装はこの値で書く。
「値」だけがチューニング対象で、その右の**不変条件は変更してはならない**。

| 項目 | 決定した初期値 | 変更してよい根拠（計測） | 変えてはいけない不変条件 |
|---|---|---|---|
| transport retry の総試行回数と遅延列 (3 節) | 合計 6 回（initial attempt 1 + retry 5）、遅延 `1,2,4,8,16s`、±20% jitter | Zenoh put 失敗率と、失敗が回復するまでの実測時間分布。少なくとも「6 回目で成功した割合」を計測できるログ | 上限が**有限**であること。遅延列が有限列であること（クランプを導入しないこと、3 節） |
| outbox 行の保持上限 = observable receipt window (6.4, 6.1 (b)) | `quiesced_at` から 24 時間 | 状態照会が quiesce の何時間後に行われるかの実績。`unknown` を返した回数を数えれば足りる | 有限の上限が存在すること。GC 後は `unknown` を返すこと |
| ack reader の get timeout (3 節) | 3.0 秒（`mcp_server.py:907` / `purge.py:80` に揃える） | 実測の ack get レイテンシ分布 | 有限であること。常駐ポーリングスレッドを持たないこと |

### 12.3 明示的に決定事項へ移した項目（旧 U1 / U2 / U3 / U4）

初版では以下を「未決」として挙げていたが、本文が既に断定的な実装契約として指定していたか
（旧 U2-U4）、決定 gate が機能しない形になっていた（旧 U1）ため矛盾していた。
**いずれも決定事項に移した**。id は旧版とのレビュー追跡のため欠番として残す。
したがって **12.1 に残る未決事項は U5 / U6 の 2 件**である。

| 旧 id | 内容 | 決定 |
|---|---|---|
| U1 | `requires_ack` の default を `True`（memo `0185:153` 側）に寄せるか、`False`（実装 `models.py:76` 側）に寄せるか | **`False`（実装側）に確定し、`0185:153` の記述を実装に合わせて訂正する**。初版は「送信者が意識せず送った message が receipt 対象外になるのは驚き最小則に反する」として `True` を推奨していたが、**ack は受信側の明示的な opt-in である**という実装事実（ack を書くのは受信 agent が `ack_message` を呼んだときだけ。`mcp_server.py:1127` / `tmux_adapter.py:62` が「自分で `ack_message` を呼ぶ責務がある」と明記）を踏まえて反転させた。default を `True` にすると、ack を呼ばない受信者宛の message が軒並み `timed_out` になり、`timed_out`（= 受信者が確認していない、5 節の `unacked`）という診断が「既定でほぼ全件に付く」ラベルに退化して価値を失う。receipt が欲しい送信者が `requires_ack=True` を明示する形なら、`timed_out` は常に「明示的に確認を求めたのに返ってこなかった」を意味する。なお default 値は state machine にも不変条件にも影響しない（12.2 と同じ性質のチューニング値）ため、実利用ログを見てから反転させることは将来も可能である。**この決定により、初版の U1 の決定 gate（7 節の項目 7 の着手時点で、項目 1 の数週間の実利用ログを根拠に決める）は撤回する** — 項目 1 と項目 7 はどちらも Phase 1.5 に含まれるため、gate が Phase 1.5 の内部で循環していた |
| U2 | expiry 無し（`ttl_sec` / `expires_at` の両方が未指定）の message を許すか | **許す**（2 節「実効期限の定義」の表のとおり、拒否しない）。無期限 message は `timed_out` には遷移しうるが `expired` には遷移せず、outbox 行の 24h 保持上限 (6.4) で必ず終端する。なお `ttl_sec` を schema 上 required にするかは `0185:147` 側の規定の話であり本メモの決定事項ではない（11 節）。required 化しても本メモの畳み込み規則は変わらない（無期限の枝が到達不能になるだけ） |
| U3 | 総試行 6 回・遅延 5 段階の具体値 | **決定**（12.2 の 1 行目。実装契約は 3 節） |
| U4 | outbox 行の保持 24 時間 | **決定**（12.2 の 2 行目。実装契約は 6.4） |

## Related

- Issue #201, Issue #185, Issue #193
- ADR-0022 (`docs/adr/0022-zenoh-agent-messaging-flow-layer.md`)
- ADR-0023 (core / memory / messaging layering)
- `docs/design/0185-messaging-mvp-design.md`（「再送戦略」「未解決事項」）
