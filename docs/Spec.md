# kioku-mesh 現状仕様

この文書は、2026-05-28 時点のリポジトリ実装から読み取れる現状仕様をまとめたものです。将来構想ではなく、`src/mesh_mem/` と `tests/` が現在保証している振る舞いを基準にしています。ADR は 0001〜0013 までを反映しています。

関連図:

- draw.io 編集元: [mesh-mem-state-transitions.drawio](./mesh-mem-state-transitions.drawio)

### Observation lifecycle

![Observation lifecycle](./mesh-mem-observation-lifecycle.svg)

### SQLite sidecar index sync

![SQLite sidecar index sync](./mesh-mem-index-sync.svg)

## 1. 目的と前提

kioku-mesh は、複数の AI コーディングエージェントと複数 PC が作業上の観測・判断・不具合原因・設定変更などを共有するための分散メモリです。

動作モードは `MESH_MEM_BACKEND`（または `~/.config/mesh-mem/config.yaml` の `backend:`）で切り替わる 2 種です（ADR-0013）。

| モード | 実行されるもの | 得られるもの |
| --- | --- | --- |
| **Mesh**（既定 `zenoh`） | zenohd + RocksDB backend | 永続的なマルチホスト mesh |
| **Local** | SQLite のみ（zenohd 不要） | 単一ホストの永続ストレージ |

- Mesh モードのトランスポートは Zenoh 1.9 系を前提とする。
- Mesh モードでは永続化の真実は zenohd + RocksDB backend 上の key-value にあり（ADR-0010）、Python 側の SQLite は検索高速化のためのローカルサイドカー（派生キャッシュ）であって正本ではない。
- Local モードでは SQLite それ自体が唯一のストアであり、`state_dir()/local/index.db` という Zenoh サイドカー（`state_dir()/index.db`）とは物理的に別の DB に保存される。
- 旧「Tier 1」（`mesh start` / `mesh join` の in-process zenoh router）は公開アーキテクチャから外され、zenohd 無しで mesh を試すための demo パスに格下げされた（ADR-0013）。cross-host replication は ephemeral で本番用ではない。
- MCP サーバは stdio transport を前提とし、Claude Code / Claude Desktop / Gemini CLI / Codex CLI などの MCP ホストから呼び出される。
- パッケージの公開名は `kioku-mesh`、CLI は `kioku-mesh`、MCP サーバは `kioku-mesh-mcp`（内部 Python パッケージ名は歴史的経緯で `mesh_mem`、ADR や設定パスにも `mesh-mem` 表記が残る）。

## 2. データモデル

### Observation

Observation は保存されるメモリ本体で、原則 immutable です。内容を更新する場合は既存 ID を変更せず、新しい Observation を保存します。

主なフィールド:

| フィールド | 型 | 既定値 / 仕様 |
| --- | --- | --- |
| `content` | `str` | 本文。必須。 |
| `agent_family` | `str` | `MESH_MEM_AGENT_FAMILY`。未設定時は `unknown`。 |
| `client_id` | `str` | `MESH_MEM_CLIENT_ID`。未設定時は `unknown`。 |
| `pc_id` | `str` | ホスト単位の永続 UUID。 |
| `session_id` | `str` | プロセス単位 ID。 |
| `project` | `str` | 任意のプロジェクト名。 |
| `tags` | `list[str]` | 任意タグ。 |
| `observation_id` | `str` | 32 文字 hex の UUID4。 |
| `created_at` | `str` | UTC の `YYYY-MM-DDTHH:MM:SS.ffffffZ`。 |
| `memory_type` | `str` | `note` / `decision` / `bug` / `pattern` / `config` / `summary`。 |
| `importance` | `int` | 1-5。モデル生成時は範囲外を clamp、CLI は argparse で 1-5 のみ許可。既定値 2。 |
| `subject` | `str` | 短いトピック名。 |
| `summary` | `str` | 検索結果で本文より優先表示される 1 行要約。 |
| `source_files` | `list[str]` | 関連ファイルパス。 |
| `references` | `list[str]` | 関連する PR / Issue / 外部参照識別子。 |
| `supersedes` | `list[str]` | 置き換え元 Observation ID。 |

