# Migration

## オンディスクパスの `mesh-mem` → `kioku-mesh` 改名 (v0.4.0 予定, #128)

v0.3.0 では binary 名のみ改名し、オンディスクのデータパスは `mesh-mem` のまま据え置いていました (下記参照)。v0.4.0 でこれらも `kioku-mesh` に揃えます。

| パス | 旧 | 新 |
|---|---|---|
| Config dir | `~/.config/mesh-mem/` | `~/.config/kioku-mesh/` |
| State dir | `~/.local/share/mesh-mem/` | `~/.local/share/kioku-mesh/` |
| systemd unit (生成物) | `mesh-mem-zenohd.service` | `kioku-mesh-zenohd.service` |

**環境変数 prefix (`MESH_MEM_*`) と Python import (`mesh_mem`) は据え置き** です。

**前提：先に新バージョン (#128 を含むビルド) を install すること。** フォールバックの読み替えは新コードにしか入っていません。旧バージョンのまま `kioku-mesh` パスへ mv すると、旧コードは `mesh-mem` しか見ないため動かなくなります。新コードさえ入っていれば旧 `mesh-mem` のまま動き続ける（警告のみ）ので、慌てず後から mv できます。

```bash
# 新バージョンを install（例: ブランチ / リリース後の PyPI / main）
uv tool install --reinstall --from 'git+https://github.com/h-wata/kioku-mesh@main' kioku-mesh
kioku-mesh --version
```

**自動移行はしません。** 新パスが無く旧パスだけがある場合、kioku-mesh は旧パスをそのまま読みつつ警告を出します。データは保持されるので、好きなタイミングで手動移行してください。

> **zenohd の停止方法は環境依存です。** `kioku-mesh init --install-systemd` で作った unit は `kioku-mesh-zenohd.service`（旧名 `mesh-mem-zenohd.service`）ですが、手で書いた unit は単に `zenohd.service` の場合もあります。まず実際の起動元を確認してください:
>
> ```bash
> ps -o pid,cmd -C zenohd                 # 動いている zenohd を特定
> cat /proc/<PID>/cgroup                   # .../<unit名>.service なら systemd 管理
> systemctl --user list-units '*zenoh*'    # user unit の実名を確認
> crontab -l | grep -i zenoh               # cron 起動でないことも一応確認
> ```
>
> systemd 管理なら `pkill` ではなく `systemctl --user stop <実際のunit名>` で止めること（`Restart=on-failure` だと pkill では respawn する）。

```bash
# 0. zenohd と MCP サーバを止めてから（開いた SQLite/RocksDB を動かしたまま mv しない）
systemctl --user stop <実際のunit名>          # 例: zenohd / kioku-mesh-zenohd / mesh-mem-zenohd
pkill -f kioku-mesh-mcp; pkill -f mesh-mem-mcp

# 1. config / state を移行
mv ~/.config/mesh-mem      ~/.config/kioku-mesh
mv ~/.local/share/mesh-mem ~/.local/share/kioku-mesh
mv ~/.local/state/mesh-mem ~/.local/state/kioku-mesh 2>/dev/null || true

# 2. unit 内の mesh-mem パス（ExecStart の config / ROCKSDB_ROOT / ExecStartPre）を書き換え
#    手書き unit の例:
sed -i 's|/mesh-mem|/kioku-mesh|g' ~/.config/systemd/user/<実際のunit名>.service

# 3. 反映して再起動 → 確認
systemctl --user daemon-reload
systemctl --user start <実際のunit名>
kioku-mesh doctor
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
