# Migration

## オンディスクパスの `mesh-mem` → `kioku-mesh` 改名 (v0.4.0 予定, #128)

v0.3.0 では binary 名のみ改名し、オンディスクのデータパスは `mesh-mem` のまま据え置いていました (下記参照)。v0.4.0 でこれらも `kioku-mesh` に揃えます。

| パス | 旧 | 新 |
|---|---|---|
| Config dir | `~/.config/mesh-mem/` | `~/.config/kioku-mesh/` |
| State dir | `~/.local/share/mesh-mem/` | `~/.local/share/kioku-mesh/` |
| systemd unit (生成物) | `mesh-mem-zenohd.service` | `kioku-mesh-zenohd.service` |

**環境変数 prefix (`MESH_MEM_*`) と Python import (`mesh_mem`) は据え置き** です。

**自動移行はしません。** 新パスが無く旧パスだけがある場合、kioku-mesh は旧パスをそのまま読みつつ警告を出します。データは保持されるので、好きなタイミングで手動移行してください。

```bash
# zenohd と MCP サーバを止めてから（開いた SQLite/RocksDB を動かしたまま mv しない）
systemctl --user stop kioku-mesh-zenohd 2>/dev/null || true   # 旧 unit 名なら mesh-mem-zenohd
pkill -f kioku-mesh-mcp; pkill -f mesh-mem-mcp

# config / state を移行
mv ~/.config/mesh-mem      ~/.config/kioku-mesh
mv ~/.local/share/mesh-mem ~/.local/share/kioku-mesh
mv ~/.local/state/mesh-mem ~/.local/state/kioku-mesh 2>/dev/null || true

# systemd unit を使っている場合は ExecStart / ROCKSDB_ROOT のパスも書き換え、daemon-reload
# その後 zenohd / MCP を再起動し、`kioku-mesh doctor` で確認
```

## `mesh-mem` → `kioku-mesh` (v0.3.0)

v0.3.0 で PyPI 配布名と CLI バイナリ名が `mesh-mem` から `kioku-mesh` に変更されました。
**オンディスクのデータパス・環境変数名・systemd unit 名は変更されていません** ので、
既存ユーザーは binary を入れ替えるだけでそのまま使えます。

| 変更されたもの | 旧 | 新 |
|---|---|---|
| PyPI 配布名 | `mesh-mem` | `kioku-mesh` |
| CLI コマンド | `mesh-mem` | `kioku-mesh` |
| MCP サーバ binary | `mesh-mem-mcp` | `kioku-mesh-mcp` |

| 変更されないもの (内部) | 値 |
|---|---|
| Config dir | `~/.config/mesh-mem/` |
| State dir | `~/.local/share/mesh-mem/` |
| systemd unit | `mesh-mem-zenohd.service` |
| 環境変数 prefix | `MESH_MEM_*` |
| Python import | `from mesh_mem import ...` |

### 切替手順

```bash
# 旧バイナリをアンインストール
uv tool uninstall mesh-mem  # or: pip uninstall mesh-mem

# 新バイナリをインストール
pip install kioku-mesh        # PyPI 経由
# または: uv tool install kioku-mesh

# MCP クライアントの登録パスを書き換え
kioku-mesh mcp install --client claude-code --force
```

`claude mcp` などで `mesh-mem-mcp` のパスをハードコードしていた場合は `kioku-mesh-mcp` に書き換えてください。`--force` 付きで `mcp install` を実行すれば一括で置き換わります。

## `zenoh-mem` → `mesh-mem` (v0.1.x)

`ZENOH_BACKEND_ROCKSDB_ROOT` のデフォルトパスが `~/.local/share/zenoh-mem` から
`~/.local/share/mesh-mem` に変更されました。既存データを引き継ぐ場合は手動で移行してください。

```bash
# 既存データを移行する場合（オプション）
mv ~/.local/share/zenoh-mem ~/.local/share/mesh-mem
```

`~/.config/systemd/user/mesh-mem-zenohd.service` を使っている場合は、
`Environment=ZENOH_BACKEND_ROCKSDB_ROOT` の値も `mesh-mem` に更新して
`systemctl --user daemon-reload` を実行してください。