JSON 読み込みは forward/backward compatible です。古い JSON に存在しない追加フィールドは既定値で補完されます。新しいスキーマの peer が送ってきた未知フィールドは drop せず、dataclass field ではない `_extras` side-channel に退避して保持し、`to_json` で再出力します（ADR-0012）。これは rolling upgrade 中に旧 peer が新フィールドを握り潰して silent data loss を起こすのを防ぐためです。`_extras` の保持境界は `to_json` / `from_json` ペアに閉じており、`dataclasses.replace()` などこのペアを経由しない clone では失われます。

未知の `memory_type` を受信した場合は例外にせず `note` に丸めますが、ログレベルは DEBUG です（legacy データの全走査で WARNING ノイズを出さないため）。元の値は受信側では失われます。なお新規構築（CLI / MCP / モデル生成）時の `memory_type` は閉じた enum で validation され、範囲外は拒否されます。

### Tombstone

削除は Observation 自体を即時削除せず、対応する tombstone を発行して表現します。

主なフィールド:

| フィールド | 型 | 仕様 |
| --- | --- | --- |
| `observation_id` | `str` | 削除対象の 32 文字 ID。 |
| `reason` | `str` | 任意の削除理由。 |
| `deleted_at` | `str` | UTC の `YYYY-MM-DDTHH:MM:SS.ffffffZ`。 |

Tombstone の存在そのものが削除シグナルです。timestamp の大小ではなく、対応する tombstone が 1 件でも観測されると検索結果から隠します。

## 3. Zenoh key 設計

Observation の key:

```text
mem/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
```

Tombstone の key:

```text
mem/tomb/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}
```

Tombstone key は Observation key の `mem/obs/` を `mem/tomb/` に置き換えたミラー構造です。

検索時の identity フィルタは key 階層に対応します。`agent_family` / `client_id` / `pc_id` / `session_id` を指定すると、その階層を絞り込みます。

## 4. Identity

Identity は保存時にサーバ側で解決されます。MCP の `save_observation` は identity を引数に持ちません。これは LLM が誤った identity を渡して名前空間を汚染するのを避けるためです。

解決規則:

| 識別子 | 解決方法 |
| --- | --- |
| `agent_family` | `MESH_MEM_AGENT_FAMILY`。未設定時 `unknown`。 |
| `client_id` | `MESH_MEM_CLIENT_ID`。未設定時 `unknown`。 |
| `pc_id` | `MESH_MEM_STATE_DIR/pc_id` に永続化。なければ UUID4 を生成。 |
| `session_id` | `MESH_MEM_SESSION_ID`。未設定時は `{YYYYMMDDTHHMMSSZ}-{uuid8}` を生成し、プロセス内でキャッシュ。 |

`pc_id` の初回生成は一時ファイルと hard link publish により、複数プロセスの同時起動でも空ファイルや不一致 ID を避けます。`MESH_MEM_STATE_DIR` は POSIX hard link をサポートするファイルシステム上に置く必要があります。

複数 agent を 1 ホストで同時に動かす場合の `agent_family` / `client_id` の付け方、`direnv` でのスコープ化、MCP-launched agent への env 渡し方は [docs/multi-agent.md](multi-agent.md) を参照。

状態ディレクトリ:

- `MESH_MEM_STATE_DIR` が非空ならそれを使用。
- Linux は `~/.local/share/mesh-mem` 固定。`XDG_DATA_HOME` は互換性維持のため無視。
- macOS は `~/Library/Application Support/kioku-mesh` 相当を `platformdirs` で解決。
- Windows は `%LOCALAPPDATA%\kioku-mesh` 相当を `platformdirs` で解決。

## 5. 保存・検索・取得

### 保存

CLI の `kioku-mesh save` と MCP の `save_observation` は、どちらも `Observation` を作成して保存します。

Mesh モードでは Zenoh に `put` し、Zenoh への書き込み成功が保存成功の契約です（ADR-0010）。retryable な失敗時は `state_dir()/pending_puts.db` にローカル queue され、後でバックグラウンドまたは `kioku-mesh drain --pending` で replay されます。queue は新しい順で上限件数までに trim されます。保存後、SQLite ローカルインデックスにも best-effort で upsert します。SQLite エラーはログに記録されますが、Zenoh 書き込み成功を取り消しません。

