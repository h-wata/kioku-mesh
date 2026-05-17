# Migration

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
