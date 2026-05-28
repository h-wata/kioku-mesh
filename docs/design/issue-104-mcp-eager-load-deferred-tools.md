# MCP client 側の deferred save tool の activation energy を下げる (#104)

Date: 2026-05-28
Refs: PR #103, ADR-0009, Issue #105, worker4_design.yaml (D51-1)

## 問題

kioku-mesh の `save_observation` のような「proactive に呼んで欲しい tool」は、
現状の MCP client（Claude Code 等）で **deferred / lazy ロード対象**になっている
ことが多い。LLM が「保存しよう」と思った瞬間に、ツールスキーマを明示ロードする
（例: Claude Code の `ToolSearch` で deferred-tools を `select:` する）1 ステップを
挟む必要があり、**activation energy が高い**。長セッションでは server instructions
が context から押し出されることもあり、この一手間が「保存をやめる」方向に効く。

PR #103 と関連設計レビューで、この deferred 挙動は **client 側の機構**であり、
**mesh-mem server からは制御不可能**であることが確認された。MCP protocol（2025 時点）
には tool 向けの always-load / eager-load を要求する属性が無い。登録済み tool は
常に `tools/list` に出るが、それを context に常駐させるか deferred にするかは
client の裁量である。

## スコープ外

mesh-mem server 側の変更。これは MCP / client エコシステムへの **提案**と、
**今すぐ使える回避策のドキュメント化**である。

> 注: 本タスクの GitHub 操作は `h-wata/kioku-mesh` に限定されているため、上流
> （`modelcontextprotocol` spec / Claude Code）への issue 起票は本リポジトリからは
> 行えない。本書は **起票可能な提案ドラフト**として in-repo に残す。

## 提案 1 — MCP spec の `ToolAnnotations` に load hint を追加

MCP の tool には任意の `annotations`（`ToolAnnotations`: `title`,
`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`）がある。
これらは **client への hint であって保証ではない**という位置づけ。ここに保存系
tool が必要とする「常駐ヒント」を追加するのが最小の spec 拡張になる。

提案する追加プロパティ（いずれか / 両方）:

- `eagerHint: boolean` — 「この tool はユーザ明示なしに proactive に呼ばれる設計。
  client は可能ならスキーマを常駐させ、deferred ロード対象から外すことを推奨」。
- `loadPriority: "eager" | "lazy"`（enum 版）— 段階表現が必要な場合。

ポイント:

- 既存 hint と同様に **MUST ではなく SHOULD/MAY**。client は無視してよい
  （後方互換）。
- server はこの hint を出すだけで、強制はしない。ADR-0009 の「規約は server が
  配るが、常駐可否は client が決める」という責務分界と整合する。
- 既存の `ToolAnnotations` への追加なので、protocol のメッセージ構造を変えない。

## 提案 2 — client（Claude Code 等）に「特定 tool を deferred から外す」設定

spec 拡張を待たずに client 単独でできる緩和。Claude Code 等の MCP client 設定に、
**サーバ単位 / tool 単位で eager-load を pin する**経路を増やす提案:

```jsonc
// 例: client 設定（提案するイメージ。現状この key は存在しない）
{
  "mcpServers": {
    "mesh_mem": {
      "command": ".../kioku-mesh-mcp",
      "eagerTools": ["save_observation"]   // deferred 化せず常駐させる
    }
  }
}
```

- ユーザがコストを納得した上で、頻繁に使う proactive tool だけ常駐させられる。
- spec 側の `eagerHint`（提案 1）が入れば、client はそれを既定の入力にできる。

## 今すぐ使える回避策（client 設定変更なしで効くもの）

提案が上流に入るまでの間、kioku-mesh ユーザが **今日**取れる手段。`docs/mcp-clients.md`
にも要約を追記した。

1. **server instructions / tool description で tool 名を明示済み（既出の緩和）**:
   ADR-0009 の `_INSTRUCTIONS` と PR #103 の各 docstring は `save_observation` を
   **名前で**呼んでいる。よって deferred でも、LLM は「探す」のではなく
   **正確な tool 名を 1 回 `select:` で取りに行く**だけで済む。これが既存の
   in-band 緩和であり、activation energy を「探索」から「単一フェッチ」に下げている。
2. **プロジェクト memory での priming（最も実効的）**: Claude Code ならプロジェクト
   `./CLAUDE.md`（または `~/.claude/CLAUDE.md`）に、保存系 tool を **名前付きで**
   呼ぶ短いルールを 1 行入れる:
   > 決定・バグ修正・発見・規約確立をしたら、`mesh_mem` の `save_observation` を
   > proactive に呼ぶ（ユーザの依頼を待たない）。
   tool 名がプロンプトに常駐するため、deferred でも初回の `select:` が確実に発火する。
3. **client 側の pin（対応 client のみ）**: もし利用中の client が「特定 MCP tool を
   常時ロード / pin する」設定を持つなら、kioku-mesh の保存系 tool を pin する。
   （Claude Code に現状この設定があるとは断定しない。無ければ提案 2 のギャップそのもの。）

## なぜ server 側で解決しないか（再掲・固定）

- MCP に always-load 属性が無い（提案 1 が無い限り server からは表明手段が無い）。
- 仮に server が instructions で「常駐させて」と書いても、deferred 化の判断は client
  が行うため強制力が無い。
- server に計測や常駐制御を持たせると、ADR-0009 の責務分界（server=規約配布、
  client=表示/ロード）を侵し、client 実装差を server が吸収する密結合になる。

## 受け入れ / フォローアップ

- 本書を提案ドラフトとして残し、上流（MCP spec / Claude Code）に起票可能な状態に
  する（リポジトリ制約により本セッションからの起票は行わない）。
- 緩和策 1–3 を `docs/mcp-clients.md` / `docs/mcp-clients.ja.md` に反映済み。
- 緩和の実効（deferred-load の一手間がどれだけ保存スキップに効くか）は **定性では
  なく** Issue #105 の opportunity coverage で前後比較して観測する。