Local モードでは Zenoh を介さず、SQLite（`state_dir()/local/index.db`）への upsert がそのまま保存です。

### 検索

既定の検索経路は SQLite ローカルインデックスです。

- `project` / `agent_family` / `client_id` / `pc_id` / `session_id` / `since_iso` / `query` / `limit` を組み合わせて絞り込む。
- フィルタは AND 条件。
- tombstone 済みの行は既定で除外。
- 結果は `created_at DESC`。
- `limit` は 1 以上、最大 `MAX_SEARCH=10000` に丸める。これは返却件数上限であり、Zenoh fallback 時の走査件数上限ではない。
- SQLite 経路の `query` は `payload_json` 全体への case-insensitive substring match。本文だけでなく project、tags、subject、summary などにも一致しうる。

`MESH_MEM_DISABLE_INDEX=1` の場合、検索は Zenoh fallback 経路になります。この経路では `mem/obs/...` と `mem/tomb/...` を取得し、Python 側で project / since / query をフィルタします。Zenoh selector で絞れるのは identity 階層のみです。

### 単一取得

`get-memory` / `get_memory` は 32 文字の完全な `observation_id` を要求します。短縮 ID は受け付けません。

検索順序:

1. SQLite index を `include_deleted=True` で検索。
2. 見つからなければ Zenoh の `mem/obs/**` を走査。

Tombstone 済み Observation も、物理削除や監査用途のため単一取得では見つかることがあります。

## 6. ローカル SQLite インデックス

SQLite index は `MESH_MEM_INDEX_DB` があればそのパス、なければ `state_dir()/index.db` に作られます。`MESH_MEM_INDEX_DB=:memory:` も指定できます。

テーブルは `obs_index` で、主な列は以下です。

- `observation_id` primary key
- `project`
- `created_at`
- `memory_type`
- `importance`
- `subject`
- `summary`
- `payload_json`
- `deleted_at`（tombstone 受信で stamp）
- `shadowed_at`（rebuild reconcile で正本に存在しなくなった live 行を hide するシャドウ列。ADR-0011）

行の可視性は 3 階層です。`deleted_at IS NULL AND shadowed_at IS NULL` が live、`deleted_at` 有りが tombstone 済み、`shadowed_at` 有り（かつ `deleted_at` 無し）が shadow です。既定検索は live のみを返します。

インデックス:

- `(project, created_at DESC)`
- `(created_at DESC)`

SQLite は WAL モード、`synchronous=NORMAL`、`busy_timeout=5000` で開かれます。長時間動く MCP サーバで WAL が肥大化しないよう、256 upsert ごと、および close 時に `PRAGMA wal_checkpoint(TRUNCATE)` を試みます。

インデックスの同期:

- `put_observation` は Zenoh 成功後に `upsert`（`shadowed_at` はクリアされる）。
- `put_tombstone` は Zenoh 成功後に `deleted_at` を stamp。
- `start_index_subscriber` が `mem/obs/**` と `mem/tomb/**` を購読し、別セッション・別 peer の書き込みを取り込む。DELETE-kind の sample も正しく扱う（PR #65）。
- 起動時に `rebuild_from_zenoh` を走らせると、Zenoh 上の obs/tomb を走査して SQLite を再構築する。Zenoh を真実源とする方針（ADR-0010）に従い、正本に存在しなくなった live 行は物理削除ではなく **shadow-delete**（`shadowed_at` に stamp）して hide する（ADR-0011）。物理削除しないのは、peer の一時離脱や storage 起動順序による transient な不在で誤 prune したとき、戻ってきた obs を可逆に復活させるためです。再度 PUT を受信すれば `upsert` で shadow は解除されます。

非 JSON payload は subscriber で DEBUG ログ扱いになり、通常運用の WARNING ノイズにはしません。

## 7. 起動時 rebuild ポリシー

`rebuild_from_zenoh` は Zenoh 上の全 obs/tomb を走査するため、データ量が多い mesh では重い処理です。

