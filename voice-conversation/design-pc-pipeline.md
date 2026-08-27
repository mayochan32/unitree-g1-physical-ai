# PC側音声会話パイプライン 設計書

作成日: 2026-08-27
対象: Unitree G1 実機テスト（PC = Windows、構成B: G1ブリッジ型）

---

## 1. やりたいこと

```
G1のマイク → PCで受信 → STT → OpenAI LLM → TTS → G1のスピーカーで再生
```

G1本体（Jetson PC2）ではなく、**PC上でSTT/LLM/TTSを動かす**。G1は「マイクとスピーカーを持った入出力デバイス」として扱う。

---

## 2. 設計を決める2つの制約

### 2.1 マイク側とスピーカー側で必要な依存が違う（最重要）

ここが構成を決める分岐点。

| | 通信方式 | 必要なもの | Windowsで動くか |
|---|---|---|---|
| **マイク入力** | UDPマルチキャスト<br>`239.168.123.161:5555` | Python標準の`socket`のみ | **動く**（SDK不要） |
| **スピーカー出力** | DDS（`AudioClient.PlayStream`） | `unitree_sdk2_python` + `cyclonedds` | **要検証**（後述） |

つまり「PCがWindowsだからSDKが使えない」という問題は、**スピーカー出力側にしか存在しない**。マイク入力は素のUDPソケットで受けられるので、OSを選ばない。

### 2.2 Windows + Unitree SDK の状況（調査結果）

- Eclipse Cyclone DDS本体はWindowsを公式サポートしている（Visual Studio + CMakeでビルド可）。
- `cyclonedds-python`もWindowsの環境変数設定について言及があり、動く見込みはある。
- ただし`unitree_sdk2_python`のREADMEはLinux前提（`apt`、`enp2s0`のようなLinuxのIF名）で書かれており、**Windowsでの動作実績は公式には確認できていない**。
- WSL2で逃げる案は**非推奨**。WSL2はmirrored networking modeでもUDPマルチキャスト受信に既知の問題が複数報告されており、マイク側が動かなくなるリスクが高い。

→ **限られた実機時間をSDKのビルドで溶かすのは避けたい**ので、スピーカー出力はG1側に薄いブリッジを置く構成（構成B）を採る。

---

## 3. 採用する構成

### 3.1 全体像

```
┌─────────────────── PC (Windows) ───────────────────┐
│                                                     │
│   [AudioSource] ──> [VAD] ──> [STT] ──> [LLM]      │
│        ▲                                    │       │
│        │                                    ▼       │
│   [AudioSink] <────────────────────────  [TTS]      │
│        │                                            │
└────────┼────────────────────────────────────────────┘
         │                    ▲
         │ WebSocket          │ UDPマルチキャスト
         │ (PCM送信)          │ (16kHz mono PCM)
         ▼                    │
┌─────────────────── Unitree G1 ─────────────────────┐
│                                                     │
│  [g1_bridge]                      [RockChip MCU]   │
│      └─> AudioClient.PlayStream()      └─> 4マイクアレイ
│              └─> 5Wスピーカー                       │
└─────────────────────────────────────────────────────┘
```

- **マイク**: G1のマルチキャストをPCが直接受信（SDK不要）
- **スピーカー**: PCからWebSocketでPCMを送り、G1上のブリッジが`AudioClient.PlayStream()`を呼ぶ

### 3.2 G1側ブリッジ（`g1_bridge`）の役割

G1のPC2（Jetson / Linux）上で動かす薄い中継プロセス。SDKが確実に動く環境で、SDK依存部分だけを引き受ける。

- **必須機能**: WebSocketでPCM（16kHz mono 16bit）を受け取り、`AudioClient.PlayStream()`で再生する
- **オプション機能**: マイクのマルチキャストを受信してPCへ中継する

マイク中継をオプションで持たせておく理由は、**PCがG1と同一L2セグメントに置けない場合（Wi-Fi経由など）にマルチキャストが届かない可能性がある**ため。届くなら直接受信の方が経路が短くて良いが、届かなければブリッジ経由に切り替えられるようにしておく。

---

