# 音声での会話

## 目的

Unitree G1上で音声による会話を成立させる。マイク入力→音声認識→対話生成→音声合成→G1のスピーカー出力までの一連のパイプラインを検証する。

現在は **PC側でSTT/LLM/TTSを動かし、G1をマイクとスピーカーを持った入出力デバイスとして扱う**構成で実装している（詳細は `design-pc-pipeline.md`）。

## 構成

```
G1マイク ──マルチキャスト──> PC ──STT──> LLM ──TTS──> ブリッジ ──> G1スピーカー
         (SDK不要)                (OpenAI)                 (TCP)
```

設計上の要点は4つ。

1. **マイク入力にSDKは要らない** — 素のUDPマルチキャスト受信なのでWindowsでも動く。SDK（DDS）が必要なのはスピーカー出力側だけ。
2. **SDK依存はG1側のブリッジに閉じ込める** — PC側の依存は `openai` だけになり、OSを問わない。
3. **G1の環境を変更しない** — G1は共有機材なので、ブリッジは**Python標準ライブラリだけ**で動くように書いてある。`pip install` も `sudo` も不要。音量は起動時に記録し終了時に元へ戻す。
4. **I/Oを差し替え可能にしてある** — G1が無くてもPCのマイク/スピーカーで開発でき、当日は設定切り替えだけで済む。

## クイックスタート

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...          # Windows: $env:OPENAI_API_KEY="sk-..."

# G1が無くても動く（事前の開発・動作確認用）
python -m pc_pipeline.main --source local-mic --sink local-speaker

# 本番構成（G1マイク直接受信 + ブリッジ経由でG1スピーカー）
python -m pc_pipeline.main --source g1-multicast --sink g1-bridge
```

`--source` / `--sink` の組み合わせで状況に応じて切り替える。

| 状況 | コマンド |
|---|---|
| 事前開発（G1なし） | `--source local-mic --sink local-speaker` |
| 本命 | `--source g1-multicast --sink g1-bridge` |
| G1にSSHできない（縮退運転） | `--source g1-multicast --sink local-speaker` |
| マルチキャストが届かない | `--source g1-bridge --sink g1-bridge` |
| PCがLinuxでSDKが入っている | `--source g1-multicast --sink g1-direct` |
| TCP版で問題が出た（バックアップ） | `--source g1-multicast --sink g1-bridge-ws` |

その他のオプション:

- `--trigger vad` — Enterキーを押さず、声を検出したら自動で認識する（既定は `ptt`）
- `--stream` — LLM応答を文単位でTTSに流し、最初の一文から喋り始める（体感レイテンシが下がる）
- `--gdl-profile path.json` — GDLの人格記述をシステムプロンプトに注入する

## 実機当日の手順

> **当日は `RUNBOOK.md` を見ること。** 持ち物、Windowsのネットワーク・ファイアウォール設定、各ステップの期待される出力、撤退ライン、トラブルシューティングまで詳細に書いてある。以下はその要約。

**この順番で下から潰していく。** 1〜4が通れば、5は事前に完成させてあるので設定切り替えだけで動く。

```bash
# 1. ネットワーク疎通
ping 192.168.123.164

# 2. マイクが届くか（SDK不要。Windowsでそのまま実行できる）
python tools/check_mic.py --local-ip 192.168.123.99
#    声を出してRMSが跳ね上がればOK
#    --save mic.wav を付ければ録音して後で耳で確認できる

# 3. G1にSSHしてブリッジを起動（インストール不要。/tmpに置くので痕跡が残らない）
scp g1_bridge/bridge_server.py unitree@192.168.123.164:/tmp/
ssh unitree@192.168.123.164 "python3 /tmp/bridge_server.py --iface eth0"

# 4. スピーカーが鳴るか（PC側から。SDK不要）
python tools/check_bridge.py --host 192.168.123.164