現在のポリシー:

| 起動形態 | 既定 |
| --- | --- |
| `kioku-mesh` CLI | rebuild を skip。one-shot 起動を高速化するため。 |
| `kioku-mesh-mcp` など長時間プロセス | rebuild を実行。起動時の一度だけコストを払う。 |

優先順位:

1. 明示 override (`kioku-mesh --rebuild ...`)
2. `MESH_MEM_FORCE_REBUILD=1`
3. `MESH_MEM_SKIP_REBUILD=1`
4. モジュール既定値

`--rebuild` は `MESH_MEM_SKIP_REBUILD=1` より優先されます。

## 8. 削除と GC

### 論理削除

`kioku-mesh delete <observation_id>` と MCP の `delete_memory` は tombstone を発行します。Observation 本体は即時には消えません。

単一 ID 指定の削除対象は 32 文字完全一致のみです。短縮 ID は誤削除防止のため拒否します。

CLI の `delete` は `observation_id` を省略して、`--project` / `--pc-id` / `--since` / `--until` の組み合わせによるバルク tombstone も発行できます。`--dry-run` で件数のみ確認、`--yes` で対話確認をスキップ、`--batch-size`（既定 1000、最大 10000）でページ・進捗粒度を制御します。MCP の `delete_memory` は単一 ID のみでバルク削除は持ちません。

### 物理削除

`kioku-mesh gc --retention-days N` は、`deleted_at` が保持期間を超えた tombstone と対応 Observation を物理削除します。既定 retention は 30 日です。

`--project` を指定した場合は、該当 project の tombstone 済み Observation のみを対象にします。現在の実装では SQLite index を利用した O(N) fast path を取り（ADR-0008）、実行前に必ず `rebuild_from_zenoh` で正本に合わせます。index が無効または失敗した場合は Zenoh 全体走査へ fallback します。

`--project` 未指定時は全 project が対象です（後方互換）。

retention sweep は同時に、保持期間を超えた shadow 行（`shadowed_at` 済み）も物理削除します（Issue #70）。shadow 行は正本に対応が無いため obs/tomb の Zenoh 削除は伴いません。`--no-shadow-prune` を付けると shadow の物理削除を skip し、tombstone のみを対象にします。

### pc_id 単位のバルク purge

`kioku-mesh gc --by-pc-id <pc_id>` は、32 文字完全一致の `pc_id` に属する Observation を一括物理削除します（bench / spam データの掃除用）。`--session-prefix` で `session_id` の接頭辞によりさらに絞れます。既定は dry-run で、実削除には `--execute` が必要です（`--yes` で対話確認をスキップ）。この経路は tomb sweep / broadcast を行いません。

### 緊急 purge

`kioku-mesh gc --force-id <observation_id>` は単一 ID を即時物理削除します。対応する Observation と tombstone を可能な範囲で列挙して削除し、さらに以下の wildcard delete を best-effort で送ります。

```text
mem/obs/*/*/*/*/{observation_id}
mem/tomb/*/*/*/*/{observation_id}
```

wildcard delete の対応状況は Zenoh backend に依存するため、失敗しても例外にはしません。完全性が必要な機微情報 purge では、保持している可能性のある各 peer で同じ `--force-id` を実行する運用が前提です。

## 9. CLI 仕様

グローバル:

- `kioku-mesh --version`
- `kioku-mesh --rebuild <command>`: 初回 index 初期化時に Zenoh から rebuild する。

サブコマンド:

- `save CONTENT`
  - `-p`/`--project`, `-t`/`--tags`
  - `--memory-type`: `note` / `decision` / `bug` / `pattern` / `config` / `summary`
  - `--importance`: 1-5
  - `--subject`
  - `--summary`
  - `--source-files`: カンマ区切り
  - `--references`: カンマ区切り
  - `--supersedes`: カンマ区切り
- `search [QUERY]`
  - `--agent-family`, `--client-id`, `--pc-id`, `--session-id`
  - `-p`/`--project`
  - `--since`
  - `-n`/`--limit`: 既定 50
  - `--format`: 出力フォーマット（既定 `text`）
