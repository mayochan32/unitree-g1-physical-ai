# G1音声ブリッジ（PC2上で実行）

PC側からWebSocketでPCMを受け取り、`AudioClient.PlayStream()` でG1のスピーカーを鳴らす中継サーバ。オプションでG1のマイクをPCへ中継もできる。

## なぜ必要か

G1スピーカーの再生には `AudioClient` = DDS通信が必要で、`unitree_sdk2_python` + `cyclonedds` に依存する。この依存はLinux前提のため、Windows PCから直接叩くのはリスクが高い。

そこで**SDK依存部分だけをG1のPC2（Ubuntu = SDKが確実に動く環境）に閉じ込め**、PCとは素のWebSocketで喋る。PC側の依存は `websockets` だけになり、OSを問わなくなる。

なお**マイク入力はSDK不要**（素のUDPマルチキャスト）なので、PCから直接受信できる。ブリッジのマイク中継機能は、マルチキャストが届かない環境（Wi-Fi経由など）向けの保険。

## デプロイ

```bash
# PCからG1のPC2へ転送
scp bridge_server.py unitree@192.168.123.164:~/

# PC2にSSHして起動
ssh unitree@192.168.123.164
pip3 install websockets
python3 bridge_server.py --iface eth0 --port 8765
```

`--iface` にはPC2側でG1内部ネットワークに繋がっているNIC名を指定する。分からなければ `ip addr` で `192.168.123.x` が付いているものを探す。

起動すると次のように出る。

```
[bridge] AudioClient初期化完了 (iface=eth0)
[bridge] ws://0.0.0.0:8765 で待機中
```

## 動作確認

PC側から（SDK不要）:

```bash
python tools/check_bridge.py --host 192.168.123.164
```

`playback_done` が返り、G1から440Hzのトーンが1秒鳴れば成功。

## プロトコル

**PC → G1**

| 種別 | 内容 |
|---|---|
| テキスト | `{"type":"play_start","sample_rate":16000}` |
| バイナリ | 16kHz mono 16bit LE PCM チャンク |
| テキスト | `{"type":"play_end"}` |
| テキスト | `{"type":"stop"}` — 再生中断 |
| テキスト | `{"type":"set_volume","volume":50}` |
| テキスト | `{"type":"mic_start"}` / `{"type":"mic_stop"}` — マイク中継の制御 |

**G1 → PC**

| 種別 | 内容 |
|---|---|
| テキスト | `{"type":"playback_done"}` |
| テキスト | `{"type":"error","message":"..."}` |
| バイナリ | マイクPCM（`mic_start` 中のみ） |

### playback_done のタイミングが重要

`PlayStream()` は音声を**送信するだけ**で、返ってきた時点ではまだ鳴っていない。そのため送信完了後に音声長ぶんの時間が経つまで待ってから `playback_done` を返している。

これを省くとPC側のエコー対策（再生中はマイクを捨てる）の区間がずれて、**G1が自分の発話を聞き取ってループする**。

## 必要なもの

- Python 3.8以上
- `unitree_sdk2_python`（PC2には通常インストール済み）
- `websockets`