## 4. 当日の不確実性への対処（設計上いちばん重要）

**G1にSSHしてコードを配置できるかは当日にならないと分からない**という前提がある。ここを設計で吸収する。

### 4.1 I/Oを差し替え可能にする

音声の入り口と出口を抽象化し、実行時に選べるようにする。

```python
class AudioSource(ABC):
    def start(self) -> None: ...
    def read_chunk(self, timeout: float) -> bytes | None: ...  # 16kHz mono 16bit PCM
    def stop(self) -> None: ...

class AudioSink(ABC):
    def play(self, pcm: bytes, sample_rate: int) -> None: ...
    def stop(self) -> None: ...
```

| 実装 | 用途 | G1必要 | SDK必要 |
|---|---|---|---|
| `G1MulticastSource` | G1マイク直接受信 | ○ | × |
| `G1BridgeSource` | ブリッジ経由でG1マイク | ○（配置要） | × |
| `LocalMicSource` | PCのマイク（事前開発用） | × | × |
| `G1BridgeSink` | ブリッジ経由でG1スピーカー | ○（配置要） | × |
| `G1DirectSink` | SDK直接（PCがLinuxの場合のみ） | ○ | ○ |
| `LocalSpeakerSink` | PCのスピーカー（フォールバック） | × | × |

これにより、次が可能になる。

- **実機前**: `LocalMicSource` + `LocalSpeakerSink` で、STT→LLM→TTSのパイプライン全体をPCだけで完成させておく
- **当日・SSH可**: `G1MulticastSource` + `G1BridgeSink`（本命）
- **当日・SSH不可**: `G1MulticastSource` + `LocalSpeakerSink`（マイクはG1、音はPCから出す縮退運転。パイプラインが動くことは示せる）

設定は環境変数かCLI引数で切り替える。

```bash
python -m pc_pipeline.main --source g1-multicast --sink g1-bridge
python -m pc_pipeline.main --source local-mic     --sink local-speaker   # 事前開発
```

### 4.2 「当日ゼロから作らない」ことが目的

実機時間は貴重なので、当日やることは**接続確認と設定切り替えだけ**にしたい。パイプラインのロジックは事前にPCだけで完成・テストしておく。

---

## 5. 会話の状態遷移

```
  IDLE
   │ トリガ（Enterキー or VADが発話開始を検出）
   ▼
  LISTENING ──── マイクPCMを蓄積
   │ 無音がN秒続く or 最大録音長に到達
   ▼
  THINKING ───── STT → LLM → TTS（この間マイクは読み捨て）
   │ TTS完了
   ▼
  SPEAKING ───── G1スピーカーで再生（この間マイクは読み捨て）
   │ 再生完了 + 追加で数百ms分を読み捨て
   ▼
  IDLE
```

### 5.1 エコー対策（必須。これを入れないと確実に破綻する）

G1のスピーカーから出た音を、G1の4マイクアレイが拾う。対策なしだと**自分の発話を認識してループする**。

- `THINKING`と`SPEAKING`の間は、マイクのパケットを受信しつつ**すべて破棄**する（ソケットに溜めない）
- 再生完了後も、残響とバッファ内の残りを捨てるために**追加で300〜500ms程度**読み捨ててから`IDLE`に戻る
- UDPは受信し続けないとソケットバッファが溢れるので、「読まない」のではなく「読んで捨てる」

つまり半二重（half-duplex）で運用する。割り込み発話（barge-in）は初版では対応しない。

### 5.2 発話区間の検出（VAD）

初版は**プッシュトゥトーク（Enterキー）**を推奨。理由は、実機での最初の立ち上げでは「マイクが実際に音を拾えているか」の切り分けを単純にしたいため。

VAD自動検出は次の段階で入れる。実装は2択。

- **RMS閾値方式**: シンプル。G1向けの既存実装（`unitree_converse`）では閾値0.008・無音2.0秒継続・最大8秒という値が使われていた。実測で調整する前提なら十分。
- **`webrtcvad` / `silero-vad`**: より頑健。G1の動作音（サーボ音）が乗る環境ではこちらが有利な**見込み**（未検証の推測）。