# 5. パイプライン全体
python -m pc_pipeline.main --source g1-multicast --sink g1-bridge
```

**2 で「パケットは届くがRMSが常に0」だった場合**、G1内蔵の音声アシスタントがマイクを掴んでいる可能性がある（`research-report.md` 3章の既知課題）。内蔵アシスタントのモードを確認する。

## コード

### 調査・設計ドキュメント

- `research-report.md` — G1のハードウェア仕様、音声まわりの制約、アーキテクチャ検討（オンデバイス完結型 / クラウドAPI活用型 / ハイブリッド）と採用の経緯
- `design-pc-pipeline.md` — PC側パイプラインの設計書。構成の選定理由、状態遷移、エコー対策、当日の不確実性への対処
- **`RUNBOOK.md` — 実機テスト当日の手順書**。持ち物、ネットワーク設定、切り分け手順、各ステップの撤退ライン、トラブルシューティング

### PC側パイプライン（`pc_pipeline/`）

| ファイル | 内容 |
|---|---|
| `config.py` | 設定。環境変数で上書きできる |
| `audio_utils.py` | PCM変換・リサンプル・WAV化 |
| `vad.py` | RMS閾値ベースの発話区間検出 |
| `stt.py` / `llm.py` / `tts.py` | OpenAI連携。`llm.py` はGDL人格の注入と文単位ストリーミングに対応 |
| `pipeline.py` | 状態機械（IDLE→LISTENING→THINKING→SPEAKING）とエコー対策 |
| `main.py` | CLIエントリポイント |
| `audio_io/` | 音声I/Oの抽象と8つの実装（G1／ローカル／ブリッジTCP／ブリッジWS） |

### G1側ブリッジ（`g1_bridge/`）

G1のPC2上で動かす中継サーバ。PCMを受け取り `AudioClient.PlayStream()` を呼ぶ。デプロイ手順は `g1_bridge/README.md`。

| ファイル | 通信方式 | G1側の追加インストール | 位置づけ |
|---|---|---|---|
| `bridge_server.py` | 素のTCP | **不要**（標準ライブラリのみ） | **本命** |
| `bridge_server_ws.py` | WebSocket | `websockets` が必要 | バックアップ |

G1は共有機材なので、環境を変えずに済むTCP版を使う。

### 疎通確認ツール（`tools/`）

- `check_mic.py` — マイクのマルチキャストが届くか（SDK不要、Windows可）
- `check_bridge.py` — ブリッジ経由でスピーカーが鳴るか（SDK不要、Windows可）。`--mic 5` でマイク中継も確認できる
- `check_bridge_ws.py` — 同上のWebSocket版（バックアップ手順用）
- `check_speaker.py` — SDK直接でスピーカーが鳴るか（G1のPC2上で実行）

### 旧アダプタ（GDL統合用）

`g1_mic_input_adapter.py` / `g1_speaker_output_adapter.py` / `g1_gdl_audio_plugin.py` は、GDL本体に音声I/Oプラグインとして組み込む想定で先に書いたもの。`pc_pipeline/audio_io/` が同じ役割をより整理された形で提供しているので、GDL統合時はそちらを参照する。

## テスト

実機もAPIキーも無しで、ロジック部分を検証できる。

```bash
python tests/test_pipeline_logic.py
```

検証している内容:

- 24kHz→16kHzのリサンプルで長さが正確に2/3になり、再生時間が変わらないこと
- WAVヘッダの往復変換
- VADの発話区間検出（終端検出、短すぎる音の棄却、先頭無音のスキップ、最大長打ち切り）
- 状態機械が1ターンを完走し、**再生中にマイク入力を捨てている**こと（エコー対策）

## 設計上の注意点

### エコー対策

G1のスピーカーから出た音を同じG1のマイクが拾うため、対策なしだと自分の発話を認識して無限ループする。

- `THINKING` / `SPEAKING` の間はマイクのパケットを**受信しつつ破棄**する（UDPなので読まないとバッファが溢れる）
- 再生完了後も `ECHO_COOLDOWN_SEC`（既定0.4秒）だけ捨て続けてから待機に戻る
- `AudioSink.play()` は**再生が鳴り終わるまでブロックする**契約。`PlayStream()` は送信するだけなので、ブリッジ側で音声長ぶん待ってから `playback_done` を返している

初版は半二重で、割り込み発話（barge-in）には対応していない。

### 初回応答の遅延

`scipy.signal` の初回importは実測で約0.8秒かかる。これを再生時の遅延importにすると「最初の応答だけ0.8秒遅い」形で表面化するため、`audio_utils.warmup()` を起動時に呼んで前払いしている。

### レイテンシ

逐次処理（`--stream` なし）で、発話終了から音が出るまで**約2〜5秒の見込み**（実測ではなく推測。実機で計測する）。`--stream` を付けると最初の一文から喋り始めるので体感が大きく改善する。

## 検討事項（TODO）

- [x] 使用する音声認識エンジンの選定 → OpenAI `gpt-4o-transcribe`
- [x] 対話生成のバックエンド → OpenAI Chat Completions
- [x] 音声合成エンジンの選定 → OpenAI `gpt-4o-mini-tts`（pcm出力）
- [x] G1本体のマイク/スピーカー仕様確認 → 16kHz mono 16bit、マルチキャスト受信とAudioClient
- [ ] レイテンシの実測
- [ ] ノイズ環境下（G1のサーボ音等）での認識精度とVADの誤爆
- [ ] G1内蔵音声アシスタントとの競合検証
- [ ] barge-in（割り込み発話）対応
- [ ] OpenAI Realtime APIへの置き換え検討

## 実験ログ

（実施した実験を都度追記）
