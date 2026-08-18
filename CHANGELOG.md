# Changelog

All notable changes to this project will be documented in this file.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

kioku-mesh is in `0.x`: APIs and on-disk storage schema may change between minor
versions without a migration path until `1.0.0`. From `1.0.0` onward, the public
CLI / MCP / Python API and on-disk schema follow Semantic Versioning: breaking
changes require a semver-major bump or an explicit migration path (ADR-0029),
except while the repository has a single operator, per the conditions in
ADR-0030.

## [Unreleased]

> **このリリースは後方非互換の変更を含む (minor bump だが安全な更新ではない)。**
> 次のリリースは 1.3.0 (minor) を予定している。observation / tombstone の保存は、
> 稼働中の local zenohd に書き込み先 scope の storage が実在することを毎回確認し、
> 確認できなければ拒否するようになった (fail-closed)。**zenohd が停止している間は
> ローカルにも記録が残らない**。従来は SQLite への upsert と pending_puts への
> enqueue で受理されていたため、利用者から見た挙動変化は大きい。既存 host は
> `kioku-mesh config render-storages --apply` と zenohd 再起動、および
> `docs/scope-enforcement-cutover.md` の移行手順を終えるまで通常の save が全部
> 拒否される。ADR-0029 の semver 契約ではこれらの変更は major bump に相当するが、
> 単一運用者期間中の例外を定める ADR-0030 の条件 (CHANGELOG 冒頭・release notes
> 最上部への明記、upgrade notes の提示) を満たしたうえでユーザー判断により minor
> とした。更新前に下記 "Upgrade notes for v1.3" を必ず確認すること。

### Added