- `get-memory OBSERVATION_ID`
- `delete [OBSERVATION_ID]`
  - `-r`/`--reason`
  - バルク削除: `-p`/`--project`, `--pc-id`, `--since`, `--until`, `--dry-run`, `--yes`, `--batch-size`
- `status`
  - version、`backend`（モード）、`pc_id`、`session_id`、`agent_family` / `client_id`（値と解決ソース）、`zenoh_session`、`last_put_at_iso`、`last_put_status`、`pending_puts`、最大 `MAX_SEARCH` 件内の件数、family/pc 別件数を表示。Mesh モードのときのみ `mesh_ready` を表示。
- `drain`
  - `--pending`: `pending_puts.db` の queue を replay。
  - `--limit`: 1 回の drain 件数上限。
- `gc`
  - `--retention-days`
  - `-p`/`--project`
  - `--force-id`
  - `--by-pc-id`, `--session-prefix`, `--execute`, `--yes`
  - `--no-shadow-prune`
- `init`: zenohd / local の starter config を生成。
  - `--mode`: `localhost`（既定）/ `local` / `hub` / `spoke`
  - `--listen`, `--connect`, `--out`, `--force`, `--print`, `--install-systemd`
- `doctor`: zenohd 到達性・config・state dir の診断。`--json` で機械可読出力。
- `mcp install`: 対応 MCP クライアントに `kioku-mesh-mcp` を登録。
  - `--client`（必須）, `--name`, `-e`/`--env`, `--force`, `--dry-run`
- `mesh start` / `mesh join PEER`: zenohd バイナリ無しの in-process zenoh router（try-it / demo パス。ADR-0013）。cross-host replication は ephemeral。

`status` の `mesh_ready` は情報表示です。成功した probe から最低秒数が経過すると `yes` になります。検索処理は readiness を待ちません。

## 10. MCP 仕様

MCP サーバは FastMCP で実装され、以下の tool を公開します。

- `save_observation`
- `search_memory`
- `get_memory`
- `delete_memory`
- `get_memory_status`

MCP 初期化時の instructions には、エージェントが能動的に `save_observation` を呼ぶべき条件と、保存を避けるべき条件の両方が含まれます。保存すべきものは設計判断、バグ原因、非自明な発見、再利用可能なパターン、設定変更、機能実装上の重要な方針、ユーザー確認済みの好み、セッションまとめなどです。逆に、PR / Issue の opened / pushed / merged / closed のような lifecycle tick、PR 説明や Issue 本文や ADR や CHANGELOG や commit message の言い換え、1 会話中の途中進捗、非自明な原因や判断を伴わない「tests pass」「build is green」だけのメモは保存対象外です。

`memory_type` は `decision` / `bug` / `pattern` / `config` を優先し、`summary` は「そのセッションで最終的にどの方向を選んだか」を残すときだけに使います。`importance` は 4-5 をプロジェクト全体や長期的な前提変更、3 を局所だが再利用価値のある知見の目安とし、1-2 を付けたくなる内容は「そもそも保存すべきか」を再確認します。

MCP tool は identity を引数に持ちません。検索 tool だけは narrowing 用に identity フィルタを受け付けます。

クライアント別の MCP 登録手順（Claude Code / Claude Desktop / Gemini CLI / Codex CLI / ChatGPT Desktop）と、Claude Code の SessionStart hook で起動時に直近メモリを注入する運用は [docs/mcp-clients.md](mcp-clients.md) を参照。

## 11. レプリケーションと整合性

複数 zenohd router を接続し、RocksDB storage backend の replication によって Observation と Tombstone を eventual-consistent に同期します。

テストで保証しているシナリオ:

- 片側 router が停止中に別 router で保存された Observation は、復帰後の replication tick 後に停止側へ同期される。
- split-brain 中に発行された tombstone は、復帰後に相手側へ同期され、該当 Observation を非表示にする。
- Tombstone は existence-based なので、時計の前後関係による last-writer-wins 判定を行わない。

ただし、`created_at`、`since_iso`、GC retention は各ホストの壁時計に依存します。NTP/chrony 等で時刻同期する運用が必要です。

## 12. 環境変数

