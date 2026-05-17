# Windows ホストのセットアップ

> **Experimental — WSL2 強く推奨。** ネイティブ Windows は CI 対象外です。
> `zenohd` の Windows バイナリ、RocksDB プラグイン、ファイアウォール / サービス
> 周りは利用者がメンテする想定で、予告なく動かなくなる可能性があります。
> Windows ワークステーションで動かす場合は、**WSL2 上で通常の Linux peer として
> 動かす方を強く推奨**します（README の Quick start 参照）。WSL2 のネットワーク
> は `mirrored` モード（Windows 11 23H2 以降）にしておくと、WSL ゲストが他の LAN
> peer から TCP/7447 で到達可能になります。下記の手順は、ネイティブ Windows
> インストールが避けられない場合（例: WSL2 内 stdio MCP に到達できない
> Windows 版 Claude Desktop）のためのものです。

mesh-mem の開発は Linux first です。Windows 10 / 11 ホストも *peer として*
ネイティブで Zenoh mesh に参加できます。下記は Linux quick start からの差分です。
identity 環境変数、CLI コマンド、MCP 登録の作法は同じで、違うのは **パス形式**
だけです。Linux 例の `~/.venv/mesh-mem/bin/<binary>` は Windows では
`C:\Users\<user>\.venv\mesh-mem\Scripts\<binary>.exe` に読み替えてください。

## 1. Python と mesh-mem のインストール

- python.org から Python 3.10+ をインストール。**Add to PATH** にチェックを
  入れること。per-user インストーラなら管理者権限は不要。
- mesh-mem は **まだ PyPI 公開していません**。checkout からインストールします。

  ```powershell
  git clone https://github.com/h-wata/mesh-mem.git
  cd mesh-mem
  python -m venv $env:USERPROFILE\.venv\mesh-mem
  & "$env:USERPROFILE\.venv\mesh-mem\Scripts\python.exe" -m pip install -e .
  ```

  （`pip install mesh-mem` は現状ヒットしません。v1.0 リリース時に PyPI に
  載せる予定です。）

## 2. zenohd のインストール

