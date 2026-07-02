# Docker で zenohd を起動する

Docker / Docker Compose がインストール済みであれば、`kioku-mesh zenohd install` の代わりに
Docker で zenohd を起動できます。apt インストールよりも環境汚染がなく、ワンコマンドで起動できます。
Python ソース (`src/kioku_mesh/`) は一切触らず、zenohd + RocksDB 層のみ Docker 化します。

**前提**: Docker Engine および Docker Compose プラグインがインストール済みであること。

```bash
# 1. リポジトリルートに移動する
cd /path/to/kioku-mesh

# 2. zenohd + RocksDB バックエンド入り Docker イメージをビルドして起動する
#    初回はビルド (RocksDB plugin のダウンロードを含む) があるため数分かかる
docker compose up -d

# 3. MCP クライアントの接続先はデフォルトの tcp/127.0.0.1:7447 のまま変更不要
#    起動確認:
docker compose ps
docker compose logs zenohd

# 4. kioku-mesh CLI で疎通確認する
kioku-mesh save "Docker 起動テスト" --memory-type note
kioku-mesh search "Docker"

# 5. 停止する
docker compose down
```

## データの永続化

RocksDB のデータは `./data/zenoh/` ディレクトリに保存されます。このディレクトリが存在する限り
`docker compose down` → `docker compose up -d` を繰り返してもデータは保持されます。

```bash
# バックアップ例
tar -czf zenoh-backup-$(date +%Y%m%d).tar.gz ./data/zenoh/

# データを完全に削除してゼロから始める場合
docker compose down
rm -rf ./data/zenoh/
```

> **注意**: `./data/zenoh/` を削除すると zenohd が保持していたすべての observation が消えます。
> `docker compose down -v` は使わないでください (named volume ではないため効果はありませんが習慣として)。

## 他 peer と接続する場合

Docker で起動した zenohd を mesh の hub または spoke として使うには、
`config/zenohd.docker.json5` の `connect.endpoints` に対向 peer の IP を追加し、
`docker compose restart zenohd` します。詳細は `config/zenohd.docker.json5` のコメントを参照してください。

## セキュリティ注意

デフォルトの `docker-compose.yaml` は port 7447/8000 をすべてのインターフェース (`0.0.0.0`) に公開します。
ローカル開発・テストのみで使う場合は、外部に公開しないよう ports を制限してください:

```yaml
# ローカルのみに制限する場合 (docker-compose.yaml を編集)
ports:
  - "127.0.0.1:7447:7447"
  - "127.0.0.1:8000:8000"
```

mesh peer として LAN に公開する場合は、信頼できるネットワーク内でのみ使用してください
(ファイアウォールや Tailscale / WireGuard での制限を推奨します)。
詳細は README の [Multi-Host Mesh](../README.md#multi-host-mesh) のセキュリティ注意を参照してください。

## zenoh バージョンを上げる場合

`ZENOH_VERSION` を変更したときは `Dockerfile` の `ROCKSDB_SHA256` も更新が必要です。
新しい digest は GitHub Releases API で取得できます:

```bash
# x86_64 (musl) の sha256 を取得する
gh api repos/eclipse-zenoh/zenoh-backend-rocksdb/releases/tags/<new_version> \
  --jq '.assets[] | select(.name | contains("x86_64-unknown-linux-musl-standalone")) | .digest'

# aarch64 (musl) の sha256 を取得する
gh api repos/eclipse-zenoh/zenoh-backend-rocksdb/releases/tags/<new_version> \
  --jq '.assets[] | select(.name | contains("aarch64-unknown-linux-musl-standalone")) | .digest'
```

取得した `sha256:` プレフィックス付き文字列の **プレフィックスを除いた部分** を
`Dockerfile` の `ARG ROCKSDB_SHA256=` に設定してください。

## aarch64 (ARM64) でビルドする場合

デフォルトは x86_64 向けです。aarch64 ホストでビルドする際は build-arg を上書きします:

```bash
# aarch64 ビルド (Raspberry Pi 5 / Apple Silicon Docker など)
docker compose build \
  --build-arg ZENOH_TARGET=aarch64-unknown-linux-musl \
  --build-arg ROCKSDB_SHA256=298734a4f50dfa12c27337cbafeb7d99949f4f3fda4c330ff6891d39fdd97112

# 以降は通常通り
docker compose up -d
```

> aarch64 digest (v1.9.0): `298734a4f50dfa12c27337cbafeb7d99949f4f3fda4c330ff6891d39fdd97112`
> x86_64 digest (v1.9.0): `88b13af5ddaadff9ec55c61765db6344666ae38731257877023b07c59a0c4bd1`