- host-global config (`~/.config/kioku-mesh/config.yaml`) に `storage_scopes` を
  追加した。保持・購読する scope をこの一つのリストから導出する (`mesh` /
  `user/<id>` / `team/<id>` のみ、`mesh` 必須、wildcard と余分なセグメントは
  正規化せず拒否)。zenohd storage の render、read path の selector、save
  preflight はすべてこの宣言から導出する (#316, ADR-0019 Phase E Addendum)
- `kioku-mesh config render-storages`: 既存 zenohd config の storages block だけを
  `storage_scopes` から render し直す。listen / connect / TLS / 手で足した設定は
  保持する。`--dry-run` / `--apply` / `--transitional` (移行中だけ pre-split の
  broad `agent_mem` を read-only の re-PUT source として残す) /
  `--acknowledge-missing-scopes` (#316)
- `kioku-mesh scope-migrate manifest` / `re-put`: pre-split の `mem/mesh/**` key を
  新しい clean な mesh storage へ移す 2 段階の移行。manifest は immutable で、
  `--expected-peers` に答えなかった router がある場合と、同じ key が異なる payload
  digest で返った場合は fail-stop。re-PUT は checkpoint 付きで再開でき、key ごとに
  live storage gate を通り、完了後に digest を verify する。`--dry-run` は本番実行が
  止まる 3 つの判定 (checkpoint binding / key ごとの gate / 最終 verify の予測) を
  実際に走らせる (#319)
- `kioku-mesh scope-inventory`: この host が scope ごとに何を保持しているかを
  読み取り専用で報告する (Zenoh directory probe + SQLite index)。tombstone は
  observation 行とは別の source から数える (#319)
- `kioku-mesh scope-purge`: 宣言していない scope の host-local copy を除去する。
  Zenoh delete は発行せず、RocksDB directory は削除ではなく rename して退避する。
  `agent_mem` (cutover の rollback artifact) と legacy 行は対象外 (#319)
- `kioku-mesh doctor`: 宣言 `storage_scopes` と自 host の live storage を照合する
  `storage_scopes` チェックを追加した。scope の欠落・volume dir や strip prefix の
  不一致・重複した broad storage の残存・scope preflight で止まっている queued put を
  FAIL として報告する。peer の storage は診断表示のみで、durability は自 host を
  基準に判定する (#316)
- 実 zenohd を 2 台使う統合テスト基盤。clean mesh dir への alignment と peer 間の
  replication parameter 一致を検証する (#318)
- 移行 runbook `docs/scope-enforcement-cutover.md`。freeze の範囲と期間、各ステップの
  gate、新しい team scope を増やす日常運用、2 段階の rollback、回収できない限界を
  記載した

### Changed

- **破壊的変更**: 通常の observation / tombstone save は、publish の前に自 host の
  live storage を Zenoh admin space で毎回確認するようになった。次のいずれかで
  拒否される: zenohd / admin storage を確認できない、`storage_scopes` が不正または
  書き込み先 scope が未宣言、live zenohd に exact な scope storage が無い、同じ key を
  受け取る broad storage が残っている、`ZENOH_CONNECT` が local router を指していない。
  **拒否された save は SQLite への成功記録も新規の queue 行も作らない** (痕跡を残さない)。
  WARN で通す flag は用意しない (#316)
- **破壊的変更**: zenohd が停止している間は save が一切通らない。従来は SQLite upsert +
  pending_puts で受理されていた (#316)
- **破壊的変更**: 広い `agent_mem` storage のままの host は、
  `config render-storages --apply` と zenohd 再起動を終えるまで通常の save が全部
  拒否される (#316)
- 既にキューにある書き込みで preflight に落ちるものは破棄されず queue に残り、
  `doctor` に表示される (#316)
- ローカルの mesh 単一 scope の `kioku-mesh mesh start` は、非永続の例外として mesh
  scope の save を受理し、その旨を process 内で一度 log に出す。user / team scope の
  書き込みは拒否する。**リモート endpoint は拒否**され、doctor もその状態を例外と
  して扱わず FAIL とする (#316)
- read isolation は既定 off。`KIOKU_MESH_SCOPE_ISOLATION=enforce` のときだけ
  subscriber / rebuild / fallback / purge の selector が宣言 scope に絞られる。この
  flag は read path 専用で、write preflight は緩めない (#316)
- `kioku-mesh init` は `storage_scopes` から scoped storage を出力するようになった。
  既存 host の変換は `init --force` ではなく `config render-storages` と移行 runbook で
  行う (`init --force` は config を全文書き直すが、旧 `agent_mem` directory のデータは
  移動しない)。`--apply` は SQLite inventory に observation があるのに render されない
  scope があるとき、`--acknowledge-missing-scopes` なしでは適用を拒否する (#316)
- `migrate-visibility` は target scope に live で exact、かつ重複しない durable storage が
  無ければ copy も delete も行わずに拒否する。resume した repair PUT にも同じ判定が
  かかる (#316)

### Fixed

- `doctor` の `shadow_visibility` hint が実在しない `kioku-mesh gc --shadows` を
  案内していたのを修正し、実際の既定挙動（`--no-shadow-prune` で無効化可能）を
  案内するようにした。

### Upgrade notes for v1.3

- **部分適用は自己修復しない。** key expression の異なる storage は別の replica group に
  なるため、旧 broad config のまま取り残された host は新しい `mem/mesh/**` group と
  alignment できない。live publication は届くが、遅れている間の差分は後から埋まらない。
  全 peer に対して一つの maintenance window 内で適用し、片側だけ適用した状態で通常運用を
  再開しないこと。
- 既存 host の移行手順は `docs/scope-enforcement-cutover.md` を参照する。freeze は
  MCP process だけでなく **raw Zenoh writer を含み**、manifest 生成前から final verify
  完了まで維持する必要がある (freeze 違反で失われた key は verify では検出できない)。
- 新規 host は `kioku-mesh init` がそのまま scoped storage を出力するので、移行は要らない。
- save が拒否されるようになった場合は、まず `kioku-mesh doctor` の `storage_scopes` を
  見る。宣言と live storage の差分と、次に打つコマンドが表示される。
- 旧 `agent_mem` directory は移行後も削除しない。cutover の rollback artifact であり、
  `scope-purge` の対象にもならない。

## [1.2.0] - 2026-08-17

> **このリリースは後方非互換の変更を含む (minor bump だが安全な更新ではない)。**
> `search_memory` と `recall_context` は、これまで無制限に返していた結果を
> 20,000 UTF-8 バイトで打ち切るようになった。件数や内容量が多い既存の呼び出しは、
> これまでと違って一部の結果しか受け取れなくなる場合がある。
> `save_observation` も、従来は受理していた正当な文章（guard 自身の検出対象である
> 閉じタグの連結を本文中で引用したもの）を ToolError で拒否するようになった
> (#312, #314)。受理範囲を狭める変更であり、これも semver 上 breaking。
> ADR-0029 の semver 契約ではこれらの変更は major bump に相当するが、単一運用者
> 期間中の例外を定める ADR-0030 の条件（CHANGELOG 冒頭・release notes 最上部への
> 明記、upgrade notes の提示）を満たしたうえでユーザー判断により minor とした。
> 更新前に下記 "Upgrade notes for v1.2" を必ず確認すること。

### Added

- `save_observation`: content / subject / summary に MCP tool-call の断片
  (`</content>` 等の閉じタグに続く save_observation の既知パラメータ名の
  開始/終了タグ（間に空白・改行・タブが入っていても検出される）、または name に
  既知パラメータ名を持つ `<parameter name="subject">` のような
  `<parameter name="...">` 表記) が
  混入している場合に ToolError で拒否する入力検証を追加した。クライアントが
  tool call を組み立てる際に文字列を終端し損ねると、本来 `memory_type` などへ
  渡るはずだった値が content 内に文字列として死蔵される (2026-06〜08 に 4 件、
  すべて claude-code)。フィールド境界を送信側に任せないための恒久的な検証で、
  検出のみを行う (サーバ側でのサニタイズはしない)。**破壊的変更**: 検出パターンは
  `save_observation` 自身の引数名にアンカーしているため一般的な XML の大半は
  弾かれないが、この guard 自体を解説する文章のように `</subject><summary>` 等の
  閉じタグ連結を本文中に引用した正当な prose は既知の false positive として拒否
  される (既存 1533 件に対し誤検知 0 / 真陽性 6 の測定はこの既知 false positive
  発見前のもの)。回避方法は当該マークアップを引用せず記述に言い換えること
  (ToolError メッセージが同じ言い回しを案内する。`mcp_server.py`
  save_observation のフィールドチェック実装を参照)。閉じタグの直前に空白・
  改行が入る形も検出する (#312, #314)。
- `kioku-mesh doctor`: 保存済みエントリに残っている tool-call 断片を洗い出す
  読み取り専用チェック `tool_call_fragments` を追加した
  (`--check-tool-call-fragments` で単独実行可)。live かつ non-superseded な
  effective 集合を走査して該当 observation_id を列挙するのみで、書き換えはしない。
  (#312)
- `get_memory_status` に直近7日の family 別 save 数を追加 (#286, closes #280)
- `search_memory` / `recall_context`: read-side project aliases
  (`mesh-mem` ↔ `kioku-mesh`). A project filter now matches every stored
  `project` label of the same logical project in **both** directions: the
  canonical name reaches the pre-rename history saved under the legacy name
  (the Issue #278 symptom), and the legacy name still reaches rows saved under
  the canonical one. Both labels are OR-ed inside a single backend query, so
  results keep their order and carry no duplicates. The mapping lives in a
  small hardcoded table (`core/project_alias.py`) and is applied only to the
  incoming search filter; `save_observation` still persists the literal
  `project` value it was given. `recall_context` names the expansion in its
  `filters:` line (`project='kioku-mesh' (also matching 'mesh-mem')`)
  (#288, closes #278).
- supersede suggestion (ADR-0026) の候補検出・ヒント描画で握り潰していた例外に
  debug ログの breadcrumb を追加した (CLI `save` / MCP `save_observation` の
  両経路)。save の成功は従来どおり守りつつ、検出が繰り返し失敗している場合に
  原因を追えるようにする。あわせて MCP 経路の renderer-error 回帰テスト
  (候補の描画中に例外が出ても save が成功し candidates が落ちるだけであること)
  を追加した。(#293, closes #236)
- Regression tests: parameterized `tests/test_store_errors.py` coverage
  verifying the Zenoh fallback's tombstone / since-until / cursor base
  filters are never overridden by a matching query term in the `or` and
  `and_or` search modes (#294, closes #230). Sabotage testing confirmed the
  tombstone and since/until cases had no prior coverage against `or`/`and_or`;
  the
  cursor case was already caught by an existing `and`-mode test but not
  against `or`/`and_or`.
- `search_memory` / `recall_context`: three more read-side project aliases
  (`/home/gisen/work/mesh-mem` -> `kioku-mesh`, `portable_colorized_scanner` ->
  `portable-scanner`, `rmf` -> `rmf_ws`), so the spelling variants found in the
  store resolve to one logical project the same way `mesh-mem` -> `kioku-mesh`
  already did (`PROJECT_ALIASES` now has 4 entries total) (#309).

### Changed

- docs: `search_memory` / `recall_context` の tool description に日英クエリ分割運用を明記
  (#311)
- messaging: message body size limits are now measured, enforced, and documented
  (#295, closes #202). The 64 KiB limit applies to `body` itself (it was
  previously applied to the whole serialized message, so a 64 KiB body was
  rejected by the ~434-byte
  JSON envelope); a separate 192 KiB envelope cap stops `payload` / metadata from
  smuggling content past it, and tmux injection keeps its 8 KiB body cap. Sizes
  are counted in UTF-8 bytes, at-limit is accepted, and over-limit is rejected —
  never truncated or split — with an error naming the actual size, the limit, and
  the alternative (shorten the body, or `save_observation` plus a short pointer).
  Zenoh itself was measured carrying 64 MiB payloads intact, so these are
  recipient-context limits, not transport limits; the measurements are recorded in
  `docs/design/0185-messaging-mvp-design.md`. The limits are enforced on both
  ends: `check_messages` and the push subscriber re-validate `body` (including
  the legacy `payload` fallback) and the serialized envelope after
  deserialization, so a message written straight into Zenoh by an older peer or
  an external publisher cannot bypass the cap. `check_messages` replaces an
  over-limit body with an explicit `[kioku-mesh: message body withheld — …]`
  notice plus a new `body_rejected` field rather than dropping the message
  silently or truncating it; the push subscriber drops it with a WARNING, since
  `check_messages` still surfaces the same message with its notice. Withholding
  the body alone was not enough: an over-limit envelope could still return its
  bulk through `delivery_adapters`, `subject`, `scope`, `msg_id`, or a
  sender/recipient id, so ~197 KiB reached the recipient with
  `body_rejected: true`. Every field of a `check_messages` item is now bounded —
  1 KiB per identity-shaped field, 4 KiB per `subject`, 16 `delivery_adapters`
  entries — an over-limit envelope is rebuilt from the minimal identity + notice
  set, and the encoded item is then re-measured against a 72 KiB per-message
  budget as a backstop. A new `withheld_fields` list plus the notice text name
  what was dropped, so nothing goes missing silently.
- **BREAKING**: `search_memory`: output is now capped at `SEARCH_OUTPUT_MAX_BYTES`
  (20,000 UTF-8 bytes); when exceeded, results are dropped from the tail and a
  trailing `[truncated: showing N of M result(s); ...]` line is appended
  (#291, closes #277). The cap covers the *whole* returned text — any prefix
  banner, the entries, and the truncation notice itself — so
  `len(result.encode('utf-8')) <= SEARCH_OUTPUT_MAX_BYTES` always holds. When a
  single observation is too large to show in full, its header and full 32-char
  `<id=...>` are preserved and only the body is shrunk at a safe UTF-8
  boundary, so `get_memory` / `delete_memory` stay callable on a partially
  displayed result. The `showing N of M` counts observations only; a prefix
  banner (e.g. an AND->OR fallback marker) is never counted as a result.
- `kioku-mesh mcp install --client <client> --repair`: overwrites only the
  retired `MESH_MEM_*` identity env vars (`MESH_MEM_AGENT_FAMILY` /
  `MESH_MEM_CLIENT_ID`) on an already-registered Claude Code or Codex CLI
  entry to the current `KIOKU_MESH_*` prefix. Command, args, every other env
  var and any field this version knows nothing about are left untouched —
  unlike `--force`, which resets the whole entry. Each client is edited
  through its own config file rather than through its CLI.
  (#287, closes #279)

  "Everything else untouched" is enforced rather than assumed:

  - Claude Code: the registration is read from, and written back to, the file
    Claude Code itself stores it in — `${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json`
    for the `user` and `local` scopes, `<cwd>/.mcp.json` for `project`. Only
    the two identity key *tokens* are rewritten, directly in the file's raw
    text; every other byte is copied through. Args (including arguments
    containing spaces), unknown fields, key order, indentation, colon/comma
    spacing, compact containers, `\/` and `\uXXXX` escapes and number spelling
    therefore survive by construction — including a valid `1e400`, which a
    re-serializing rewrite turned into the non-standard `Infinity` token that
    Claude Code rejects the whole file for. A config that already contains such
    a token, or a duplicate key, fails closed instead of being edited.
  - Claude Code: this deliberately replaces the earlier `claude mcp get` +
    `remove` + `add` route. That route was lossy and could not be made
    lossless: `Args:` is printed space-joined, so `["--flag", "two words"]`
    came back as three arguments, and a multi-line env value's continuation
    lines print at column 0 byte-identically to an unknown field. Editing the
    JSON also removes the window in which the entry was deleted but not yet
    re-added, so no rollback path is needed.
  - Claude Code: the write protocol is fail-closed at every step. A symlinked
    config is written through to its referent, so the link is not replaced by a
    regular file. The file is compared against the bytes `--repair` read both
    before the backup and immediately before the replace, so an update another
    process (Claude Code itself rewrites this file) made in between is never
    silently dropped — the repair aborts with nothing written. That comparison
    covers the file's metadata as well as its bytes: the replacement carries the
    identity, mode, owning group and extended attributes read at the start, so a
    permission change or an xattr (an ACL included) added while the replacement
    was being staged would be reverted by the rename. Both are re-read
    immediately before the replace and any difference fails the repair closed. A
    filesystem with no extended attributes at all reports none on both reads, so
    the check stays inert there rather than refusing. The previous
    file is copied to `<file>.bak-<UTC timestamp>` (timestamped so a hand-made
    `.bak` is never clobbered), created exclusively (`O_EXCL`) at no wider than
    `0600` so a `0600` config's secrets are not copied into a umask-wide
    backup. The new text is validated as strict JSON equal to the intended
    document *while it is still the temp file*, so nothing unverified is ever
    live, and the replacement keeps the original's mode, group and extended
    attributes. Because `os.replace` hands the destination the *staged* file's
    metadata, every xattr — POSIX ACLs and SELinux labels included — is copied
    across before the rename; an attribute that can neither be set nor is
    already present with the same value on the staged file fails the repair
    closed rather than disappearing silently. The rename is followed by a
    directory `fsync`. Once the replace has landed the backup is kept whatever
    fails next: a directory-`fsync` error reports that the config is live but
    not durably confirmed and names the retained pre-repair copy, instead of
    deleting the only file that could undo the repair. The result is finally
    read back: a mismatch restores the backup, unless the file changed again
    after the replace, in which case that newer file is left alone and the
    backup path is reported rather than overwriting another writer's update.
  - Claude Code: a name registered in more than one scope fails closed. The
    error lists every scope and file it was found in and how to resolve the
    ambiguity; nothing is written.
  - Codex CLI: only the identity key tokens inside the target entry's env are
    rewritten, so that entry's own `args` / `enabled` / `startup_timeout_sec`,
    its comments and its value quoting survive verbatim. The rewritten file is
    re-parsed and compared against the intended document before it is written;
    a layout the editor cannot handle fails closed with the file untouched. A
    quoted identity key keeps its quote style, so the rename changes the key
    name and nothing else.
  - `mcp install --client codex-cli` now escapes values per TOML 1.0, so a
    command path or env value containing `"` or `\` no longer produces a
    config that fails to parse.
- Superseded ADR-0029's semver clause with ADR-0030, which documents a
  single-operator exception allowing breaking changes to ship as minor
  releases (CHANGELOG-first disclosure + upgrade notes + release note
  placement required), and its failure condition once a third-party user
  exists. (#283)
- **BREAKING**: `recall_context`: output is now capped at `RECALL_OUTPUT_MAX_BYTES`
  (20,000 UTF-8 bytes), reusing the `search_memory` cap helper. Results were previously
  concatenated in full, which made the median response ~25 KB and pushed 22.9%
  of calls past the client's tool-output limit, so the recalled context never
  reached the conversation at all. Entries are dropped from the tail with the
  same `[truncated: showing N of M ...]` notice; a single observation larger
  than the whole budget falls back to a UTF-8-safe partial cut rather than
  returning nothing (#308).

### Fixed

- ADR-0028 補遺の live/effective 集計 SQL を `LocalIndex.search` の
  existence-based supersede filter (`local_index.py:669-687`) と一致するよう
  修正。単純な `superseded_by IS NULL` は supersede コピー自身が
  tombstone/shadow/失効している場合に元 row を誤って隠す方向にズレる
  (#313, PR #310/#311 cross-review 追従)。
- `recall_context` tool description の `search_mode='and_or'` の説明を
  「union」から「AND first; remaining slots are OR-filled」(intersection-first)
  に修正。実装 (`LocalIndex.search`) は AND 結果を先頭に置き、AND が limit に
  達すれば OR phase を実行しないため、旧記述は実装と逆だった
  (#313, PR #310/#311 cross-review 追従)。
- Messaging: acknowledgement rows with no matching message are no longer read
  as acknowledgements. `is_acked` is an exact-pair point lookup, so such a row
  suppressed a *live* message carrying the same
  `(msg_id, recipient_session_id)` with no warning — the residue left by the
  old purge bug was never inert. The inbox schema is now versioned (v2) and
  gains `pending_acks`, `message_tombstones`, `legacy_unknown_acks` and
  `recovery_audit`; opening an older database runs a single transactional,
  idempotent pass that keeps every ack with a message authoritative and moves
  the rest into `legacy_unknown_acks`, losslessly and without deleting
  anything. Whether such a row is purge residue or an acknowledgement that
  simply arrived before its message cannot be decided from the stored columns,
  and age is not evidence either, so nothing is guessed:
  `kioku-mesh messaging orphan-acks list` reports them read-only (paginated,
  no writes, no mtime change) and `orphan-acks recover` resolves one exact
  pair at a time behind a required fresh backup and an explicit `--execute`,
  recording a before image in `recovery_audit`. `recover --action promote`
  re-checks the matching message under the write lock, not only in preflight,
  so a message deleted in between can no longer leave an acknowledgement with
  nothing behind it; a promotion that would replace a *different* existing
  acknowledgement is refused rather than silently rolling it back, and the
  audited before image covers the quarantined row, the authoritative ack and
  the message together. There is deliberately no bulk delete and no age-based
  cleanup. This is the first of three units; the `check_messages` suppression
  path itself changes in the next one (N4). (#304)
- Messaging: `check_messages` no longer drops a message without saying so, and
  expiry purge no longer manufactures the ack rows that made it happen (N4,
  unit 2 of 3). Three changes: `is_acked` now believes an acknowledgement only
  while its message row still exists, so a stray ack can no longer suppress a
  live arrival carrying the same `(msg_id, recipient_session_id)`;
  `purge_expired` writes a `message_tombstones` row and removes the pair's ack
  in the same transaction as the message, so an expired id is retired rather
  than left half-deleted; and every arrival goes through one
  `register_or_classify` transaction that decides between a new message, a
  duplicate of a live one, a retired id, a retired id reused for a different
  message, a quarantined (legacy-unknown) pair, and an acknowledgement that
  arrived before its message. Acks observed ahead of their message are held in
  `pending_acks` — never read as authoritative — and promoted when the message
  lands. Anything withheld from the inbox is reported in a new `diagnostics`
  array on the `check_messages` response, carrying the withheld envelope, the
  ack metadata behind the decision and the exact-pair command that resolves
  it; `messages`, `count` and `truncated` are unchanged. Retiring an id means
  a resend needs a new `msg_id`: re-putting a purged id is reported as
  `duplicate_retired`, or as `protocol_violation` when the envelope changed.
  The inbox schema is v3 (two nullable columns on `message_tombstones`
  recording the retired envelope, added in place on existing databases).
- Messaging (#305): closed the three ways an arrival could still be withheld without
  a word (cross-review of the above). An ack row written *after* the upgrade
  pass — what an old writer leaves behind during a rolling upgrade — was not
  covered by the one-time migration, so it became authoritative as soon as its
  message was registered and reproduced the original symptom; the classifier
  now quarantines any ack that has no message, one exact pair at a time, on the
  ingress path itself, and an unresolved quarantine keeps its message withheld
  on every poll instead of only the first. An arrival that was already expired
  skipped the classifier entirely, so its id was never retired and could carry
  a different message later; expired arrivals are now tombstoned inside the
  classifying transaction rather than by the purge that follows the poll, which
  means one failed purge can no longer make a retired id reusable. And a
  classifier failure (a locked index, a disk error), an unreadable payload, or
  a failed inbox query were swallowed by a broad `except`, leaving `count: 0`
  with no diagnostics; each is now reported as `classification_failed`,
  `arrival_undecodable`, `reply_error` or `selector_failed`, carrying the
  affected message where there is one. Two further withholding reasons became visible in the
  same pass — `ack_first_promoted` and `expired_on_arrival` — and the delivery
  filter now takes ack state from the transaction that classified the arrival
  instead of asking the index a second time. The inbox schema is v4:
  quarantined acks record a `provenance` (`migration` or
  `post_migration_ack`), added in place on existing databases and shown by
  `orphan-acks list`.
- Messaging (#305): three narrower gaps from the second cross-review of the above. An
  expired arrival was deleted from Zenoh storage *before* it was classified, so
  a classification that then failed took the last copy of the message with it —
  no tombstone was written, the id quietly became reusable, and the
  "it is retried on the next poll" remedy was untrue because nothing would
  arrive again; the delete now happens only after the classifying transaction
  has committed. `record_ack` checked that the message was registered outside a
  transaction and wrote the acknowledgement in a second one, leaving a window in
  which `purge_expired` could retire the pair in between and turn the write into
  exactly the unmatched ack this release exists to stop creating; the check and
  the write now share one `BEGIN IMMEDIATE`, as every other writer here already
  did. And `IngressResult` no longer carries a `suppressed` flag: whether an
  arrival reaches the message list depends on the caller's `include_acked` /
  `include_expired` request as well as on the verdict, which the index never
  sees, so a single boolean decided inside the index could not answer it — an
  acked duplicate would have come back flagged as withheld even from a caller
  that asked for acked mail, and with no diagnostic attached. Callers read
  `acked` (decided inside the classifying transaction) and the code's
  `is_diagnostic` instead. (#305)
- Messaging: `kioku-mesh messaging orphan-acks status` reports whether a node
  has finished the ack-state rollout, and `docs/messaging-orphan-ack-rollout.md`
  describes the fleet procedure (N4, unit 3 of 3). The check is read-only and
  exits 0 when the node is done, 1 when something still blocks it, so it can be
  the per-host step of a rollout script. Three things block: a database below
  the current messaging schema, an ack with no message still sitting in `acks`
  outside the quarantine, and a pair quarantined *after* the migration pass —
  the last two both mean a writer predating the fix is still running somewhere.
  Quarantined rows that are merely unresolved do **not** block: they are the
  ambiguity the design refuses to guess away, and failing the check on them
  would manufacture pressure to bulk-clear the quarantine. A fleet is done when
  every node exits 0 with the same reported writer version; the command reads
  one database and does not infer anything about the others. (#306)
- `search_memory`: the #285 AND->OR fallback marker (`(no AND match; fell
  back to OR)`) was appended directly into the result list, so the #291
  byte-cap counted it as a result and `showing N of M` was off by one
  whenever a fallback search also hit the cap. The marker is now passed to
  `_cap_search_output` via `prefix=` instead: its bytes still count toward
  the cap and it always survives truncation, but it is excluded from the
  `N`/`M` entry count.
- `mcp install --repair`: a `chmod` / `chgrp` landing on the config while
  `--repair` was preparing its replacement could be silently reverted. The
  mode and group stamped onto the staged file came from their own `os.stat`,
  taken just before the metadata snapshot that the pre-replace
  compare-and-swap uses as "the original". A concurrent permission change in
  between was therefore already inside the snapshot — the re-check saw nothing
  changed and let the replace through, while the staged file still wore the
  values read a syscall earlier. `--repair` reported success and the change
  was gone. Both values are now derived from that single snapshot, so the
  staged file and the compare-and-swap baseline can no longer disagree
  (#287, closes #279).
- Test suite: disabled the `launch_testing` / `launch_ros` pytest plugins via
  `addopts` in `pyproject.toml`. When a shell has ROS2 sourced, `PYTHONPATH`
  pulls in those plugins' setuptools entry points, which conflict with
  pytest's built-in `caplog` handler registration and silently drop WARNING
  log records. That made `tests/test_metadata_required.py`'s 5
  `agent_family`-resolution tests fail nondeterministically outside CI
  (`caplog.records` came back empty even though the warning was logged to
  stderr), unrelated to any code under test. GitHub Actions never sees this
  since its runners don't have ROS2 sourced. (#290, closes #289)
- `search_memory`: when `search_mode` is left at its default `'and'` and that
  AND search returns zero results, the tool now automatically retries with
  `search_mode='or'` and prefixes the result with `(no AND match; fell back
  to OR)` when the retry finds hits. `recall_context`'s `and_or` default was
  already resilient to this; `search_memory` previously returned `"No
  matching memories."` for any natural-language query missing one term.
  Explicit `search_mode='or'`/`'and_or'` calls and the default value itself
  are unchanged (#285, closes #276).
- `get_memory_status` の直近7日集計 (#286, closes #280): 検索が `MAX_SEARCH` 上限に達し、かつ
  返却された最古の行がまだ7日窓の内側にある場合、セクション見出しを
  `family (last 7d) [PARTIAL: search limit … reached; …]` とし、各行を
  `family_7d <name>: >=N` の下限値表示に変えた（従来は打ち切られた件数を確定値の
  ように表示していた）。あわせて `created_at` の扱いを堅牢化: offset の無い naive
  timestamp は UTC とみなして比較する（従来は aware な cutoff との比較が
  `TypeError` になり、status 出力全体が `failed to read shared memory` に落ちて
  いた）、欠損・解析不能な値は1件ずつスキップ、窓は `[now-7d, now]` として未来
  日時を除外し、スキップ件数を出力に明示するようにした。
- tests: `tests/test_replication_subscriber.py` is no longer flaky. The suite
  published from a just-opened Zenoh session and then waited a fixed 0.4s for
  asynchronous delivery. A sample published before that session's declarations
  have propagated to the router is not delivered to the subscribers the router
  did not yet route to, and that notification is never re-sent, so the wait
  could never succeed under the conditions that first triggered this fix
  (single-host loopback zenohd, contended load) — measured there: the local
  index still had not seen such a sample 10s later, while a storage query on
  the same key answered. An independent re-measurement on a quieter,
  higher-core-count host could not reproduce that particular symptom (30
  runs, 0 failures with the canary handshake disabled), so how often the
  window is actually hit appears to depend on load and hardware, not only on
  the race existing. The sample is durable (it reaches the router's
  storage); what is lost, when the window is hit, is the live delivery to
  subscribers, i.e. the index update. Every fixed sleep in the file
  is now a wait on the condition the test actually cares about (row indexed /
  row gone / storage holds the key / both callbacks logged), and opening a
  remote session now re-publishes a canary until it is observed, so nothing
  under test is published before the path is known to deliver. A new test pins
  what the production one-shot CLI shape (lazy-open -> put -> close, as in
  `kioku-mesh save`) delivers to an already-established peer: both its live
  subscriber and the router's storage receive the sample. (#298)

- tests: the same fixed-sleep pattern is gone from the rest of the router-backed
  suite. `tests/test_gc.py` (42 sleeps), `tests/conftest.py`'s inter-test purge
  and `tests/test_e2e_sync.py` now wait on the condition each site depends on —
  obs readable / obs gone / tomb key stored / purge absorbed — with the waiting
  helpers extracted to `tests/wait_helpers.py` and shared with
  `test_replication_subscriber.py`. Assertions that something must *not* have
  been purged use a sentinel barrier instead of a sleep, and every store-session
  re-point in the E2E tests handshakes before publishing. The cross-router waits
  additionally check *which* router answered (replies queried with consolidation
  disabled, then matched on the replier zid), so "B holds a local replica" is
  verified rather than assumed from a reply A could have served. Measured over
  30 consecutive runs of the two files: 0 failures before and after,
  36.6s → 8.0s per run. (#301)

- tests: the same fixed-sleep pattern is now also gone from the rest of the
  suite. `tests/test_mcp_cli.py`, `test_mcp_server.py`, `test_store_single.py`,
  `test_visibility_write.py`, `test_mesh_embedded_router.py`,
  `test_local_backend.py` and `test_messaging_presence.py` now wait on the
  condition each site depends on, reusing `tests/wait_helpers.py`. Of the 71
  sleep sites audited across these files (`test_local_index.py` included), 64
  became waits (including 7 in `test_mcp_server.py` — post-put/post-tombstone
  ingest waits flagged by cross-review as having no behavioral reason to stay
  fixed sleeps — converted after the initial pass, retiring the
  `_INGEST_SETTLE` constant); 4 in `test_mesh_embedded_router.py` keep a fixed
  sleep (1 already inside an existing poll, 3 because no observable substitute
  condition exists, documented inline); 1 in `test_local_index.py` was removed
  as a no-op (`test_local_index_query_by_project_returns_recent` sets each
  observation's `created_at` explicitly before `upsert`, so the sleep between
  inserts created no clock gap — confirmed by 5/5 passing runs without it); a
  second `test_local_index.py` no-op sleep (`test_fts_bm25_ranking_and_tiebreak`,
  between the bm25-relevance pair) was found and removed the same way — both
  observations are constructed via `_mk_obs()`, fixing `created_at` at
  construction time, before the sleep ever runs, so the sleep never affected
  either timestamp (confirmed by 5/5 passing runs without it); and 1 remaining
  `test_local_index.py` sleep (same test, between the tie-break pair) still
  forces a real `created_at` clock gap — it runs *before* the second
  observation is constructed — for an observation whose timestamp is not
  explicitly set, unrelated to Zenoh declaration exchange, so it stays a fixed
  sleep. No flakiness was observed
  in either version over 30 consecutive runs of the 7 changed files (0
  failures before and after); the waits also cut the run time, 36.6s → 18.2s
  per run. (#302)

- `init --install-systemd`: when `zenohd` is not on `PATH`, the generated unit's
  `ExecStart` now checks `zenohd_install.default_bin_dir() / 'zenohd'`
  (`~/.local/share/kioku-mesh/bin/zenohd`) before falling back to the hardcoded
  `/usr/bin/zenohd` constant, and prints a notice indicating which binary was
  baked in (#292, closes #223).
- `backfill-metadata`: summary derivation no longer ends a sentence at a period
  inside an identifier (version numbers, filenames, IP addresses, dotted
  identifiers, decimals), nor at a numbered-list marker (`… 落とし穴 3 件: 1. Node
  v22 …` used to derive the summary `… 落とし穴 3 件: 1.`). An ASCII `.` / `!` /
  `?` now ends a sentence only when a space or the end of the text follows it,
  and a period closing a one-letter token (`e.g.`) or a digits-only token
  (`1.`, `12.`) is treated as a marker rather than a sentence end.

  Measured by auditing **every** dry-run target whose derived summary ends in an
  ASCII terminator while the content still has text after it — deliberately not
  the narrower "the next character is alphanumeric" check, which by construction
  cannot see a mis-split whose period is followed by a space. Over the 321
  repairable observations in the live store that audit surfaces 6 candidates
  before the fix (5 real mis-splits, 1 correct sentence end) and 2 after
  (0 mis-splits — both are correct first sentences whose content continues). (#284)

### Upgrade notes for v1.2

- `search_memory` / `recall_context` を呼ぶ側は、応答が 20,000 UTF-8 バイトを
  超える場合に結果が末尾から打ち切られることを前提にすること。打ち切りが
  発生すると末尾に `[truncated: showing N of M ...]` の通知行が付く。全件が
  必要な場合は `limit` を絞って複数回呼ぶか、`get_memory` で個別の
  observation_id を取得すること。単一の observation 自体が上限を超える場合は、
  ヘッダと `<id=...>` を保持したまま本文のみ UTF-8 安全に切り詰められる。
- 影響を受けるのは 1 回の応答が 20,000 バイトを超えるような大きめの検索・
  想起のみで、通常サイズの呼び出しの挙動は変わらない。
- `save_observation` を呼ぶ側で、content / subject / summary に MCP
  tool-call のマークアップ（`</content>` 等の閉じタグに save_observation の
  既知パラメータ名の開始/終了タグが続く形（間に空白・改行・タブが入っていても
  検出される）、または name に既知パラメータ名を持つ `<parameter name="subject">`
  のような `<parameter name="...">` 表記）を
  文中に引用している場合は ToolError で拒否されるようになった (#312, #314)。
  name の値が `...` や `parameter` のような非該当の文字列であれば拒否されない。
  該当箇所を「このタグを引用する」のではなく記述に言い換えれば通る。既存の
  非マークアップな通常 content には影響しない。

## [1.1.0] - 2026-08-08

> **このリリースは後方非互換の変更を含む (minor bump だが安全な更新ではない)。**
> `save_observation` (MCP) と `kioku-mesh save` (CLI) は `subject` / `summary` を
> 必須とするようになり、これらを渡していない既存の呼び出しはエラーになる。
> ADR-0029 の semver 契約ではこの変更は major bump に相当するが、利用者が
> 本人の環境のみであることからユーザー判断で minor とした。更新前に
> 下記 "Upgrade notes for v1.1" を必ず確認すること。

### Changed

- **BREAKING**: `save_observation` (MCP) と `kioku-mesh save` (CLI) で
  `subject` / `summary` を必須にした。空文字・空白のみに加え、`-` / `N/A` /
  `TBD` 等のプレースホルダも欠落として拒否する。ADR-0028 Phase5 の
  warn-only lint では効果が出ず、実データ 1058 件のうち subject 欠落 271 件・
  summary 欠落 282 件 (両方欠落 232 件) が蓄積していたため、警告期間を置かず
  即エラーとした。取り込み経路 (replication subscriber / index rebuild /
  `Observation.from_json`) には適用しない — 旧バージョンの peer から届いた
  payload を落とすとメッシュのデータが静かに欠落するため。CLI / MCP の
  後方非互換変更である。deprecation 期間・互換オプション・移行用の
  フォールバックは実装しない (単一ユーザー環境のため不要と判断)。
  ADR-0029 の semver 契約では major bump に相当する変更だが、利用者が本人の
  環境のみであることからユーザー判断で v1.1.0 (minor) として出す。
- **BREAKING**: MCP `save_observation` の `subject` / `summary` を既定値なしの
  引数にし、公開 inputSchema の `required` に含めた。あわせて欠落・
  プレースホルダ時の拒否を、通常の戻り値文字列ではなく MCP のツールエラー
  (`is_error=true`) として返すようにした。docstring に REQUIRED と書いても
  schema が optional のままでは MCP クライアントが引数を省略でき、拒否も
  「成功」として読まれて再試行されなかったため。
- ランチャ検出で異なる family のマーカーが同時に見つかった場合
  (Claude Code から起動された Codex 等) は、テーブル順で先頭を採らず
  `unknown` + 警告に倒すようにした。誤った family は無警告で信頼される分、
  検索性が落ちるだけの `unknown` より危険なため。あわせて `CODEX_HOME` を
  ランチャマーカーから外した — ユーザーが shell profile で export しうる
  設定パスであり、Codex CLI は子プロセスに渡さないため、検出に使っても
  誤ラベルにしかならない。`KIOKU_MESH_AGENT_FAMILY` の明示指定が最優先で
  ある点は従来どおり。
- `agent_family` / `client_id` の解決順を
  `KIOKU_MESH_*` → ランチャ検出 (`CLAUDECODE` 等) → `unknown` に変更した。
  `unknown` へ落ちる場合は「識別子の設定が壊れている」ことを警告として出す
  (従来は無言で `unknown` になっていた)。v1.0.0 で削除した旧 `MESH_MEM_*`
  は**読まない** — ADR-0029 の shim 削除方針は維持し、旧名が残っている
  クライアント設定は設定側で修正する。

### Added

- `kioku-mesh doctor` に identity チェックを追加した。MCP クライアント設定
  (`~/.claude.json` / `~/.codex/config.toml`) が identity を廃止済みの
  `MESH_MEM_AGENT_FAMILY` / `MESH_MEM_CLIENT_ID` だけで宣言している場合は
  **FAIL (exit code 2)** を返す。identity の解決順は `KIOKU_MESH_*` →
  ランチャ検出 → `unknown` で旧 prefix は一切読まれないため、この設定は
  「非推奨」ではなく実際に効いていないため。同じマッピング内に
  `KIOKU_MESH_*` の対応キーがあるときは現行キーが読まれるので報告しない。
  検出対象は identity の 2 キーに限定してある (`MESH_MEM_STATE_DIR` 等は
  このチェックの hint「`KIOKU_MESH_AGENT_FAMILY` /
  `KIOKU_MESH_CLIENT_ID` にリネームせよ」が当てはまらないため対象外)。
  あわせて、直近 50 件の観測のうち 8 割以上が `agent_family=unknown` の
  場合は WARN を出す。こちらは CLI からの保存など正当に unknown となる
  原因もあるため WARN に留めている。
  doctor は設定ファイルを書き換えず、観測のサンプリングも既存 index を
  read-only で開いて読むだけで、state ディレクトリ・DB・スキーマを新規に
  作らない。v1.0.0 (#266) で `MESH_MEM_*` の読み取りを削除した際、
  手書きの MCP 設定が旧 prefix のまま取り残され、5 週間・286 件の観測が
  `unknown` で保存され続けたことに気付けなかったため。

- `state_dir()` に `create=False` を追加した。パスの解決だけを行い
  ディレクトリを作らない。doctor のような read-only な呼び出し元が、
  何も無いホストを診断しただけで state ディレクトリや SQLite ファイルを
  作ってしまうのを防ぐため。既定は従来どおり `create=True`。

- 観測の失効フローを追加した (#272)。使い捨ての観測に寿命を持たせられるよう
  `save_observation` へ `expires_at` / `ttl_sec` を追加し、期限切れの観測は
  `recall_context` / `search_memory` の既定結果から外れるようにした
  (`get_memory` での id 指定参照は従来どおり可能)。あわせて掃除用の
  `kioku-mesh gc-observations` を追加した。期限切れ TTL・保持期間を過ぎた
  tombstone・同 shadow の 3 種を対象 id と理由付きで一覧表示する **dry-run が既定**で、
  実際の tombstone 化 / 物理削除には `--execute` (と確認) が必要。
  local index には `expires_at` 列を追加した (既存 DB は起動時に自動 migration、
  寿命を持たない既存行は無期限のまま)。

- `kioku-mesh backfill-metadata` を追加した。subject / summary が欠落した
  既存の観測を content から導出して補完する。既定は dry-run で、`--apply`
  を渡したときだけ書き込む。補完は **append-only** で行う: 元の観測を
  同じ `observation_id` / キーで上書きせず、導出した subject / summary を
  持つ新しい観測を新 ID で保存し、`supersedes` で旧観測に紐づける
  (ADR-0002 の immutable 契約 / ADR-0028 の append-only な SoT を維持する
  ため)。旧観測はディスク上に残り、ADR-0021 の supersede フィルタで検索
  結果から隠れる。identity (`agent_family` / `client_id` / `pc_id` /
  `session_id`) と `created_at` は元の観測から引き継ぐため、補完によって
  実行ホストへ帰属が移ったり recency の並びが変わったりしない。失敗は
  観測単位で、途中で失敗しても成功済みの追記は残り exit code は非 0 に
  なる。再実行時は既に superseder を持つ観測をスキップするため、重複した
  supersede は作られない。`agent_family` は観測のキー
  (`mem/obs/<family>/...`) の一部であり payload の書き換えでは修正できない
  ため、件数の報告と設定修正の案内のみを行い、書き換えはしない。

- 検索・recall 結果に書き込み元ホストの表示を追加した。メモリはメッシュ内の
  全ホストへ複製されるため、別 PC で保存された絶対パスや tmux pane 指定を
  現在のホストのものと誤認して引き継いでしまう問題があった。
  `recall_context` / `get_memory` は各エントリに
  `origin: <client_id> (this pc|other pc|unknown pc)` 行を出力し、
  `search_memory` は別ホスト由来のエントリにのみ
  `[origin: <client_id>, other pc]` サフィックスを付ける。判定は保存済み
  `pc_id` と現ホストの `get_pc_id()` の比較で行う (スキーマ変更なし)。
  あわせて MCP `_INSTRUCTIONS` に CROSS-PC ORIGIN 節を追加し、
  ホスト固有情報 (絶対パス / tmux pane / ポート / PID / 実行中状態) は
  origin ホストのものであり、ローカルで検証してから使うようエージェントに
  指示するようにした。

### Fixed

- 観測の失効フロー (#272) の Codex cross-review (PR #273) で指摘された
  blocking 3 件を修正した。(1) `gc-observations --execute` が確認画面に
  表示していない tombstone まで物理削除していた問題を修正した。候補一覧は
  local index 由来なのに対し実行側は表示済み id を使わず
  `gc_tombstones` / `gc_shadows` の global sweep を再実行しており、index に
  行を持たない orphan tombstone (raw store 側にのみ残る) まで巻き込むため、
  1 件の確認に対して 2 件削除されうる状態だった。gc API に `only_ids`
  境界を追加し、実行側は確認済み候補 id のスナップショットだけを処理する
  ようにした (orphan の掃除は従来どおり `kioku-mesh gc` の責務)。
  (2) shadow 済みかつ期限切れの行が TTL bucket と shadow bucket の両方に
  入り、TTL 側が先に tombstone 化することで `gc_expired_shadows` による
  再確認・revive の機会を奪っていた問題を修正した (TTL 候補 SQL に
  `shadowed_at IS NULL` を追加し bucket を排他化)。(3) 期限切れの観測が
  supersede 元の観測を隠し続け、既定検索から両方消えていた問題を修正した。
  supersede の存在判定が superseder の `deleted_at` / `shadowed_at` しか
  見ておらず `expires_at` を見ていなかったため、期限切れ superseder は
  それ自身が結果から外れつつ旧観測も隠したままになっていた。

- v1.0.0 で `MESH_MEM_*` 互換 shim を削除した際 (ADR-0029)、既存の MCP client
  設定 (`~/.claude.json` / `~/.codex/config.toml`) が旧名のままだと、以後の
  保存がすべて `agent_family=unknown` / `client_id=<user>@<host>` に**静かに**
  落ちていた問題に対処した。実データでも `unknown` 445 件のうち 404 件が
  v1.0.0 リリース月 (2026-07) に集中していた。旧名の読み直しはせず、
  (1) `unknown` へ落ちる際に必ず警告を出す、(2) Claude Code の `CLAUDECODE`
  等、ランチャが子プロセスへ渡すマーカーからの検出を追加する
  (`IdentitySource.DETECTED`)、の 2 点で対応する。Codex CLI の MCP
  subprocess にはマーカーが渡らないため、Codex 側は MCP 設定に
  `KIOKU_MESH_AGENT_FAMILY` を明示する必要がある
  (`kioku-mesh mcp install --client <client> --force` で更新できる)。
- `search_memory` / `recall_context` が同一 `observation_id` を複数回返す
  ことがあったバグを修正した。根本原因は `LocalIndex.upsert()` が
  `obs_fts` (FTS5) へ `INSERT OR REPLACE` していたが、FTS5 の仮想テーブルは
  `observation_id` に `UNIQUE`/`PRIMARY KEY` 制約を持てないため実質的に
  常に新規 INSERT として扱われ、同じ observation を再度 `upsert()` するたび
  (例: 自分自身の PUT が replication subscriber 経由でエコーバックされた
  場合) に `obs_fts` へ重複行が積み重なっていた。`upsert()` を挿入前に
  既存行を削除するよう修正し、加えて `LocalIndex.search()` に
  `observation_id` ベースの dedupe (limit 適用前に実施) を追加して、
  すでに重複行が溜まった既存 DB に対しても安全側に倒した。
- 上記修正の Codex cross-review (PR #270) で指摘された2件の major issue を
  追加修正した。(1) `LocalIndex.search()` の dedupe が固定倍率 (`limit * 5`,
  上限2000件) の over-fetch + Python側dedupe だったため、重複が無い通常検索
  でも公開契約上の上限 `MAX_SEARCH=10000` 件を返せず2000件で頭打ちになり、
  `get_memory_status` の `truncated` 判定・family/pc 集計も過少表示になって
  いた問題を修正した。FTS5 结合を伴う検索経路を
  `ROW_NUMBER() OVER (PARTITION BY observation_id ORDER BY rank)` を使った
  SQL側のdedupe(limit適用前に一意化)に置き換え、over-fetchの倍率に依存しない
  方式にした。(2) `LocalIndex.upsert()` の `obs_fts` DELETE→INSERTで、
  INSERT が `sqlite3.Error` を投げた場合に rollback していなかったため、
  DELETE 済みの中間状態がコミットされないまま残ってしまう問題を修正し、
  失敗時は明示的に `rollback()` するようにした。

### Upgrade notes for v1.1

- `save_observation` (MCP) / `kioku-mesh save` (CLI) を呼ぶ側は `subject` と
  `summary` を必ず渡すこと。省略・空文字・`-` / `N/A` / `TBD` 等の
  プレースホルダはいずれも保存されず、MCP ではツールエラー
  (`is_error=true`)、CLI では非 0 終了になる。既存の保存済みデータと、
  他ピアから複製されてくる payload には適用されないため、読み取り経路は
  影響を受けない。
- 既に subject / summary が欠落している観測は
  `kioku-mesh backfill-metadata` で補完できる (既定は dry-run、`--apply` で
  書き込み)。補完は append-only で、元の観測は書き換えずに新 ID の観測を
  `supersedes` で紐づける。
- MCP クライアント設定 (`~/.claude.json` / `~/.codex/config.toml`) が
  v1.0.0 で削除された `MESH_MEM_AGENT_FAMILY` / `MESH_MEM_CLIENT_ID` のまま
  残っている場合、`kioku-mesh doctor` が FAIL (exit code 2) を返すように
  なった。`KIOKU_MESH_AGENT_FAMILY` / `KIOKU_MESH_CLIENT_ID` にリネームするか
  `kioku-mesh mcp install --client <client> --force` で再生成すること。
- local index に `expires_at` 列が増える。既存 DB は起動時に自動 migration
  され、寿命を持たない既存行は無期限のまま残る。

## [1.0.0] - 2026-07-02

### Removed

- **BREAKING**: `KIOKU_MESH_LEGACY_WRITE_EMERGENCY` escape hatch を削除した
  (ADR-0029 PR 2)。この env var は無視されるようになり、legacy layout
  (`mem/obs/**`) への書き込みはできなくなった。移行手順は下記
  "Upgrade notes for v1.0" を参照。
- **BREAKING**: `KIOKU_MESH_LEGACY_READ_FALLBACK` escape hatch を削除した
  (ADR-0029 PR 3)。この env var は無視されるようになり、
  search / rebuild / replication subscriber / find-by-id のいずれも、
  設定値によらず legacy namespace (`mem/obs/**`, `mem/tomb/**`) を読まなくなった。
  `migrate-visibility` の明示的なスキャナと `doctor --check-legacy-namespace` は
  引き続き legacy namespace を読める (移行経路として維持)。移行手順は下記
  "Upgrade notes for v1.0" を参照。
- **BREAKING**: `mesh_mem` import compatibility shim (`src/mesh_mem/`) を削除した
  (ADR-0024 / ADR-0029 PR 4)。`import mesh_mem` / `from mesh_mem import ...` は
  `ImportError` になる。`import kioku_mesh` に置き換えること。
- **BREAKING**: `MESH_MEM_*` 環境変数の fallback (`KIOKU_MESH_*` が未設定のとき
  `MESH_MEM_*` を読む挙動) を削除した (`kioku_mesh.core._env_compat` ごと削除、
  ADR-0024 / ADR-0029 PR 4)。`MESH_MEM_*` は設定しても無視される。
  `KIOKU_MESH_*` にリネームすること。

### Versioning

- ADR-0029 の semver 宣言: `1.0.0` 以降、public CLI / MCP / Python API と
  on-disk schema は Semantic Versioning に従う。breaking change は
  semver-major bump または明示的な migration path を要する。

### Upgrade notes for v1.0

ADR-0029 が v1.0 のスコープと deprecation 手順を定義した。v1.0.0 へのアップグレード
前に以下を確認すること:

- `KIOKU_MESH_LEGACY_WRITE_EMERGENCY` は削除済み (このリリース以降、env var は無視される)。
- `KIOKU_MESH_LEGACY_READ_FALLBACK` は削除済み (このリリース以降、env var は無視され、
  `on` に設定しても legacy namespace は読まれない)。
- アップグレード前に `kioku-mesh doctor --check-legacy-namespace` を実行し、
  legacy obs/tomb が 0 件であることを確認する。
- legacy データが残っている場合は
  `kioku-mesh migrate-visibility --from legacy --to <user|team|mesh>` で移行する。
- 両方の env var を shell/systemd/MCP 設定から削除する。
- `mesh_mem` パッケージ経由の import を使っている場合は `kioku_mesh` に置き換える
  (`import mesh_mem` → `import kioku_mesh`)。
- `MESH_MEM_*` 環境変数を使っている場合は `KIOKU_MESH_*` にリネームする
  (例: `MESH_MEM_STATE_DIR` → `KIOKU_MESH_STATE_DIR`)。

## [0.8.0] - 2026-06-30

### BREAKING CHANGES

- visibility 未指定の `kioku-mesh save` / MCP `save_observation` / Python API が
  default で `mesh` に書き込まれるようになった (ADR-0019 Phase D / v0.8)。
  従来の legacy layout (`mem/obs/**`) への書き込みは
  `KIOKU_MESH_LEGACY_WRITE_EMERGENCY=on` でのみ可能 (v0.8.x 限定、v1.0 で削除)。
  migration: `kioku-mesh doctor --check-legacy-namespace` で既存 legacy データを確認後、
  `kioku-mesh migrate-visibility --from legacy --to <user|team|mesh>` を実行してください。

### Added

- `KIOKU_MESH_LEGACY_READ_FALLBACK=on`: legacy namespace (`mem/obs/**`, `mem/tomb/**`)
  からの読み取りをオプトインで有効化する env var を追加 (ADR-0019 Phase D PR(3))。
  デフォルト off — replication subscriber / rebuild scan / fallback search / find-by-id
  の全パスで legacy キーをスキップ。`on` 設定時は一回限りの WARNING ログを出力し、
  `kioku-mesh migrate-visibility` への移行を促す。v0.8.x 限定、v1.0 で削除予定。
- `doctor check_legacy_namespace`: legacy namespace (`mem/obs/**`, `mem/tomb/**`)
  に残存している未マイグレーション observation を検知する preflight check を追加。
  `visibility_migration.py` の selector を再利用し text/JSON 両出力対応。
  `--check-legacy-namespace` フラグで個別実行可能 (ADR-0019 Phase D PR(1))
- Added shadow visibility to `status` output (live/tombstoned/shadowed counts) and
  `doctor` check (`check_shadow_visibility`); added `list_shadowed_obs` to LocalIndex
  and invariant tests for INV-2/INV-3/INV-4/INV-5 (ADR-0028 Phase 1)
- ADR-0028 Phase3: `LocalIndex.inspect_by_id` による computed state
  (live/tombstoned/shadowed/superseded/physical-missing) の表面化。
  `get_memory`(MCP) および `get-memory`(CLI) のレスポンスに `state` フィールドを追加(additive)。
  CLI に `--include-hidden` オプション追加。既存インタフェース変更なし。
- Add `recall_context` MCP tool for additive filtered context recall with
  memory_types/source_files/references filters and grouped Markdown output (ADR-0028 Phase4)
- ADR-0028 Phase5: save-quality guardrails (`save_lint`) — warn-only validators
  (generic noise, missing subject, secret pattern) for CLI and MCP save_observation.
  MCP `save_observation` now returns a JSON string encoding
  `{observation_id, status, visibility, warnings}` (with optional `supersede_candidates`)
  instead of plain text. Compatibility note: MCP clients must parse the returned
  string as JSON to access individual fields.
  The CLI `save` command continues to return plain text `saved: <id> (visibility=...)`
  unchanged — no parsing required.
- docker compose で zenohd を起動可能にする `docker-compose.yaml` と設定ファイル
  (`config/zenohd.docker.json5`) を追加。`eclipse/zenoh:1.9.0` + `zenoh-backend-rocksdb`
  musl standalone を使用し、RocksDB を `./data/zenoh/` に永続化する (#253)

### Changed

- Phase D PR(4): `tests/conftest.py` の `KIOKU_MESH_LEGACY_READ_FALLBACK` global enable を解消し、
  legacy read が必要なテストのみ個別 monkeypatch に移行。ADR-0028 invariant / shadow テストの
  fixture を `visibility='mesh'` に統一 (cleanup only — behavior 変更なし)。
  README に visibility migration guide セクションを追加。

## [0.7.0] - 2026-06-26

**Theme: Full-text search additions and stabilization**

### Breaking Changes / Migration Required

- **Python パッケージ名を `mesh_mem` → `kioku_mesh` に rename (ADR-0024, #206).**
  `import mesh_mem` は後方互換 shim として `v1.0.0` まで動作しますが `DeprecationWarning` を発行します。
  `import kioku_mesh` に移行してください。
  CLI コマンド (`kioku-mesh`, `kioku-mesh-mcp`) および環境変数プレフィックス (`KIOKU_MESH_*`) は変更ありません。
  旧環境変数 `MESH_MEM_*` は `v1.0.0` まで fallback + `DeprecationWarning` で動作します。
  **移行手順**: `import mesh_mem` → `import kioku_mesh`、`from mesh_mem.xxx import yyy` → `from kioku_mesh.xxx import yyy`。
  `mesh_mem` shim は `v1.0.0` で削除予定。

### Added

- **`search_mode` parameter for `search_memory`: `'and'` / `'or'` / `'and_or'`** (#225).
  デフォルト `'and'`(全語必須)、`'or'`(いずれか1語)、`'and_or'`(完全一致 AND を優先し OR で補完)の3モードを
  `search_memory` MCP ツールおよび `search_observations` API に追加。

- **Incremental FTS rebuild + COUNT-mismatch guard** (ADR-0025, #228).
  reconcile / 起動時の rebuild_from_zenoh の FTS 処理を差分適用に変更し、rebuild コストを削減。
  差分適用後に COUNT(obs_fts) と live obs_index 件数(deleted_at IS NULL AND shadowed_at IS NULL)を比較し、
  不一致時のみ full rebuild にフォールバックする guard を追加。threshold(しきい値)概念は無い。

- **FTS5 trigram 全文検索 + supersedes-aware 検索** (ADR-0021, #204).
  `LocalIndex` に FTS5 virtual table (`obs_fts`) を追加し、日本語部分一致を含む bm25 ランキング付き全文検索を実現。
  3-stage fallback: trigram → 標準 FTS5 → LIKE。`obs_index` に `superseded_by` カラムを追加し、
  supersedes チェーンを辿って obsolete な記憶を検索結果から自動除外 (`include_superseded=False` デフォルト)。
  `doctor` コマンドで FTS5 capability を表示。`SCHEMA_VERSION` 2 → 3 前方 migration 付き。

- **`storage-level TTL purge` for messaging store** (#222).
  MCP tool `purge_expired_messages` と check_messages 経路での inline 自動 purge を追加。
  TTL 期限切れメッセージを storage レベルで削除し inbox の肥大化を防ぐ。

- **`migrate-visibility` subcommand**: safely move legacy `mem/obs/...` / `mem/tomb/...` keys into
  explicit visibility namespaces (`user`/`team`/`mesh`) via copy-verify-delete-repair ordering
  with mandatory backup/checkpoint (ADR-0019 Phase C, #226)

- **`zenohd install` subcommand**: auto-download zenohd + zenoh-backend-rocksdb with SHA-256
  checksum verification, arch/OS/libc detection, and PATH guidance (#221)

- messaging Phase 4 — MessageMemoryBridge で received message を save_observation に転送 (#185, #199)
- messaging Phase 3: tmux send-keys adapter (opt-in)。default off、exact pane/sender/scope allowlist、
  8 KiB size limit、retry+drop、注入 ≠ ack 契約 (#185, #198)
- messaging Phase 2: PresenceManager (30s heartbeat, 90s TTL, scope isolation) (#185, #196)
- messaging Phase 2: ZenohBridge (spool ↔ Zenoh put/sub, 64 KiB body limit) (#185, #196)
- messaging Phase 2: MCP tools `check_messages` / `ack_message` with server-side scope resolution (#185, #196)
- messaging Phase 1: Message/Ack schema, keyspace builder, local inbox spool, local ack index (#185, #194)
- Edge-case tests for short-term AND, double-quote escape, and whitespace-only query in `search()` (#213)
- test: supersedes-aware 隠蔽 x FTS full rebuild の回帰テスト追加 (#209)
- test: tmux adapter の 8 KiB boundary pass ケース追加 (#198)

### Fixed

- **`and_or` two-phase ordering in `_search_via_zenoh` fallback** (#229).
  Zenoh fallback 経路でも `and_or` モードの2フェーズ ordering (AND matches first、OR 補完を後方) を実装。

- zenohd/rocksdb download URL を upstream 実在形式に修正
  (zenoh- prefix、-standalone suffix、全 OS で .zip) (#221)
- SHA-256 検証を GitHub Releases API の digest フィールドを使う方式に変更
  (.sha256 companion は upstream に存在しないため) (#221)
- `include_expired=True` でのデバッグ閲覧が storage を破壊しないよう、lazy-delete を
  `if not include_expired:` の内側に移動。デバッグ閲覧は read-only になり、GC は
  `purge_expired_messages` MCP ツール / 通常の `check_messages` 経路に委ねる (#222)
- `purge_expired_msgs` のスキャン失敗を `(purged_count, scan_ok)` タプルで呼び出し側に
  伝達。MCP ツールはスキャン失敗時に `purge incomplete: scan failed (0 messages purged)` を返し、
  0件成功と区別できる (#222)
- Escape LIKE wildcard chars (`%`, `_`, `\`) in `search()` to prevent over-matching (#213)
- search(): 複数語クエリを AND 検索に修正。スペース区切りの各語を個別に評価し、
  3文字未満の語は LIKE フォールバックで補完 (#211)
- search_memory の語句検索が常に空を返すバグを修正。FTS5 テーブルを追加し、
  既存 index.db への冪等 rebuild を実装 (#207)
- FTS tags 表現の一貫化 + PEP604 型注釈統一 (#208)
- FTS follow-up bundle: rebuild skip-guard (R1)、tags edge-case tests (C1)、docstring (C2) (#212)
- bridge: `promote_hint` を strict bool (`is True`) 判定に変更、truthy 非 bool 値で昇格しない (#185)
- bridge: `save_fn` 例外時に `_promoted_ids` へ登録しない failure-path test 追加 (#185)
- Fix `_messaging_scopes()` to reject unknown visibility values (B1: prevents silent scope widening) (#195)
- Add defensive validation in `put_message()` for recipient kind and ID fields (#195)
- Add `scopes` field to `Presence.to_dict()` payload for consumer clarity (#195)

### Changed

- test: pytest-xdist 並列化導入、全テスト実行時間 90秒 → 21秒に短縮（-n auto、16コア）(#197)
- CI も pytest-xdist 並列実行に変更 (#197)
- messaging: keyspace key shape を `msg/{scope}/inbox/{session|agent}/{id}/{msg_id}` に統一 (#195)
- messaging: `Message` dataclass に direct delivery schema の first-class フィールドを追加 — `schema_version`, `kind`, `sender`, `recipient`, `body`, `content_type`, `requires_ack`, `delivery_adapters`, `correlation_id`, `_extras` (#195)
- messaging: local inbox index の Ack/dedup を recipient session 単位に変更 — `messages` table を `(msg_id, recipient_session_id)` 複合 PK に、`ack_message()` は未知 msg_id で `ValueError` を raise (#195)

## [0.6.0] - 2026-06-24

### Changed

- **`src/mesh_mem/` を ADR-0023 に従い `core/`・`memory/`・`messaging/`・`bridge/` の 4 層に分離 (Issue #186).** Zenoh セッション・mTLS・identity・keyspace・config 等のインフラ層を `core/` に、store・local_index・pending_queue 等の観測データ管理層を `memory/` に移動。`messaging/` と `bridge/` は現時点でスタブ枠のみ（ADR-0023 策定、実装は未着手）。既存の `src/mesh_mem/*.py` は `sys.modules` エイリアスとして残し、後方互換を保つ。`core` が `memory` を import しないことを `tests/test_layering.py` の AST 静的解析で担保。

## [0.5.0] - 2026-06-12

### Added

- **Readers for visibility-tiered namespaces (ADR-0019 Phase A, #177).** All read paths — the replication subscriber, the startup index rebuild, the legacy Zenoh fallback search / find-by-id, and the shadow-sweep re-verify — now cover the upcoming `mem/mesh/**`, `mem/user/{user_id}/**` and `mem/team/{team_id}/**` namespaces alongside the legacy `mem/{obs,tomb}/**`. A single broadened selector per kind (`mem/**/obs/**`; Zenoh's `**` matches zero or more chunks) covers every shape. A new `keyspace` module centralizes the key vocabulary. Writes are unchanged (legacy keys only — Phase B); upgrade all mesh hosts to 0.5.x before any host starts writing tiered keys.
- **Canonical-key gate on every index-mutating read path (Codex review on #177).** The broadened selectors also match key shapes outside the spec; a valid Observation payload under such a key could previously have polluted the local index. Every ingest point now requires the key to parse as a canonical kioku-mesh key and the payload `observation_id` to equal the key's trailing id — including the shadow-revive path in `gc_expired_shadows`, so a forged payload can no longer resurrect a shadowed row.
- **Session-scoped save block + nudge in `get_memory_status` (#158, #160).** The status output gains `session_age`, `this_session_saves`, `this_session_last_save_age` and a conditional machine-readable `nudge` (no saves after 10 min, or 20+ min since the last save). Counts are derived from the observation store, not process-local state, so they survive MCP server restarts. Server instructions re-define save triggers as language-agnostic semantic acts with EN/JA/ZH/KO anchors (#159), and an optional Claude Code hook (`scripts/hooks/check-unsaved-decisions.sh`) reminds about unsaved decisions on PreCompact / `/clear` (#161).

### Changed

- **`store.py` split into focused modules (#167; PRs #170, #171, #173, #174).** The 1,608-line monolith is now `pending_queue.py` (failed-put queue + drain worker), `transport.py` (Zenoh session lifecycle, retry policy, transport status), `replication.py` (rebuild policy, key parsing, replication subscriber) and `purge.py` (retention GC, shadow sweep, pc-scoped bulk purge), leaving a ~430-line read/write core. Pure refactor: `store.<name>` keeps working for every public symbol via façade re-exports. Note for test authors: the re-exports are plain aliases — monkeypatch internals on the owning module, not on `store` (#172, #175; the contract is documented in `store.py` and frozen by a test).
- **ADR-0019 visibility tiers renamed to `user` / `team` / `mesh` (#176).** The originally proposed `priv` (never leaves one host) inverted the primary use case — on a personal multi-PC mesh, "private" notes are exactly the ones that should sync across the owner's machines — and labeling personal data `pub` was misleading. Tiers are now named by reach; all three are Zenoh-backed (no more SQLite-as-source-of-truth exception); a machine-local tier is deferred until a concrete need appears. Docs only — ADR-0019/0020 remain Proposed.


## [0.4.1] - 2026-06-01

### Removed

- **BREAKING: `kioku-mesh init --mode localhost` removed (#151).** The `localhost` mode generated an ephemeral zenohd + in-memory volume that did not survive a router restart, and was a strict subset of features already covered by `--mode local` (persistent single-host, no zenohd) and `kioku-mesh mesh start` (ephemeral Zenoh smoke test, no zenohd binary needed). The `init` default changes from `localhost` to `local`. Migration: replace `kioku-mesh init` / `kioku-mesh init --mode localhost` with `kioku-mesh init --mode local` for persistent single-host use, or `kioku-mesh mesh start` for a throwaway Zenoh smoke test. The shipped `config/zenohd_localhost.json5` template is removed. Existing `~/.config/kioku-mesh/zenohd.json5` files generated by past runs continue to work — only the wrapper command changes.

### Added

- **`scripts/save_coverage.py`: objective metric for proactive-save adherence (#105).** A standalone, transport-agnostic tool that turns a JSONL trace of `opportunity` (a moment that should have been saved — bug fix, decision, discovery) and `save` (an actual `save_observation` call) events into a single number: `coverage = saved opportunities / total opportunities`. Greedy 1:1 matching within a configurable window (`--window-seconds`, default 1800s), with `--require-type-match` to gate on `kind`/`memory_type` agreement, `--json` output, and `--min-coverage` for CI gating (exits non-zero when the trace falls below the bar). Lives under `scripts/` (not the `mesh_mem` package) because it's analysis tooling, not part of the MCP server. Trace-collection paths (hook scripts, log scrapers) are intentionally out of scope here and tracked separately. See `docs/design/issue-105-proactive-save-opportunity-coverage.md`.

## [0.4.0] - 2026-06-01

### Added

- **Mutual TLS for the mesh via a CSR-based private CA (`kioku-mesh tls`).** Optional transport-level peer authentication for deployments where network admission (Tailscale / WireGuard / trusted LAN) is not enough on its own. A new `tls` command group provisions a small private PKI — `tls init-ca` creates the CA, `tls request --san <addr>` generates a peer key (which never leaves the host) plus a CSR, `tls sign` issues the certificate on the CA host, and `tls install` places the signed cert + CA cert into `~/.config/kioku-mesh/tls/`. `tls info` reports subjects, SANs, and expiry. Only non-secret material (CSRs, signed certs, the CA cert) is ever exchanged between hosts. `kioku-mesh init --mode <hub|spoke> --tls` then emits a `tls/`-scheme config with a `transport.link.tls` block (`enable_mtls`, `verify_name_on_connect`), refusing to run until the certs exist. `kioku-mesh doctor` gains a `tls_certs` check (WARN under 30 days to expiry, FAIL if expired or missing while the config enables mTLS). Keys are EC P-256; peer certs carry both serverAuth and clientAuth. Adds a `cryptography` runtime dependency. See [docs/mtls.md](docs/mtls.md).
- **Copy-paste enrollment + one-command `tls enroll` for mTLS provisioning.** Replaces the `scp` round-trip (`request` → scp → `sign` → scp → `install`) that proved fiddly in practice. `tls request` and `tls sign` now print a single armored, copy-pasteable block (`-----BEGIN KIOKU-MESH CSR/CERT BUNDLE-----`) to stdout that you paste into the next command on the other host — no SSH, no path juggling, works over any channel. The signed block bundles the peer cert *and* the CA cert, so the peer pastes one block instead of shuttling two files. `tls sign` / `tls install` also accept a file argument or `-o` file output for a move-one-file flow, and `tls install --cert/--ca` still accepts the original two-file form. Blocks carry only non-secret material; private keys never appear in one. For anyone with SSH to the CA host, the new `tls enroll <ca-host> --san <addr>` folds request → sign-over-SSH → install into a single command (with `--ssh-port`, `--ssh-opt`, `--remote-mesh`, `--days`). See [docs/mtls.md](docs/mtls.md).

### Fixed

- **`tls enroll` no longer clobbers an existing peer key when the remote signing step fails.** A failed enrollment previously overwrote the host's `peer.key`, breaking an already-working mTLS setup. The key is now only replaced once a valid signed certificate has been obtained. The remote command run over SSH is also properly quoted.

## [0.3.3] - 2026-05-30

### Changed

- **README overhaul for the OSS launch (PR #136, #137).** Documentation only — no code or on-disk storage-schema changes.
  - Lead with the differentiator — "Shared memory for AI coding agents, across tools and machines" — instead of generic persistence, and add a "Why kioku-mesh" section framing the cross-machine, multi-agent problem.
  - Add the project logo as the title and a 20s demo GIF (one agent saves a decision; an agent on another host recalls it live over the mesh).
  - Add Mermaid architecture and hub-and-spoke topology diagrams to the Multi-Host Mesh section.
  - Describe mesh mode as "Zenoh/RocksDB is the source of truth, each host's SQLite is a rebuilt local read cache", restate the trusted-network model, and note mTLS peer authentication is under consideration.
  - Tighten prose throughout.

## [0.3.2] - 2026-05-29

### Fixed

- **Fresh spoke no longer reports `count: 0` after joining a populated mesh (#133, PR #134).** `get_index()` now rebuilds the local SQLite index once when it is empty, even under the one-shot CLI's default skip (#38). A newly provisioned spoke that has already replicated rows into zenoh-rocksdb previously showed `count: 0` from `status` / `search` until `kioku-mesh --rebuild status` was run by hand, because the index is only backfilled by an explicit rebuild and the replication subscriber ingests only *new* writes. Explicit opt-outs (`set_rebuild_on_init_explicit(False)` / `MESH_MEM_SKIP_REBUILD=1`) are honored, and the populated-index fast path is unchanged.
- **`kioku-mesh init --install-systemd` against an existing config no longer demands `--force` or rewrites the config (#133, PR #134).** Installing the systemd unit on top of an already-provisioned `zenohd.json5` is now a pure add-on: the existing config is reused unchanged and only the unit is generated. `--force` still overwrites the config when explicitly requested.

### Added

- **`kioku-mesh init --mode spoke` now prints the post-start backfill step (#133, PR #134).** The completion output points at the one-time `kioku-mesh --rebuild status` so the empty-index `count: 0` state during onboarding is self-explanatory rather than looking like a failure.

## [0.3.1] - 2026-05-29

### Changed

- **On-disk paths renamed `mesh-mem` → `kioku-mesh` (#128).** `kioku-mesh init` now writes `~/.config/kioku-mesh/`, the state dir defaults to `~/.local/share/kioku-mesh/`, and `init --install-systemd` generates `kioku-mesh-zenohd.service`. **No automatic data migration:** when only a legacy `mesh-mem` directory exists, kioku-mesh reads it as before and prints a one-time warning nudging a manual `mv` (see `docs/migration.md`). The env-var prefix (`MESH_MEM_*`) and Python import name (`mesh_mem`) are intentionally left unchanged. This completes the brand consistency the v0.3.0 rename deferred.
- **"Tier 1" removed as a first-class architecture tier; rebranded as the demo path for `mesh start` / `mesh join`.** The README architecture table is now `Local` (SQLite, default) vs `Mesh` (zenohd + RocksDB, persistent multi-host); the in-process zenoh router (`mesh start` / `mesh join`) is documented as a "try mesh without zenohd" demo path, with the ephemeral cross-host replication caveat called out explicitly. Rationale: the Tier 0 → 1 → 2 progression broke monotonicity (Tier 1 loses cross-host persistence relative to Tier 0's local persistence), and Tier 1's only real use case is "evaluate mesh before installing zenohd" — first-class tier status overstated its value and increased onboarding cognitive load. CLI help for `mesh` / `mesh start` / `mesh join` updated accordingly. No code or runtime behavior change. See ADR-0013 for the full rationale.

### Added

- **`kioku-mesh init` now guides the path from single-host to multi-host mesh** (#96, PR #127): mode-specific follow-up hints after `init` (localhost → both scale-up paths; hub → a ready-to-run `--mode spoke` command pre-filled with the detected LAN IP; spoke → a hub-side `--listen` reminder), plus a full per-mode `--mode` description block (`RawDescriptionHelpFormatter`) and a "Picking a `--mode`" table in the README.

### Documentation

- README restructured (#125, #126): hero + table of contents + Roadmap section, a single end-to-end Quickstart, Operations folded under Power users, Contributing split out. Install guidance now leads with PyPI (#123).
- Added a migration guide for the on-disk path rename, including how to identify an environment-specific `zenohd` systemd unit before stopping it (#128, #131).

## [0.3.0] - 2026-05-25

### Renamed

- **PyPI distribution and CLI renamed `mesh-mem` → `kioku-mesh`**. The original
  name was rejected by PyPI's similarity check (collides with an unrelated
  `meshmem` AI-memory package). Internal artifacts deliberately preserved so
  existing users only swap the binary: on-disk paths (`~/.config/mesh-mem/`,
  `~/.local/share/mesh-mem/`), env-var prefix `MESH_MEM_*`, systemd unit name
  `mesh-mem-zenohd.service`, and Python import name `mesh_mem` are unchanged.
  (The on-disk paths and systemd unit name are renamed in a later Changed
  entry above — see #128; env-var prefix and import name remain `mesh_mem`.)
  See `docs/migration.md` for the `uv tool uninstall mesh-mem &&
  uv tool install kioku-mesh && kioku-mesh mcp install --force` upgrade.

### Fixed

- **Tier 1 mesh integration** (#112 post-merge fix): `mesh-mem mesh start` now starts an index subscriber within the router process, so peer saves published via `ZENOH_CONNECT` are written to the router's local SQLite index and visible from `mesh-mem search` in the router context. `mesh join` is now foreground (Ctrl-C to stop) and also starts a replication subscriber. Addresses post-merge review B1/B2/I1/I2/N1.
- **`mesh start` peer hint now shows real host IP** (B3 fix): when listening on a wildcard address (`0.0.0.0`), the startup message now auto-detects the host's LAN IP(s) and shows separate hints for same-host (`127.0.0.1`) and other-host connections. Previously the other-host hint showed loopback `127.0.0.1`, causing remote peers to connect to their own loopback.
- **`mesh-mem doctor` connected-peer count**: `check_embedded_router` uses `router_zids` from an external peer probe as an approximation; full peer enumeration requires in-process router state and is deferred to #113.
- **Forward-compatibility for Observation schema**: when a peer running
  an older release receives a PUT carrying fields it doesn't know about,
  those fields are now preserved via a `_extras` side channel and re-emitted
  on `to_json`, instead of being silently stripped on SQLite round-trip.
  Fixes silent data loss during rolling upgrades. (#75)
- **`mesh-mem-mcp` interactive misinvocation now exits with usage**
  instead of starting the stdio loop and flooding stderr with
  JSON-RPC parse errors. Set `MESH_MEM_MCP_ALLOW_TTY=1` to bypass
  the check for protocol-level debugging. (#98)

### Added

- **Tier 1 embedded zenoh router** (#112): `mesh-mem mesh start` opens an in-process zenoh router (`mode=router`) with configurable TCP listen endpoint (default `tcp/0.0.0.0:17447`) — no `zenohd` binary required. `mesh-mem mesh join <peer>` opens an in-process peer session and verifies connectivity. `mesh-mem doctor` reports embedded router reachability via `MESH_MEM_ROUTER_ENDPOINT` (default `tcp/localhost:17447`). Backend abstraction unchanged: no tier-specific branch in `store.put_observation()`.
- **Introduce `local` backend** (#109): `mesh-mem init --mode local` provisions a config that does NOT require `zenohd` on PATH. The existing SQLite store (`local_index.py`) is promoted from sidecar to first-class backend. Both the CLI and MCP server route through the same `MemoryBackend` abstraction so the demo path and the agent path are byte-for-byte the same code. Select with `mesh-mem init --mode local` or `MESH_MEM_BACKEND=local`. Unlocks: `mesh-mem demo` (#108) and issues #110–#113.
- **README adds an "Install zenohd" section** (#83) before the Quick start. Covers the apt one-liner (Eclipse Debian repo) plus the `zenohd --version` + rocksdb-backend-loaded verify steps so a Debian / Ubuntu first-touch user can get a working router without reading upstream Zenoh docs. macOS / Windows / non-apt Linux paths defer to [zenoh.io/docs/getting-started/installation](https://zenoh.io/docs/getting-started/installation/) rather than embedding fragile prebuilt-zip / cargo recipes that would drift out of sync with upstream. The existing `## Requirements` Zenoh 1.9 bullet cross-links to the new section.
- **`mesh-mem doctor` diagnostic command** (#84). Runs a small set of deterministic checks so first-touch users can answer "why isn't this working" without reading three separate README sections: `zenohd_binary` (PATH lookup), `config_file` (`~/.config/mesh-mem/zenohd.json5` present), `zenohd_reachable` (TCP probe to `ZENOH_CONNECT`, default `tcp/localhost:7447`), and `state_dir_hardlinks` (writable + POSIX hard-link capable, the same constraint `get_pc_id` relies on). Each result carries `status` (`pass`/`warn`/`fail`), one-line `summary`, actionable `hint`, and machine-readable `details`. Exit code reflects the worst severity (0/1/2) for scripting. `--json` emits `{ok, worst_status, checks: [{name, status, summary, hint, details}]}`. Process-owner discrimination, time-sync inspection, and MCP-client registration probes are deferred — they are platform-specific and easier to misdiagnose than to skip; the v0.3 scope is the testable core.
- **`client_id` defaults to `<user>@<host_short>` when env-unset** (#82). v0.3 onboarding: first-touch users no longer need to export `MESH_MEM_CLIENT_ID` for observations to carry an informative identity. `agent_family` keeps the `unknown` default — it's an aggregation axis where the cost of misclassification (e.g. labeling a non-Claude session `claude` because an env var happened to leak) outweighs the cost of an uninformative default. Launcher detection is deferred. `mesh-mem status` now shows the provenance of each identity value (`from MESH_MEM_AGENT_FAMILY` / `default — set MESH_MEM_AGENT_FAMILY to override`) so users can confirm what's being written. New `identity.IdentitySource` enum (`env` / `detected` / `default`) and `resolve_agent_family()` / `resolve_client_id()` helpers expose the value+source tuple. Identity segments are sanitized against Zenoh-reserved characters (`/ * ? $ #`) before they enter the key namespace.
- **`mesh-mem mcp install --client <claude-code|codex-cli>` for one-shot MCP registration** (#85). Removes the largest first-touch friction for AI-coding-agent users: instead of reading `docs/mcp-clients.md` and picking the right per-client recipe, one command bakes the absolute path to `mesh-mem-mcp` and sensible env defaults into the chosen client's config. Claude Code goes through `claude mcp add -s user ... -e ... -- <path>` (the only registration path Claude Code actually reads). Codex CLI gets a `[mcp_servers.<name>]` TOML block in `~/.codex/config.toml`, with idempotent block-level substitution that preserves other servers and user comments outside the block. Flags: `--name` (registry key, default `mesh_mem`), `-e KEY=VALUE` (env override; repeatable), `--dry-run` (print without executing), `--force` (replace existing registration). Claude Desktop, Gemini CLI, and ChatGPT Desktop are deferred (Claude Desktop pending #87 macOS / Windows verification; Gemini and ChatGPT Desktop pending stable upstream config schemas) — their manual recipes remain in `docs/mcp-clients.md`.
- **`mesh-mem init --install-systemd`** (#86). One extra flag on `init` writes a user-scope systemd unit at `~/.config/systemd/user/mesh-mem-zenohd.service` (XDG-aware) pointing at the same `zenohd.json5` the init step wrote, with the absolute `zenohd` path baked in so the user manager doesn't need shell PATH. `--print` extends to emit both bodies (config + unit) separated by a comment header so the user can split them. `--force` covers both files. The platform check refuses cleanly on macOS / Windows / hosts without `systemctl`. Missing `zenohd` binary degrades to a warning + documented fallback path rather than aborting — the unit is still installable, the user just edits ExecStart before enabling.
- **`Observation.references` field** for first-class PR / Issue / external identifiers (#73). CLI: `mesh-mem save --references "#67,PR#68"`. MCP: `references=["#67", "PR#68"]`. Old JSON without the field deserializes to `[]`.
- **Shell completion for the `mesh-mem` CLI** via `argcomplete` (#76). Install the new `completion` extra (`pip install -e '.[completion]'`) and run `eval "$(register-python-argcomplete mesh-mem)"` from `.bashrc` / `.zshrc`. Subcommands, static flags, and `--memory-type` complete from argparse metadata; `--project` / `--pc-id` / `--by-pc-id` use dynamic completers that read distinct values from the **local SQLite index only** (no Zenoh round-trip, no `rebuild_from_zenoh`), so tab-completion stays sub-100 ms even on populated meshes. `argcomplete` is optional — if it is not installed the CLI behaves exactly as before.

### Changed

- **MCP tool descriptions reinforced for proactive save**: `save_observation`,
  `search_memory`, and `get_memory_status` docstrings now carry per-tool
  PROACTIVELY reminders so the protocol stays active in long sessions where
  the server instructions may have been pushed out of the context window.
  `get_memory_status` output now includes `last_save_at` (ISO timestamp of the
  most recent index entry) to surface skipped saves as a self-check hint. (#51)

- **`mesh-mem delete` no longer aborts at 10 000 matches** (#66). The bulk-delete path now pages via a `(created_at, observation_id)` DESC cursor (`LocalIndex.search` gained an inclusive `until_iso` filter and a stable tiebreaker), tombstoning all matching rows regardless of size. `--batch-size` (default `1000`, max `MAX_SEARCH=10000`) controls per-page and progress granularity. Individual `put_tombstone` failures no longer abort the sweep — they increment a `failures` counter and the process exits non-zero only at the end. When the target set exceeds `MAX_SEARCH` the interactive prompt prints an extra warning suggesting `mesh-mem --rebuild gc --retention-days 0 --project ...` as the faster path when the rows live only in the local SQLite index (ADR-0010 / ADR-0011 shadow-sweep). The same hint is appended to stderr on every bulk-delete completion to discourage raw `DELETE FROM obs_index` workarounds.

- **MCP server instructions add an explicit SKIP list and type/importance guidance** (#73). PR/Issue lifecycle ticks, restated PR/ADR/CHANGELOG content, in-conversation progress logs, and bare `tests pass` notes are now called out as save-skip cases. `decision` / `bug` / `pattern` / `config` are preferred over `summary`; `importance` 1-2 invites reconsidering whether to save at all.
- **Docs: install guidance now leads with `uv tool install`.** README Quick start and `docs/mcp-clients.md` recommend `uv tool install git+https://github.com/h-wata/kioku-mesh.git` (or `--editable .` for a local checkout), which exposes `mesh-mem` / `mesh-mem-mcp` at `~/.local/bin/` without venv activation or full-path invocation. MCP registration examples updated accordingly. The manual `python3 -m venv ~/.venv/mesh-mem` flow is retained as a fallback. No code or runtime behaviour change.
- docs: README rewritten around v0.3 hero + Tier 0/1/2 narrative (#110)
- docs: README Power users section polish — Features ordering, internal anchors, mesh-specific doctor placement (#111)

### Documentation

- **README rewritten for v0.3 first-impression**: top-down structure with a
  one-paragraph pitch, demo placeholder, Wave 1-2 quick start (init / doctor /
  mcp install), and a "What you get" capabilities list. English-primary; Japanese
  sections clearly demarcated. (#89)
- **README "Status & known limitations" reframed as design scope** (#88): the section now leads with the LAN/VPN trusted-peer design statement, separates Versioning (SemVer commitment) from Operational notes (cold-era resync, gc broadcast, MAX_SEARCH cap), and keeps the "don't expose to untrusted networks" callout intact. No factual claims removed.

## [0.2.5] - 2026-05-19

### Added

- **`gc --retention-days` now sweeps shadowed index rows alongside tombstones**
  (#70). After #67 introduced shadow-delete, long-shadowed rows had no
  physical-removal path and grew the SQLite index forever. The retention
  sweep now collects shadow rows whose `shadowed_at` predates the same
  cutoff used for tombstones, **re-verifies each candidate against the
  live Zenoh state**, and either upserts the row back to live (false-
  shadow recovery — the upstream obs reappeared since the rebuild that
  flagged it) or physically deletes it (genuine expiry). The CLI driver
  additionally runs `rebuild_from_zenoh` before the sweep so that
  stale-but-not-yet-shadowed local rows enter the discovery branch on
  a one-shot `mesh-mem gc` invocation (CLI startup skips rebuild by
  default per #38). If the live query fails the sweep is skipped
  entirely — never delete on transport ambiguity. Output reads
  `retention N-day sweep: physically deleted {n} tombstones / {m} shadows (revived {k})`.
  Pass `--no-shadow-prune` to opt out (tombstone-only sweep, prior
  behavior; rebuild is also skipped in that branch). The shadow sweep
  is otherwise local-only — no Zenoh delete is issued for the purged
  half because the upstream key is already absent; other peers run the
  sweep independently and converge.

### Changed

- **All user-facing CLI / MCP runtime strings are now English** (#53). Previously
  `mesh-mem` CLI prints (`save`, `search`, `delete`, `status`, `drain`, `gc` and
  argparse `--help`) and MCP tool returns (`save_observation`, `search_memory`,
  `get_memory`, `delete_memory`, `get_memory_status`, `drain_pending_puts`)
  mixed Japanese and English. They are now uniformly English to match the
  already-English `logger.*` output and to keep MCP responses safely parseable
  by non-Japanese agents. The Japanese trigger phrase `"前にやった"` inside the
  MCP `instructions` field is preserved on purpose — it is a deliberate hint
  for recognizing Japanese user input. **Breaking** for any script that greps
  Japanese substrings from CLI or MCP output (e.g. `保存完了`, `削除`, `件数`).

### Fixed

- **Rebuild now reconciles SQLite index against Zenoh, not just appends to it**
  (#67). `LocalIndex.rebuild_from_zenoh` was add-only: it upserted whatever
  it saw in Zenoh but never pruned `existing - zenoh_set`. Combined with
  the subscriber gap fixed in #65, that left long-lived ghost rows after
  a peer purged keys on Zenoh while another peer was offline. Rebuild now
  marks `existing` rows that did not appear in the Zenoh scan as
  *shadowed* via a new `shadowed_at` column. Shadowed rows are hidden
  from `search` / `find_by_id` (same as tombstones) but a later upsert —
  including replay of the obs from Zenoh — clears the shadow and the row
  comes back to life. Tombstones remain stronger than shadows: applying
  a tombstone clears any prior shadow, and rebuild no longer overwrites
  an existing tombstone's `deleted_at` timestamp. Rebuild also skips
  writes for live rows whose `payload_json` is unchanged, avoiding WAL
  inflation on populated meshes (ADR-0007 / Issue #32).
- **`get_memory_status` exposes index visibility counts**. Output now
  includes `index_rows: live=N / tomb=N / shadow=N` so operators can see
  the read-path state, not just the Zenoh-scan totals.

### Added

- **`LocalIndex.mark_shadowed_missing` + `VisibilityCounts`**. New
  index methods backing the rebuild reconcile path. Schema migrates
  forward from v1 to v2 by adding a `shadowed_at TEXT` column;
  existing rows are treated as live until the next rebuild revisits
  them.

### Fixed

- **Replication subscriber now mirrors Zenoh DELETE into the SQLite index**
  (#64). `start_index_subscriber` previously parsed every `mem/obs/**` /
  `mem/tomb/**` sample as JSON and silently dropped DELETE-kind samples
  (empty payload → `JSONDecodeError` → DEBUG log). As a result, a
  `mesh-mem gc --by-pc-id ... --execute` (or any `session.delete` on an
  obs/tomb key) issued on one peer purged Zenoh storage and that peer's
  local index, but left ghost rows in every other peer's
  `~/.local/share/mesh-mem/index.db`, inflating `get_memory_status`
  counts and search hits. The subscriber now dispatches on `sample.kind`
  and calls `LocalIndex.physical_delete` for DELETE samples whose key
  ends in a 32-hex `observation_id`. Malformed keys (wrong length, non-
  hex, missing trailing segment) are conservatively ignored.

### Added

- **Local fallback queue for failed puts** (#50). `put_observation` /
  `put_tombstone` retryable failures are now persisted to
  `state_dir()/pending_puts.db` and replayed on the next successful save.
  `pending_puts` count is exposed in CLI `status` and MCP
  `get_memory_status`.
- **Startup and manual drain for pending puts** (#57). `mesh-mem-mcp`
  now starts a daemon background drain on startup when transport is
  reachable and queued `pending_puts` exist. Operators can also trigger
  replay explicitly via `mesh-mem drain --pending [--limit N]` or the MCP
  `drain_pending_puts` tool.
- **`mesh-mem search --format {text,markdown,json}`** (#58). Search now
  supports stable machine-oriented JSON output plus single-line markdown
  bullets suitable for SessionStart hooks, while preserving the previous
  human-readable text output as the default.
- **Sample Claude Code SessionStart hook script** (#58). Added
  `scripts/hooks/session-start.sh` plus README setup instructions for
  loading recent mesh-mem context into a new Claude Code session.

### Changed

- **`TransportStatus` schema gained `pending_puts: int`**. Callers that
  destructure the dataclass need to pick up the new field.
- **Drain progress is surfaced in status output**. CLI `status` and MCP
  `get_memory_status` now report whether a drain is in progress, the last
  drain timestamp, and the cumulative number of queued rows replayed by
  the current process.

## [0.2.4] - 2026-05-11

### Added

- **`mesh-mem gc --by-pc-id PCID [--session-prefix X] [--execute] [--yes]`**:
  bulk physical purge of every observation that was saved under a given
  ``pc_id``, optionally narrowed by ``session_id`` prefix. Use case: a
  benchmark / smoke run on a peer host saved tens of thousands of
  synthetic observations under throwaway sessions and they are now
  flooding the mesh. ``--execute`` is required to actually delete; the
  default is dry-run with a per-session histogram. With ``--execute`` the
  CLI also gates on an interactive ``yes`` prompt (skip with ``--yes``
  for CI / scripted use; non-interactive ``--execute`` without ``--yes``
  is rejected with exit 2 so an operator cannot pipe the command into a
  background job and have it auto-destroy). For every matched obs the
  mirrored ``mem/tomb/...`` slot is also exact-key deleted, so legitimate
  tombstones under the same ``pc_id`` are cleaned up at O(1)/match
  without falling back to the ``mem/tomb/**`` global sweep that
  ``--force-id`` performs (the sweep stalls on ``GET_TIMEOUT`` past 30k
  tombstones). Backed by ``store.scan_obs_by_pc_id`` +
  ``store.execute_bulk_purge``.

### Changed

- **CLI skips `rebuild_from_zenoh` on startup by default** (#38). The
  one-shot `mesh-mem` process previously paid the full ~15 s zenoh
  scan + JSON-parse + SQLite-membership-check on every invocation
  against a populated mesh, which made interactive use unworkable on
  busy peers (~117k records observed). The local SQLite index still
  converges via the replication subscriber within the process
  lifetime, so `save` / `search` / `get-memory` / `delete` / `status`
  see live writes without the rebuild. Long-running processes
  (`mesh-mem-mcp`, autonomous agents) keep the previous behavior —
  the rebuild cost amortizes across their uptime.
  Opt back in per-invocation with `mesh-mem --rebuild ...` or via the
  new `MESH_MEM_FORCE_REBUILD=1` env var. ``--rebuild`` uses the new
  explicit-override channel (codex review P2) so it outranks even an
  ambient ``MESH_MEM_SKIP_REBUILD=1`` exported from a shell profile or
  wrapper script — direct user intent on the typed invocation always
  wins over env-level config. Resolution order: explicit override >
  ``MESH_MEM_FORCE_REBUILD`` > ``MESH_MEM_SKIP_REBUILD`` > module default.
- **One-off migration scripts moved under ``scripts/migrations/``**.
  ``cleanup_legacy_memory_types.py`` (v0.2.2 → v0.2.3 enum migration)
  is operator tooling that should not be shipped as a CLI subcommand,
  but should still travel with the repo for any peer that has not yet
  migrated. The ad-hoc ``scripts/purge_observations_by_pc_id.py`` is
  removed in favor of the CLI flag above.

### Fixed

- **CLI commands no longer hang on exit** (#44). ``mesh-mem`` short-lived
  invocations now explicitly close the Zenoh session on exit (including
  on early returns / exceptions). Previously the session lingered until
  process teardown, which on some hosts left the CLI waiting on its own
  background tasks and made shell scripts that chain mesh-mem commands
  unusable.

### Performance

- **Project-scoped gc switches to the SQLite local index** (#32-A).
  ``gc_expired_tombstones(project=...)`` previously enumerated the entire
  Zenoh ``mem/tomb/**`` namespace (~60s on production data with months
  of test residue) regardless of how few tombstones actually matched the
  project. The new fast path queries the local index for
  ``(project, deleted_at)`` rows, then issues exact-key deletes — O(N)
  on the project-scoped subset, not O(M) on the global tombstone count.
  Always realigns the index via ``rebuild_from_zenoh`` before the SQLite
  query (codex review P1) — a non-empty sidecar from earlier short-lived
  CLI runs may be partial, and gating the rebuild on ``row_count() == 0``
  would silently miss older project tombstones. Falls through to the
  legacy global scan when the index is disabled
  (``MESH_MEM_DISABLE_INDEX=1``) or the fast path raises.
- **SQLite WAL bounded checkpoint policy** (#32-B). Long-running
  ``mesh-mem-mcp`` processes hold the index connection open
  indefinitely, which blocks SQLite's automatic checkpoint from
  completing the truncate phase — observed WAL grew to 130 MB (≈ same
  size as the main DB) on a host that had been writing for weeks.
  ``LocalIndex`` now issues an explicit
  ``PRAGMA wal_checkpoint(TRUNCATE)`` every 256 upserts and once on
  ``close()``, keeping the WAL bounded without introducing a
  background thread.

### Documentation

- **README Windows quick start refreshed** (#36): drop the misleading
  `pip install mesh-mem` line (the package is not on PyPI yet); call
  out the user-local zenohd install path for non-admin (`%LOCALAPPDATA%\Programs\zenoh\`)
  alongside the admin `Program Files\zenoh\` path; pin the exact zip
  asset names (`zenoh-1.9.0-x86_64-pc-windows-msvc-standalone.zip` and
  the rocksdb backend equivalent) so users stop guessing among the four
  naming patterns; clarify that `New-NetFirewallRule` needs an elevated
  PowerShell and that outbound-only peers can skip the inbound rule
  entirely; document the venv path-style mapping
  (`~/.venv/mesh-mem/bin/<bin>` → `Scripts\<bin>.exe`); cross-link the
  new `--rebuild` opt-in (#38) for first-time alignment on a populated
  mesh.
- **Spec.md, ADR-0006..0009, hub-and-spoke topology PoC report**
  (#41–#43): canonical specification, four new ADRs (hub-and-spoke
  topology, SQLite local index sidecar, project-aware O(N) gc, MCP
  server instructions protocol — supersedes ADR-0003 and ADR-0005),
  and an empirical 3-PC topology verification report.

## [0.2.3] - 2026-05-08

### Added

- **`memory_type` is now validated against a closed enum**
  (`note`, `decision`, `bug`, `pattern`, `config`, `summary`, exposed as
  `mesh_mem.models.VALID_MEMORY_TYPES`). The MCP `save_observation` tool
  returns a friendly error string and refuses to persist when an LLM
  passes an out-of-enum value (regression introduced when the v0.2.2
  PROACTIVE SAVE protocol shipped without a corresponding type guard);
  the CLI's `--memory-type` choices are derived from the same constant.
  ``Observation.from_json`` clamps unknown values from peers to
  ``"note"`` with a WARNING log, preserving forward-compat with peers
  on a future-extended schema.
- **README §"Non-interactive smoke from `claude -p`"**: documents the
  `--permission-mode bypassPermissions` flag required for MCP tool
  calls in `-p` mode (without it, the first tool call lands in
  `permission_denials` and the LLM exits with "permission needed").
  (#34)

### Changed

- **CLI `--memory-type` choices narrowed.** v0.2.2 accepted
  `note / decision / bugfix / discovery / config / pattern / fact /
  status / learning`; v0.2.3 accepts only the canonical six listed
  above. New `mesh-mem save --memory-type bugfix` (or `discovery` /
  `fact` / `status` / `learning`) is now rejected by argparse.
  Existing observations on the mesh whose `memory_type` is one of
  the dropped values continue to display unchanged — this is a
  write-side restriction, not a read-side one.
- **`README` Windows host setup marked Experimental** and now opens
  with a "WSL2 strongly recommended" callout. Native Windows is not
  in CI; the section remains for the rare cases (e.g. Claude Desktop
  on Windows) where WSL2 is not an option. (#36 partial — sub-points
  1, 2, 4 still open.)

### Fixed

- **Issue #31: index subscriber non-JSON payloads no longer log at
  WARNING.** `gc` broadcast-purge and similar control payloads can
  arrive on `mem/obs/**` / `mem/tomb/**` with non-Observation bytes;
  the subscriber now catches `JSONDecodeError` specifically and logs
  at DEBUG, while other exceptions continue to log at WARNING. A new
  unit test asserts no WARNING is emitted for non-JSON payloads.

## [0.2.2] - 2026-05-08

### Added

- **MCP server now ships a PROACTIVE SAVE protocol** via FastMCP's
  `instructions=` field. Claude Code (and any MCP host that surfaces
  `initialize_result.instructions`) now sees the trigger list —
  decision / bug / discovery / pattern / config / feature /
  preference / session summary — on connect, so coding agents
  auto-call `save_observation` without per-project CLAUDE.md tweaks.
  Previously the tool was registered but had no in-band signal
  telling agents *when* to use it, so dogfooding fell back to manual
  saves only. A smoke test pins the protocol so future refactors
  can't silently drop it.
- **GitHub Actions CI** (`.github/workflows/ci.yml`) running pre-commit
  and `pytest tests/` on `ubuntu-24.04` with Python 3.12, triggered on
  every PR and on push to `main`. (#22)
- **Claude Code Action workflow** (`.github/workflows/claude.yml`)
  posting an automated AI review on every PR and responding to
  `@claude` mentions in comments. Requires `ANTHROPIC_API_KEY`
  repo secret to be set by the maintainer. (#23)

### Changed

- **`_search_via_zenoh` filter evaluation order is now test-locked at
  the internal-state level**: a unit test asserts that an item which
  matches `keyword` but fails `project` is never registered in
  `results_by_id`, catching a regression that final-result inclusion
  tests would miss. The first of the existing filter-order tests had
  its docstring re-aligned with the assertion. (#13, Codex review
  IMPORTANT 2 follow-up)
- **`scripts/smoke_5peer_mesh.py` `_cli_search_count()` now raises** on
  non-zero exit rather than collapsing the failure to `0`. This
  separates replication zero-result from CLI / transport failure and
  makes flaky-test triage tractable. (#14, Codex review IMPORTANT 5
  follow-up)
- **`scripts/smoke_5peer_mesh.py` `_start_router()` closes the parent's
  log-file handle** once `subprocess.Popen` has duped the fd into the
  child, freeing Windows from the open-handle delete-block during
  cleanup. (#15, Codex review IMPORTANT 7 follow-up)
- **`scripts/smoke_5peer_mesh.py` `_cli_save()` parsing now anchors on
  a 32-char hex `observation_id` regex** instead of `split()[-1]`, so
  adding a trailing summary line to the save CLI output no longer
  silently corrupts the smoke. (#18, Codex review NICE-TO-HAVE 3
  follow-up)
- **`scripts/smoke_5peer_mesh.py` Phase 1 connectivity check now raises**
  on missing links instead of printing-and-continuing, so a partial
  mesh fails fast in Phase 1 rather than producing confusing results
  in Phase 2/3. (#17, Codex review NICE-TO-HAVE 2 follow-up)

### Removed

- **Claude Code Action workflow** (`.github/workflows/claude.yml`,
  introduced in #23) removed. In the current dev flow Claude is
  already involved in authoring most diffs, so a same-model
  auto-review on the merged result added little independent value;
  cross-vendor review (Codex) is run manually for high-stakes
  changes instead. (#26)

### Documentation

- **`state_dir()` clarifies `MESH_MEM_STATE_DIR=''` semantics**: an
  empty string is treated as "not set" and falls through to the per-OS
  default. v0.2.0 interpreted an empty string as the current working
  directory; v0.2.1+ does not. Use `MESH_MEM_STATE_DIR=.` to keep the
  cwd-relative behavior. A unit test pins this fallback. (#16, Codex
  review NICE-TO-HAVE 1 follow-up)

## [0.2.1] - 2026-05-02

### Added

- **5-peer mesh config template** (`config/zenohd_peer.json5.template`)
  with `{SELF_IP}` / `{PEER_N_IP}` placeholders, plus a 5-host walkthrough
  (`config/peers/example_5peer.md`) including sample IPs, ufw / iptables
  rules, and verification commands. (`2a6beae`)
- **README "Multi-agent identity" section** explaining how to run
  multiple Claude Code / Codex / autonomous agents on a single host
  using distinct `MESH_MEM_CLIENT_ID` values, with naming conventions,
  `direnv` examples, and MCP harness env-block configuration. (`2a6beae`)
- **README "Multi-host mesh setup" section** documenting N-peer setup
  steps (topology, per-peer config, firewall, boot, verify) with a
  troubleshooting table. (`2a6beae`)
- **README "Windows host setup" section** covering zenohd install,
  NSSM service registration, `New-NetFirewallRule` for TCP 7447, and
  `w32tm` time-sync verification. Documentation only; the project has
  not yet field-tested a mixed-OS LAN/VPN deployment. (`3fe7161`)
- **`mesh-mem status` `mesh_ready` field** reporting `yes` once the
  local node has at least one successful peer probe and has been up
  for the minimum settle time (~5 s warm in the localhost smoke; cold-era
  catch-up may take longer). Informational only; no API change. (#8, `63c2907`)
- **5-peer mesh smoke test** (`scripts/smoke_5peer_mesh.py`) that runs
  five zenohd routers **on localhost**, verifies 100/100 observation
  propagation in Phase 2, peer-restart convergence in Phase 3, and
  latency p50/p99 in Phase 4. Two consecutive runs PASS; this validates
  the wiring at peer count 5, not real LAN/VPN deployment. (`a93681d`,
  `6bfd0a9`)

### Changed

- **`state_dir()` now resolves per-OS**:
  Linux keeps the fixed `~/.local/share/mesh-mem` path
  (`XDG_DATA_HOME` is intentionally NOT honored to preserve
  pre-v0.2.1 behavior and avoid a silent migration for users who
  set it). macOS uses `~/Library/Application Support/mesh-mem` and
  Windows uses `%LOCALAPPDATA%\mesh-mem` via `platformdirs`. The
  `MESH_MEM_STATE_DIR` environment-variable override is unchanged
  on all OSes. New runtime dependency: `platformdirs>=4.0`.
  macOS / Windows users who previously placed data outside the new
  default location should set `MESH_MEM_STATE_DIR` before the first run
  after upgrade to keep using the existing store; otherwise an empty
  store is created at the new default and the old data remains
  untouched at the previous path. (`3fe7161`, `3325109`; Codex review
  BLOCKER fix — Linux silent migration when `XDG_DATA_HOME` is set)
- **`smoke_5peer_mesh.py` cleanup hardened**: routers we started are
  terminated by PID first, with a `cmdline`-verified port lookup as
  fallback only for orphan `zenohd` processes — closing a TOCTOU
  where a port reuse during cleanup could SIGKILL an unrelated
  process. A shared `_wait_for_rocksdb_lock_to_disappear()` helper
  raises `RuntimeError` from both `_graceful_stop_router()` and
  `_cleanup_smoke_processes()` when the RocksDB `LOCK` file persists
  past the deadline, surfacing a hung previous `zenohd` instead of
  silently continuing. Idempotent reruns no longer leave residual
  rows nor risk killing an unrelated process. (Codex review BLOCKER
  + IMPORTANT; `6bfd0a9`, `79694b2`, `1b81e7a`)

### Fixed

- **`search_observations` Zenoh fallback filter order is now
  test-locked**: tombstone → project / identity → since → keyword.
  This eliminates the post-restart "`--project` returns 0 while empty
  keyword returns rows" race observed when `MESH_MEM_DISABLE_INDEX=1`
  is in effect. The SQLite-first read path (v0.2.0 default) was
  already race-free via `PRIMARY KEY` deduplication and indexed
  filtering. (closes #8, `63c2907`)

- **`mesh_ready` no longer hangs on an empty store**: a successful
  zero-reply probe is now treated as ready, so freshly initialised
  deployments stop reporting permanent `waiting` in `mesh-mem
  status`. (Codex review IMPORTANT, `9e61871`)
- **`scripts/smoke_5peer_mesh.py` no longer hardcodes a developer
  home directory**: the result YAML path is configurable via
  `--result-yaml` and the script is documented as POSIX-only.
  (Codex review IMPORTANT, `9e61871`)

### Added (test deps)

- **`PyYAML>=6.0`** added to the `test` optional-dependency, fixing
  the missing dependency that would otherwise break the 5-peer smoke
  runs in clean environments. (Codex review IMPORTANT, `79694b2`)

## [0.2.0] - 2026-05-01

### Added

- **SQLite local index sidecar** for fast observation search. Populated on
  every `save_observation` / `put_tombstone` and rebuilt from Zenoh-RocksDB
  on startup. Keeps results consistent after restart and cross-host
  replication. (#7, 8b06c14 / 73e8ba2 / f195cd5)
- **Tier-4 benchmark** verifies `search_observations` stays sub-200 ms at 50k
  observations (6.47 ms p50 limit=1000, 352× faster than Tier-3 baseline).
  (#7, c06f0b0)
- **Observation schema extended** with six optional structured fields:
  `memory_type`, `importance`, `subject`, `summary`, `source_files`,
  `supersedes`. All fields default to backward-compatible values; old
  observations decode correctly with `from_json`. (#9, 7a5ccd3 / 469a516)
- **MCP tool `save_observation`** accepts the six new structured fields
  (all optional). (#9, 469a516)
- **MCP tool `get_memory(observation_id)`** returns the full record including
  all structured fields. (#9, 469a516)
- **CLI `mesh-mem save`** accepts `--memory-type`, `--importance`, `--subject`,
  `--summary`, `--source-files`, `--supersedes`. (#9, b4e9fc0)
- **CLI `mesh-mem get-memory <id>`** fetches a single observation by full
  32-char ID. (#9, b4e9fc0)
- **CLI `mesh-mem gc --project <name>`** scopes retention sweep to one project,
  preventing accidental cross-project tombstone deletion. (#11, 2faad5b)
- Issue #8 reproduction script (`scripts/repro_issue_8.py`) with 2-router
  localhost configs; reveals Zenoh routing behavior and a latent
  `observation_id` deduplication gap. (#8, b06661f)
- ADRs 0001–0005 documenting PoC design decisions (transport choice,
  tombstone semantics, filter strategy, identity env, gc scope).
- `config/zenohd_localhost.json5` for single-host development without LAN
  peers. (`844f1a3`)
- Systemd drop-in override example `docs/systemd-zenohd-override.example.conf`
  for auto-starting zenohd via the apt-packaged unit. (#3, `c4cfaee`)
- `fastmcp` added as a `test` extra dependency enabling MCP smoke tests.
  (#4, `e5768c4`)
- DR 24h test writer script `scripts/run_dr_writer.sh`. (`4aca9f1`)
- Benchmark script `scripts/bench_bulk_save.py` (Tier-1/2/3). (`66e0a08`)

### Changed

- **`search_observations` / `find_observation_by_id` now read from the
  SQLite local index by default.** Latency at 50 k observations: sub-200 ms
  (was 2.2 s at 16 k with the full Zenoh scan). Set `MESH_MEM_DISABLE_INDEX=1`
  to revert to the Zenoh full-scan fallback. (#7, f195cd5)
- **`search_memory` (MCP) and `mesh-mem search` (CLI) output format** now
  shows `[memory_type][importance] created_at (project) subject` on line 1
  and `summary` (or `content[:80]`) on line 2, separated by `---`. (#9)
- **Default `limit` unified to 50** across CLI (`mesh-mem search`), MCP
  (`search_memory`), and API (`search_observations`). Previously 20 for
  CLI/MCP and 50 for API. (#1, `c0f5194`)
- `ZENOH_BACKEND_ROCKSDB_ROOT` default path aligned to
  `~/.local/share/mesh-mem` (was `~/.local/share/zenoh-mem`). (#2, `2a39ff5`)
- `config/zenohd_home.json5` and `config/zenohd_office.json5` LAN IP
  placeholders reverted to `192.168.3.x / 192.168.3.y`; hardcoded deployment
  IPs removed. (`36c12b7`)

### Fixed

- **Default search `limit` unified** to 50 across all interfaces. (#1, `c0f5194`)
- `search_observations` zenoh fallback path now deduplicates results by
  `observation_id`; the SQLite-first path was already deduplicated by
  `PRIMARY KEY`. Surfaces in multi-router topologies. (#12, `8cb0f54`)
- `test_search_respects_since_iso_filter` pinned to a fixed `created_at`
  value, eliminating CI clock dependency. (`40b1fe9`)

### Documentation

- README `## Time sync` section expanded: `chrony` installation, `chronyc
  tracking` / `chronyc sources -v` / cross-host `date -u` verification,
  `timedatectl` warning (12.75 s drift observed with `synchronized: yes`),
  `chronyc makestep` recovery, and links to NTP skew PoC results. (#10, `69cc40b`)
- `plan.md` and `README.md` synced to as-built state: Observation schema,
  MCP/CLI signatures, PoC verification results summary, and open issues
  section. (`88e2019`)

### Security

- Replaced hardcoded LAN IPs in zenohd config templates with
  `192.168.3.x / 192.168.3.y` placeholders to avoid leaking
  deployment-specific addresses. (`36c12b7`)

---

## [0.1.0] — 2026-04-24

Initial tagged release. Experimental / early preview.

### Added
- Python package `mesh-mem` (entry points: `mesh-mem`, `mesh-mem-mcp`).
- CLI subcommands `save`, `search`, `delete` (logical / tombstone), `status`,
  and `gc` (physical delete: `--retention-days` sweep, `--force-id` emergency
  purge).
- FastMCP-based stdio MCP server exposing `save_observation`, `search_memory`,
  `delete_memory` (tombstone), and `get_memory_status`.
- Zenoh 1.9 transport with RocksDB storage backend; replication via
  `zenohd` mesh.
- E2E tests covering save/search, split-brain / reconnect sync, tombstone
  emission, and physical gc. FastMCP in-process and subprocess smoke tests.
- Documentation: quick start, MCP registration (Claude Code via
  `claude mcp add`, Claude Desktop, Gemini CLI, Codex CLI), systemd user
  unit, firewall (ufw / iptables) recipe, time-sync requirement, retention
  cron, and an emergency purge runbook.
- `LICENSE` (MIT, Copyright © 2026 h-wata) and
  `pyproject.toml` metadata (`license`, `license-files`, `authors`,
  `keywords`).

### Verified
- Local single-host topology (`config/zenohd_localhost.json5`): zenohd
  starts, RocksDB backend persists, CLI and MCP both round-trip.
- Two MCP clients on the same host (Claude Code + Codex CLI) share a
  single zenohd: an `observation` saved by one client is visible to
  `search_memory` from the other.

### Security
- Replaced hardcoded LAN IPs in `config/zenohd_home.json5` and
  `config/zenohd_office.json5` with placeholders (`192.168.3.x` /
  `192.168.3.y`) to align with README guidance and avoid leaking
  deployment-specific addresses.

### Known limitations
- No transport-level authentication or encryption; LAN-only.
- MCP transport is stdio only — web apps (`claude.ai`, `chatgpt.com`) are
  not supported in this release.
- Real two-host (Home ↔ Office) LAN deployment is documented but not yet
  field-tested.
- Search is a scan over up to `MAX_SEARCH=10000` observations; there is
  no full-text index yet.
- `gc --force-id` broadcast is best-effort; missed replicas catch up on
  their next `gc --retention-days` run.