Zenoh のバージョンに合う standalone zip 2 つを
[Eclipse Zenoh releases ページ](https://github.com/eclipse-zenoh/zenoh/releases)
から取得します（執筆時点では 1.9.0）。

- `zenoh-1.9.0-x86_64-pc-windows-msvc-standalone.zip`
- `zenoh-backend-rocksdb-1.9.0-x86_64-pc-windows-msvc-standalone.zip`

（releases ページには 1 アセットあたり 4 種類の命名パターンが並びますが、
Windows 10 / 11 では特別な理由がない限り `msvc` を選びます。`gnu` ではなく。）

インストール先は管理者権限の有無で決めます。

- **管理者権限がある場合**: 両 zip を `C:\Program Files\zenoh\` に展開して
  **machine** PATH に追加。
- **管理者権限がない場合**（企業 Win11 だとこちらが普通）:
  `%LOCALAPPDATA%\Programs\zenoh\` に展開し、**user** PATH に追加
  （System Properties → Environment Variables → User variables → `Path`）。
  どちらの場合も `zenohd.exe` と rocksdb プラグイン DLL を同じディレクトリに
  置く必要があります。

## 3. peer ごとの config

- `config\zenohd_peer.json5.template` をコピーして `{SELF_IP}` /
  `{PEER_N_IP}` を実 IP に置換。詳細は
  [config/peers/example_5peer.md](../config/peers/example_5peer.md) を参照。
  Windows でもパス形式が違うだけで内容は同じ。
- JSON5 文字列値の中のパスは **forward slash で書く** とエスケープが楽です。

  ```json5
  // optional override; default storage dir is %LOCALAPPDATA%\mesh-mem
  // when ZENOH_BACKEND_ROCKSDB_ROOT points there.
  // Forward slashes work on Windows in zenoh's config parser.
  ```

## 4. zenohd の起動（オプションでサービス化）

対話的な起動：

```powershell
$env:ZENOH_BACKEND_ROCKSDB_ROOT = "$env:LOCALAPPDATA\mesh-mem"
New-Item -ItemType Directory -Force -Path $env:ZENOH_BACKEND_ROCKSDB_ROOT | Out-Null
zenohd.exe --config C:\path\to\zenohd_peer.json5
```

自動起動するなら、Windows サービスとして登録します。NSSM
(Non-Sucking Service Manager, https://nssm.cc) を使うと、`sc.exe` よりも
stdout / stderr ログ周りがきれいに扱えます。

```powershell
nssm install zenohd "C:\Program Files\zenoh\zenohd.exe" "--config C:\path\to\zenohd_peer.json5"
nssm set     zenohd AppEnvironmentExtra "ZENOH_BACKEND_ROCKSDB_ROOT=C:\Users\<user>\AppData\Local\mesh-mem"
nssm start   zenohd
```

## 5. Windows Defender Firewall

`New-NetFirewallRule` は **elevated PowerShell** が必要です。管理者権限なしで
実行すると `Access is denied.` で **silent に失敗** します。PowerShell を
右クリック → "Run as administrator" するか、`Start-Process powershell -Verb RunAs`
で UAC を出してください。

```powershell
New-NetFirewallRule -DisplayName "mesh-mem zenohd" `
                    -Direction Inbound -Action Allow `
                    -Protocol TCP -LocalPort 7447 `
                    -RemoteAddress 192.168.1.0/24,10.0.0.14
```

`-RemoteAddress` は実際に mesh する LAN/VPN レンジに絞ってください。

**outbound しか張らない peer**（他 peer から dial-in されない側）はこの
ステップは丸ごとスキップ可能です。Windows Firewall は確立済みソケットの
return traffic は通すからです。hub-and-spoke レイアウトでは
**spoke はこの inbound ルール不要**、hub だけ必要、になります。
このホストが他 peer の `connect.endpoints` に登場するようになった時点で
ルールを追加してください。

## 6. 時刻同期 (w32time)

Windows には `w32time` サービスが標準搭載されています。mesh-mem に必要なのは
peer 間でサブ秒の一致だけです。状態確認と強制 resync：

```powershell
# Status
w32tm /query /status
w32tm /query /source

# Force an immediate correction (Linux's `chronyc makestep` equivalent)
w32tm /resync /force

# Cross-check against another peer
w32tm /stripchart /computer:192.168.1.10 /samples:5 /dataonly
```

数百 ms を超えるズレが出る場合は、`w32tm /config /update /manualpeerlist:"time.cloudflare.com"`
で信頼できる NTP サーバに変更してください。

## 7. データディレクトリ

v0.2.1 以降、mesh-mem は OS ごとに state ディレクトリを解決します：

- **Windows**: `%LOCALAPPDATA%\mesh-mem`（例: `C:\Users\<user>\AppData\Local\mesh-mem`）— `platformdirs` 経由
- **macOS**: `~/Library/Application Support/mesh-mem` — `platformdirs` 経由
- **Linux**: `~/.local/share/mesh-mem`（固定、v0.2.0 から不変。
  `XDG_DATA_HOME` は **意図的に honor しない**。v0.2.1 以前のパスを保ち、
  XDG を設定済みのユーザを silent migration しないため）

別パス（例: 速い NVMe）に向ける場合：

```powershell
$env:MESH_MEM_STATE_DIR = "D:\mesh-mem-state"
```

## 8. Smoke check

```powershell
$env:MESH_MEM_AGENT_FAMILY = "claude-code"
$env:MESH_MEM_CLIENT_ID    = "claude-windows-1"

mesh-mem save "hello from windows" --project demo --memory-type note
# 別の peer から:
mesh-mem search "hello from windows" --project demo --limit 5
```

このホストが既存 mesh に **新規参加** する peer の場合、ローカル SQLite index は
空ですが、in-process replication subscriber が観測の到着とともに populate
してくれるので、新規 publish データに対する `save` / `search` は普通に動きます。
既存 peer の zenohd RocksDB に積まれた **過去データ** を一気にローカル index に
取り込みたい場合は、一度だけ `--rebuild` で起動します：

```powershell
mesh-mem --rebuild status   # one-time alignment scan
```

rebuild は populated mesh では **数十秒** かかります（~117k records で実測
~15 s）。以降の CLI 起動は default で scan をスキップ (#38) するので、対話用途は
サブ秒に戻ります。

## 既知の制限

- **CI でカバーしていない。** mesh-mem の CI は Linux のみで、ネイティブ
  Windows の regression は事前マージテストで検知できず、利用者が実行時に
  気づくしかありません。上記セクションを Experimental としている主な理由です。
- WSL2: Windows ホストの zenohd を WSL から触るには、WSL ネットワークを
  `mirrored` モード（Windows 11 23H2+）にするか、TCP/7447 を手動 forward
  する必要があります。デフォルトの `nat` モードでは Windows が WSL ゲストから
  隠れます。（WSL2 内で mesh-mem を動かせるなら、それが一番です。）
- Windows での search latency は、SQLite-first パスで Linux と同等です
  （OS 依存層は `pathlib` だけ）。v0.2.0 のベンチ数値がそのまま参考になります。
- Windows 上の Claude Desktop は、WSL2 内に置いた MCP サーバを stdio で
  起動できません。Windows で Desktop 連携が必要なら、上記のネイティブ
  インストールが唯一の道です — Experimental の注意書きを承知の上で進めてください。