まずRMS方式で動かし、ノイズで誤爆するようならVADライブラリに差し替える、という順序が安全。

---

## 6. 音声フォーマットの流れ

サンプルレートの変換ポイントを明確にしておく（ここを間違えると再生速度がおかしくなる）。

| 区間 | フォーマット | 備考 |
|---|---|---|
| G1マイク → PC | **16kHz** mono 16bit LE PCM | G1本体の固定仕様 |
| PC → OpenAI STT | WAVにヘッダを付けて送信 | 16kHzのままでよい |
| OpenAI TTS → PC | **24kHz** mono 16bit LE PCM（ヘッダなし） | `response_format="pcm"`の仕様 |
| PC → G1スピーカー | **16kHz** mono 16bit LE PCM | `AudioClient.PlayStream()`の要件 |

**24kHz → 16kHz のダウンサンプルが必須**。比が正確に3:2なので、`scipy.signal.resample_poly(audio, up=2, down=3)` が使える（既存アダプタの線形補間より品質が良い）。scipyを入れたくなければ既存の`_resample()`でも動く。

---

## 7. OpenAI API の選定

| 用途 | 使うAPI | 備考 |
|---|---|---|
| STT | `/v1/audio/transcriptions`<br>`gpt-4o-transcribe` または `whisper-1` | 日本語は`language="ja"`を明示すると精度・速度が安定 |
| LLM | Chat Completions / Responses API | 会話履歴を保持。システムプロンプトにGDLの人格記述を注入できる |
| TTS | `/v1/audio/speech`<br>`gpt-4o-mini-tts` | `response_format="pcm"`（24kHz 16bit LE）。`instructions`で話し方を指示可能 |

### 7.1 GDL連携の接点

LLMのシステムプロンプトにGDLの人格記述を流し込めば、そのまま`gdl-integration`テーマにつながる。今回のテストプログラムは**GDLの人格をG1に憑依させる器**として設計しておくと無駄がない。

```python
system_prompt = build_system_prompt(gdl_profile)  # GDLのJSONから生成
```

---

## 8. レイテンシの見積もりと改善案

### 8.1 初版（逐次処理）の見積もり

以下は**実測値ではなく推測**。実機で計測して埋める前提。

| 区間 | 推定 |
|---|---|
| 発話終了検出 | 0.5〜2.0秒（無音判定の待ち時間そのもの） |
| STT | 0.5〜1.5秒 |
| LLM | 0.5〜2.0秒 |
| TTS | 0.5〜1.5秒 |
| PC→G1転送 + 再生開始 | 0.1〜0.5秒 |
| **合計（発話終了→音が出るまで）** | **約2〜5秒** |

### 8.2 改善の打ち手（初版の後）

1. **LLMをストリーミングし、文単位でTTSに流す** — 最初の一文だけ先に喋り始められるので体感が大きく改善する。効果が最も大きい。
2. **無音判定時間を詰める** — 2.0秒→0.8秒など。誤切断とのトレードオフ。
3. **`PlayStream`のチャンク送信sleepを見直す** — 公式サンプルは96,000バイト（約3秒）ごとに1秒sleepを入れているが、短い応答なら1チャンクで収まりsleepは発生しない。長い応答で詰まるようなら要調整。
4. **OpenAI Realtime APIに置き換える** — 双方向ストリーミングでサーバ側VAD・割り込み対応があり、レイテンシは大幅に下がる。ただし実装の複雑度は上がるので、逐次版が動いてから検討する別案とする。

---

## 9. ネットワーク構成

- PCをG1の**有線Ethernetポートに直結**するのを基本とする
- PCのIPは `192.168.123.99/24` を推奨（Unitreeのドキュメントが推奨している値）
- G1側: PC2 = `192.168.123.164`、オーディオMCU = `192.168.123.161`
- マルチキャスト参加時は、**join対象のローカルインターフェースIPを明示指定**する（PCに複数NICがあると別NICで待ち受けて無音になる）
- Wi-Fi経由はマルチキャストがルータを越えない可能性が高いので避ける。どうしてもWi-Fiなら、ブリッジ経由のマイク中継（`G1BridgeSource`）に切り替える