| 環境変数 | 用途 |
| --- | --- |
| `ZENOH_CONNECT` | Python client が接続する Zenoh endpoint。既定 `tcp/localhost:7447`。 |
| `ZENOH_BACKEND_ROCKSDB_ROOT` | zenohd RocksDB backend の保存先。zenohd 側設定で使用。 |
| `MESH_MEM_BACKEND` | backend モード。`zenoh`（既定）または `local`。`~/.config/mesh-mem/config.yaml` の `backend:` より優先。 |
| `MESH_MEM_AGENT_FAMILY` | 保存時の `agent_family`。 |
| `MESH_MEM_CLIENT_ID` | 保存時の `client_id`。 |
| `MESH_MEM_SESSION_ID` | 保存時の `session_id` を固定する。 |
| `MESH_MEM_STATE_DIR` | `pc_id`・SQLite index・`pending_puts.db` の既定配置先。 |
| `MESH_MEM_INDEX_DB` | SQLite index DB の明示パス。 |
| `MESH_MEM_DISABLE_INDEX` | `1` で SQLite index を無効化し、Zenoh fallback を使う。 |
| `MESH_MEM_SKIP_REBUILD` | `1` で起動時 rebuild を skip。 |
| `MESH_MEM_FORCE_REBUILD` | `1` で起動時 rebuild を強制。 |
| `MESH_MEM_MCP_ALLOW_TTY` | `1` で MCP サーバが TTY 上でも起動できるようにする（通常は stdio 前提）。 |
| `MESH_MEM_ROUTER_ENDPOINT` | `doctor` の mesh router probe 先。既定 `tcp/localhost:17447`。 |
| `XDG_CONFIG_HOME` | `config.yaml` の置き場所のベース（既定 `~/.config`、配下 `mesh-mem/`）。 |

## 13. 制約

- `mem/**` に transport-level auth / encryption はない。7447/tcp は信頼済み peer のみに絞る必要がある。
- SQLite index は正本ではない。破損や未同期時は Zenoh 正本から rebuild する。
- FTS5 による全文検索は未実装。現在の SQLite 検索は `payload_json` への substring match。
- CLI は one-shot 起動で rebuild を skip するため、新規 host が既存 mesh の過去データをすぐ検索したい場合は `kioku-mesh --rebuild ...` を明示する。
- `gc --force-id` の wildcard purge は best-effort。到達不能 peer への完了確認はない。
- Native Windows は実験的扱い。CI は Linux 前提。WSL2 を強く推奨。ネイティブ Windows ホストの zenohd / RocksDB / Firewall / 時刻同期セットアップ手順は [docs/windows-setup.md](windows-setup.md) を参照。
- 0.x 系のため、API や on-disk schema は 1.0 まで互換性維持が保証されない。

## 14. テストで確認されている範囲

主なテスト対象:

- モデルの JSON 互換性、ID 形式、`memory_type` validation、未知フィールドの `_extras` 保持（ADR-0012）。
- identity のキャッシュ、環境変数 override、`pc_id` の同時生成安全性。
- CLI/MCP の保存、検索、削除（単一・バルク）、取得、status。
- SQLite index の schema、検索、削除 stamp、物理削除、rebuild、shadow-delete reconcile（ADR-0011）。
- Local backend（SQLite 単独）の保存・検索・削除。
- Zenoh fallback、session reconnect、readiness 表示、`pending_puts` の queue / drain（ADR-0010）。
- 2 router の offline diff sync と tombstone propagation。
- in-process mesh router（`mesh start` / `mesh join`）と doctor 診断。
- GC retention、project filter、`--force-id`、`--by-pc-id` バルク purge、shadow sweep、wildcard delete 失敗時の耐性。

実機・運用手順の概要は `README.md`、プラットフォーム別・マルチエージェント・MCP クライアント別の詳細手順は `docs/` 配下（[windows-setup.md](windows-setup.md) / [multi-agent.md](multi-agent.md) / [mcp-clients.md](mcp-clients.md) / [migration.md](migration.md)）、設計判断の背景は `docs/adr/`、PoC 検証結果は `docs/poc-reports/` を参照してください。
