# G1音声ブリッジ（PC2上で実行）

PC側からPCMを受け取り、`AudioClient.PlayStream()` でG1のスピーカーを鳴らす中継サーバ。オプションでG1のマイクをPCへ中継もできる。

## なぜ必要か

G1スピーカーの再生には `AudioClient` = DDS通信が必要で、`unitree_sdk2_python` + `cyclonedds` に依存する。この依存はLinux前提のため、Windows PCから直接叩くのはリスクが高い。

そこで**SDK依存部分だけをG1のPC2（Ubuntu = SDKが確実に動く環境）に閉じ込め**、PCとは単純なTCPで喋る。

なお**マイク入力はSDK不要**（素のUDPマルチキャスト）なので、PCから直接受信できる。ブリッジのマイク中継機能は、マルチキャストが届かない環境向けの保険。

## 2つの版がある

| ファイル | 通信方式 | G1側の追加インストール | 位置づけ |
|---|---|---|---|
| `bridge_server.py` | 素のTCP | **不要**（標準ライブラリのみ） | **本命** |
| `bridge_server_ws.py` | WebSocket | `websockets` が必要 | バックアップ |

**G1は共有機材なので、環境を変えずに済む `bridge_server.py` を使う。** WebSocket版は、TCP版で問題が起きた場合の代替として残してある。

## 本命：TCP版（インストール不要）

```bash
# /tmp に置く（再起動で自動的に消えるので痕跡が残らない）
scp bridge_server.py unitree@192.168.123.164:/tmp/

ssh unitree@192.168.123.164
python3 /tmp/bridge_server.py --iface eth0
```

`--iface` にはPC2側でG1内部ネットワークに繋がっているNIC名を指定する。分からなければ `ip addr` で `192.168.123.x` が付いているものを探す（通常は `eth0`）。

起動すると次のように出る。

```
[bridge] 現在の音量を記録: 42（終了時に戻す）
[bridge] AudioClient初期化完了 (iface=eth0)
[bridge] tcp://0.0.0.0:8765 で待機中
[bridge] 停止する場合は Ctrl+C（音量は自動で元に戻ります）
```

### 共有機材への配慮

このスクリプトは**G1の環境を変更しない**ように作ってある。

- **追加インストール不要** — 外部依存は `unitree_sdk2py` のみで、これはG1に元から入っている
- **sudoを使わない** — 設定ファイルもシステムサービスも触らない
- **ファイルを書かない** — ログは標準出力のみ
- **音量を元に戻す** — 起動時に `GetVolume()` で現在の音量を記録し、終了時に復元する。`SetVolume()` は本体設定を変えるので、次に使う人に影響しないようにするため
- **終了時に `PlayStop`** — 再生状態をリセットする

`/tmp` に置けば、残るファイルも再起動で消える。

## バックアップ：WebSocket版

TCP版で問題が起きた場合のみ使う。`websockets` のインストールが必要なので、**システムのPythonを汚さないようvenvを使うこと**。

```bash
scp bridge_server_ws.py unitree@192.168.123.164:/tmp/

ssh unitree@192.168.123.164
# --system-site-packages で unitree_sdk2py は継承しつつ、websocketsはvenv内だけに入る
python3 -m venv --system-site-packages /tmp/voice_venv
/tmp/voice_venv/bin/pip install websockets
/tmp/voice_venv/bin/python /tmp/bridge_server_ws.py --iface eth0
```

`/tmp` に作れば再起動で消える。PC側は `--sink g1-bridge-ws` を指定する。

> G1がインターネットに出られない場合は、事前にPCでwheelを落として持ち込む。詳細は `../RUNBOOK.md` の該当節。

## 動作確認

PC側から（SDKもwebsocketsも不要）:

```bash
python tools/check_bridge.py --host 192.168.123.164

# マイク中継もあわせて5秒確認する
python tools/check_bridge.py --host 192.168.123.164 --mic 5
```

`playback_done` が返り、G1から440Hzのトーンが1秒鳴れば成功。

WebSocket版を使っている場合は `tools/check_bridge_ws.py` を使う。

## プロトコル（TCP版）

長さ付きの単純なフレーム。

```
[1バイト: 種別][4バイト: ペイロード長(ビッグエンディアン)][ペイロード]

種別 0x01 = JSON制御メッセージ
種別 0x02 = 生PCM（16kHz mono 16bit LE）
```

**PC → G1**

| 種別 | 内容 |
|---|---|
| JSON | `{"type":"play_start","sample_rate":16000}` |
| PCM | 16kHz mono 16bit LE PCM チャンク |
| JSON | `{"type":"play_end"}` |
| JSON | `{"type":"stop"}` — 再生中断 |
| JSON | `{"type":"set_volume","volume":50}` |
| JSON | `{"type":"mic_start"}` / `{"type":"mic_stop"}` |
| JSON | `{"type":"ping"}` |

**G1 → PC**

| 種別 | 内容 |
|---|---|
| JSON | `{"type":"playback_done"}` |
| JSON | `{"type":"error","message":"..."}` |
| JSON | `{"type":"pong"}` |
| PCM | マイク音声（`mic_start` 中のみ） |

再生用とマイク用で別々のTCP接続を張る想定。サーバは接続ごとにスレッドを立てるので、両方が同時に来ても問題ない。

### playback_done のタイミングが重要

`PlayStream()` は音声を**送信するだけ**で、返ってきた時点ではまだ鳴っていない。そのため送信完了後に音声長ぶんの時間が経つまで待ってから `playback_done` を返している。

これを省くとPC側のエコー対策（再生中はマイクを捨てる）の区間がずれて、**G1が自分の発話を聞き取ってループする**。

`check_bridge.py` は往復時間が音声長より明らかに短い場合に警告を出すので、この同期が壊れていないか確認できる。

## 必要なもの

**TCP版**: Python 3.8以上と `unitree_sdk2py`（G1に元から入っている）のみ。

**WebSocket版**: 上記に加えて `websockets`。