---

## 10. ディレクトリ構成（実装時）

```
voice-conversation/
├── README.md
├── research-report.md            # 既存: 調査レポート
├── design-pc-pipeline.md         # 本書
│
├── g1_mic_input_adapter.py       # 既存: マルチキャスト受信
├── g1_speaker_output_adapter.py  # 既存: AudioClient直接（Linux用）
├── g1_gdl_audio_plugin.py        # 既存: GDL統合ラッパー
│
├── pc_pipeline/                  # PC側で動かす本体
│   ├── config.py                 # IP/ポート/モデル名/閾値
│   ├── audio_io/
│   │   ├── base.py               # AudioSource / AudioSink 抽象
│   │   ├── g1_multicast_source.py
│   │   ├── g1_bridge_source.py
│   │   ├── local_mic_source.py
│   │   ├── g1_bridge_sink.py
│   │   ├── g1_direct_sink.py
│   │   └── local_speaker_sink.py
│   ├── vad.py                    # 発話区間検出
│   ├── stt.py                    # OpenAI STT
│   ├── llm.py                    # OpenAI LLM（履歴管理・GDL注入）
│   ├── tts.py                    # OpenAI TTS
│   ├── audio_utils.py            # リサンプル・WAV化
│   ├── pipeline.py               # 状態機械
│   └── main.py                   # CLIエントリポイント
│
└── g1_bridge/                    # G1のPC2上で動かす
    ├── bridge_server.py          # WebSocket ⇄ AudioClient
    ├── requirements.txt
    └── README.md                 # デプロイ・起動手順
```

---

## 11. ブリッジのプロトコル

WebSocket 1本。バイナリフレーム＝PCM、テキストフレーム＝制御。

**PC → G1**

| 種別 | 内容 |
|---|---|
| テキスト | `{"type":"play_start","sample_rate":16000}` |
| バイナリ | 16kHz mono 16bit LE PCM チャンク |
| テキスト | `{"type":"play_end"}` |
| テキスト | `{"type":"stop"}`（再生中断） |
| テキスト | `{"type":"mic_start"}` / `{"type":"mic_stop"}`（マイク中継の制御・オプション） |

**G1 → PC**

| 種別 | 内容 |
|---|---|
| テキスト | `{"type":"playback_done"}` |
| テキスト | `{"type":"error","message":"..."}` |
| バイナリ | マイクPCM（マイク中継有効時のみ） |

`playback_done`を待ってから`IDLE`に戻ることで、エコー対策の同期が正確になる。

---

## 12. 実機当日の確認手順（切り分け順）

順番が大事。下から順に潰していく。

1. **ネットワーク疎通** — PCからG1のPC2（`192.168.123.164`）にping
2. **マイクのマルチキャストが届くか** — 受信バイト数とRMSを出すだけのスクリプトで確認
   - ここで無音（ゼロ値）なら、G1内蔵の音声アシスタントのモードが影響している可能性がある（調査レポート3章の既知課題）
3. **G1へのSSH可否** — 可ならブリッジを配置、不可なら`LocalSpeakerSink`に切り替え
4. **スピーカーから音が出るか** — 固定のWAVを1本再生するだけのテスト
5. **パイプライン全体を通す**
6. **エコーの挙動確認** — 音量を上げていって、どこでループが起きるか見る
7. **レイテンシ実測**

**1〜4が通れば、5は事前に完成させてあるので設定切り替えだけで動く**、という状態にしておくのが今回の設計の狙い。

---

## 13. 未確定・要検証事項

- G1内蔵の音声アシスタントとマルチキャスト受信の競合（調査レポート3章から継続）
- `unitree_sdk2_python`のWindows動作可否（構成Bを採るので今回は回避するが、将来のために検証しておく価値はある）
- G1の動作音（サーボ）がVADとSTT精度に与える影響
- `PlayStream`のチャンク間sleepが長い応答でどう効くか
- マイクアレイ4chのうち、マルチキャストで流れてくるのがどういう処理を経た信号か（ビームフォーミング済みか、単純なmix downか）は未確認
