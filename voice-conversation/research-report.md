# Unitree G1 音声会話システム調査レポート

作成日: 2026-08-25（追記あり）

## 1. 目的

Unitree G1 で音声会話（マイクで聞く→AIが理解する→音声で返す）を実現するにあたり、G1のハードウェア仕様と、実現可能なアーキテクチャパターンを整理する。

---

## 2. Unitree G1 のハードウェア仕様

### 2.1 コンピュート構成

G1には役割の異なる2つの計算ユニットがある。

- **モーション制御用コンピュータ（PC1）**: Unitreeの歩行・バランス制御専用。ユーザーコードからは基本的にDDS（Data Distribution Service）経由の通信のみで、直接アクセスして自由にプログラムを走らせる対象ではない。
- **開発用コンピュータ（PC2 / 拡張コンピューティングモジュール）**: NVIDIA Jetson Orin NX相当（Arm Cortex-A78AE 8コア、Ampereアーキテクチャ GPU、メモリ16GB、ストレージ最大2TB、IP: 192.168.123.164）。音声認識・LLM推論・TTSなど、ユーザーが自由に載せるアプリケーションはここで動かす。

標準構成はCPUのみのモデルもあり、EDUグレードでGPU搭載のOrinモジュールがオプション追加される構成もある（購入時にどの計算モジュールが載っているか要確認）。

### 2.2 ネットワーク

- 首の接続部にGigabit Ethernet（1000BASE-T）ポートが2つ。有線LAN経由でPC2にSSH接続し、ROS2/DDS通信を行うのが標準的な開発フロー。
- DDS通信はUnitreeの `unitree_sdk2`（C++）または `unitree_sdk2_python` で行う。

### 2.3 センサー

- 3D LiDAR（Livox MID360、水平360°・垂直59°）
- 深度カメラ（Intel RealSense D435i相当）
- IMU（詳細な仕様は公開情報が限定的）

### 2.4 オーディオハードウェア

- **マイクロフォンアレイ**: 4マイクアレイ搭載
- **スピーカー**: 5Wスピーカー1基
- 音声ハードウェアはPC2（Jetson側）ではなく、RockChip系のMCU（サブコントローラ、IP例: 192.168.123.161）が直接制御している構成が確認されている。つまり「マイクの生データを取る」には、Jetson上のアプリからこのMCUが流すストリームを受け取る必要がある。

### 2.5 DOF・バッテリー・価格帯

- 標準機: 23自由度（脚6×2、腰1、腕5×2）。EDUグレードは器用な三指ハンドなどオプションで最大43自由度まで拡張可能。
- バッテリー: 9000mAh、連続稼働約2時間、54V/5A急速充電。
- 価格帯: 標準機 約$13,500〜、EDUグレードは要見積り（複数の情報源で$24,500〜$43,900超まで幅がある）。

---

## 3. 音声会話を実現する上でのG1固有の制約

ここがG1で音声会話システムを作る際に一番重要なポイント。

1. **マイク入力へのアクセスに制約がある**
   Unitree公式SDK（`unitree_sdk2_python`）経由でマイク音声を取得しようとすると、G1本体の「Voice Assistant（内蔵音声アシスタント）」がWake-up Conversation Modeに入っていないと音声データがゼロ値（無音）で返ってくる、という報告がコミュニティのIssueで上がっている。つまり、内蔵の音声アシスタント機能と自作の音声パイプラインが同じマイクリソースを取り合う形になっており、公式には「生マイクストリームを自由に横取りする」ための正式なAPIが十分に整備されていない。
   → 実際に動いている実装（後述の`unitree_converse`）では、公式SDKの高レベルAPIを経由せず、RockChip側MCUが流す **UDPマルチキャストの生PCMストリーム（16bit mono 16kHz、`239.168.123.161:5555`宛）** を直接受信する、という一段低レイヤーの回避策を取っている。

2. **スピーカー出力は比較的整備されている**
   `AudioClient`というAPIがあり、Jetson側から16kHz monoのPCM/WAVを送ると本体スピーカーで再生できる。TTSの出力はこの経路に流し込むのが素直。

3. **内蔵の音声アシスタント機能自体も存在する**
   G1にはUnitree製の音声アシスタント（ウェイクワード認識・簡易対話）が標準搭載されており、UnifoLM等のAI機能と統合されている。自作システムを作る場合、この内蔵機能とバッティングしないよう「内蔵アシスタントをOFFにする/共存させる」設計判断が必要。

---

## 4. アーキテクチャパターン（2つの実例と比較）

### パターンA: オンデバイス完結型（クラウド不要）

実例: `SaxionMechatronics/unitree_converse`（G1向けの実装、通称"Aletta"）

```
[4マイクアレイ]
   └(UDPマルチキャスト 16kHz PCM)→ [ASR: faster-whisper (base, CPU)]
                                          ↓ テキスト
                          [ロボット状態を注入したプロンプト]
                                          ↓
                        [LLM: Ollama上のLlama 3.2 3B (GPU推論)]
                                          ↓ 応答テキスト
                              [TTS: Piper (en_US-lessac-medium)]
                                          ↓ 16kHz PCM
                        [AudioClient API] → [本体スピーカー]
```

- 全処理をJetson Orin NX上で完結（Ubuntu 20.04 + ROS2 Foxy）。
- ボタン（F1）を押している間だけ録音するプッシュトゥトーク方式。無音検出（閾値0.008、2秒継続）で発話終了、最大録音8秒。
- 体感の応答速度は明記されていないが、CPU上のWhisper base + 3Bクラス小型LLMという構成から、発話終了後おおよそ2〜4秒程度のレイテンシが妥当な推測値。
- 長所: ネット接続不要、プライバシー担保、フィールド作業でも安定動作。
- 短所: 3BクラスのLLMなので対話の質・知識量はクラウドの大規模モデルに劣る。日本語対応も要検証（このリポジトリは英語音声前提）。

### パターンB: クラウドAPI活用型（リアルタイムAPI or 逐次処理）

実例: Unitree Go2向けの実装例だが、G1にもそのまま応用できる構成。

```
[マイク] → [ローカルで録音: arecord/pyaudio]
        → [ASR: OpenAI Whisper API または Google Speech]
        → [LLM: ChatGPT API]
        → [TTS: クラウドTTS or pyttsx3]
        → [スピーカー再生]
```

- 有線/無線LAN経由でインターネットに出られることが前提（現場のWi-Fi品質に依存）。
- GPT-3.5クラスで短い応答なら1〜2秒、全体で「発話→応答再生開始」まで約3秒程度という報告値。
- 長所: 大規模モデルの応答品質、開発の速さ（公式SDKに直接のASR/TTSエンドポイントが無いため、結局OS標準のオーディオユーティリティか非公式ライブラリを使う点はオンデバイス型と同様）。
- 短所: ネット依存、クラウドAPIコスト、ラウンドトリップレイテンシ。

### パターンC: ハイブリッド構成（当初案・現在は不採用）

上記2つの折衷案として当初検討したもの。ASRとTTSはローカル固定、LLMのみクラウド/ローカルを切り替える構成。ゼロから音声パイプラインを構築する前提でのレイテンシ・オフライン耐性重視の判断だったが、後述の理由により今回は採用しない。

```
[4マイクアレイ] --UDPマルチキャスト(16kHz PCM)--> [VAD + ローカルASR]
                                                          ↓ テキスト
                                    ネットが安定していれば ┬→ クラウドLLM (高品質・高レイテンシ許容時)
                                                          └→ ローカル小型LLM (オフライン・低レイテンシ優先時)
                                                          ↓ 応答テキスト
                                              [ローカルTTS: Piper/VOICEVOX等]
                                                          ↓
                                        [AudioClient API] → スピーカー
```

---

## 5. 結論の更新（2026-08-25 追記）：パターンBを採用

初版レポートではパターンC（ハイブリッド構成）を推奨としたが、これは「ASR/LLM/TTSをゼロから統合する」ことを前提にした一般論的な判断だった。その後の検討で、**GDL（Ghost Description Language）側に既にSTT→LLM→TTSの一気通貫パイプラインが機能として存在し、かつ音声I/O部分はプラグイン構造で任意のハードウェアに合わせられる**ことが判明したため、結論を修正する。

### 5.1 採用するアーキテクチャ

```
[G1: 4マイクアレイ]
   └(UDPマルチキャスト 16kHz PCM)→ [GDL 音声I/Oプラグイン（G1用アダプタ）]
                                            ↓
                                   [GDL: STT → LLM → TTS パイプライン]
                                            ↓ 16kHz PCM
                        [GDL 音声I/Oプラグイン] → [AudioClient API] → [本体スピーカー]
```

- STT/LLM/TTSの中身はGDL本体の既存実装をそのまま使う（=実質パターンBの構成をGDLが既に体現している）。
- G1側で新規に作る部分は「音声I/Oプラグイン（アダプタ層）」のみに限定される。具体的には次の2つ。
  1. **マイク入力アダプタ**: RockChip MCUが流すUDPマルチキャスト（`239.168.123.161:5555`、16bit mono 16kHz PCM）を受信し、GDLのSTT入力インターフェースに渡す。
  2. **スピーカー出力アダプタ**: GDLのTTS出力（PCM/WAV）を`AudioClient` API経由でG1本体スピーカーに送る。
- パターンC（ローカルASR/TTSを自前で持つ構成）は、GDLという既存の完成された資産と機能が重複するため不採用とした。GDLがクラウドLLM前提で作られているのであれば、オフライン耐性が必要な場面（現場でネットが不安定など）が出てきた時点で、GDL側にローカルLLMへのフォールバック機構を追加するかどうかを別途検討する、という位置づけにする。

### 5.2 残る検討事項

- GDLの音声I/Oプラグインインターフェース（音声データのフォーマット、サンプリングレート、呼び出し方）の仕様確認。
- G1側の内蔵音声アシスタント（Wake-up Conversation Mode）とGDLプラグインのマイクストリーム受信が競合しないかの実機検証。
- UDPマルチキャスト受信からAudioClient送信までのレイテンシの実測。

---

## 6. 実装コード：G1音声I/Oアダプタ層

5章の方針に基づき、G1側で新規実装が必要な「音声I/Oアダプタ層」の実装コード。GDL本体のSTT/LLM/TTSインターフェース仕様が未確定のため、`g1_gdl_audio_plugin.py` の基底クラス部分（`GDLAudioIOPluginBase`）は仮のインターフェースとして書いてある。マイク・スピーカーそれぞれのG1固有ロジック（`G1MicInputAdapter` / `G1SpeakerOutputAdapter`）は実際に動作しているG1向け実装（`unitree_converse`）や公式SDKのソースから確認した仕様に基づいており、そのまま使い回せる想定。

### 6.1 マイク入力アダプタ（`g1_mic_input_adapter.py`）

RockChip系MCUが流すUDPマルチキャスト（`239.168.123.161:5555`、16bit mono 16kHz PCM）を受信し、コールバックへPCMチャンクを渡す。

```python
"""
g1_mic_input_adapter.py

Unitree G1 の内蔵4マイクアレイから音声を取得し、GDL (Ghost Description Language)
の音声入力プラグインへ橋渡しするアダプタ層。

【出典・確認済み事実】
G1本体のマイクはPC2(Jetson)ではなくRockChip系サブコントローラが直接制御しており、
16bit mono 16kHz PCMのストリームを UDPマルチキャスト (239.168.123.161:5555) で
垂れ流している。これは実際に動作しているG1向け実装
(SaxionMechatronics/unitree_converse の stt_node.py) から確認したUDP受信コードに
基づく。マルチキャストグループ参加にはローカルIP(PC2側 = 192.168.123.164)の指定が必要。

【要確認・要調整の前提】
- GDL側の「音声入力プラグイン」が実際にどんなインターフェース(基底クラス/コールバック
  シグネチャ)を要求するかはまだ確認していない。ここでは「PCMチャンク(bytes)を
  コールバックで渡す」「明示的にstart/stopする」という一般的な形にしてあるので、
  実際のGDLプラグインABCに合わせてクラス継承・メソッド名を調整すること。
- G1内蔵の音声アシスタントが有効(Wake-up Conversation Mode等)だと、このストリームを
  同時に使えない/取り合いになる可能性がある。実機で内蔵アシスタントとの共存可否を要検証。
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Callable, Optional


class G1MicInputAdapter:
    """
    G1のマイクストリーム(UDPマルチキャスト)を受信し、
    16kHz mono 16bit PCMのバイト列チャンクをコールバックへ渡すアダプタ。

    GDL側の音声入力プラグイン基底クラスがあるなら、そこを継承するか、
    on_audio_chunk コールバックをGDLのプラグイン登録APIに渡す形で接続する。
    """

    # G1本体からのマイクストリームの固定仕様（実装確認済み）
    SAMPLE_RATE = 16000
    CHANNELS = 1
    SAMPLE_WIDTH = 2  # bytes (16-bit)

    def __init__(
        self,
        on_audio_chunk: Callable[[bytes], None],
        multicast_group: str = "239.168.123.161",
        port: int = 5555,
        local_ip: str = "192.168.123.164",  # G1 PC2 (Jetson) のIP。環境に合わせて変更。
        recv_timeout: float = 0.2,
    ):
        """
        Args:
            on_audio_chunk: 受信したPCMチャンク(bytes)を渡すコールバック。
                             GDLのASR入力APIに直接繋ぐことを想定。
            multicast_group: G1マイクストリームのマルチキャストアドレス。
            port: マルチキャストのポート番号。
            local_ip: マルチキャストグループに参加する側(PC2)のローカルIP。
            recv_timeout: recvfromのタイムアウト秒数。stop()の応答性に影響。
        """
        self._on_audio_chunk = on_audio_chunk
        self._multicast_group = multicast_group
        self._port = port
        self._local_ip = local_ip
        self._recv_timeout = recv_timeout

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()

    def start(self) -> None:
        """マイクストリームの受信を開始する（非ブロッキング、別スレッドで受信）。"""
        if self._running.is_set():
            return

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self._sock.bind(("", self._port))

        mreq = struct.pack(
            "4s4s",
            socket.inet_aton(self._multicast_group),
            socket.inet_aton(self._local_ip),
        )
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self._sock.settimeout(self._recv_timeout)

        self._running.set()
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """マイクストリームの受信を停止する。"""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _recv_loop(self) -> None:
        assert self._sock is not None
        while self._running.is_set():
            try:
                data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            if data:
                try:
                    self._on_audio_chunk(data)
                except Exception:
                    # コールバック側の例外で受信ループを落とさない。
                    # 実運用ではロガーに出す。
                    pass

    def __enter__(self) -> "G1MicInputAdapter":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


# --- 動作確認用の簡易サンプル ------------------------------------------------
if __name__ == "__main__":
    import numpy as np

    def _debug_on_chunk(pcm_bytes: bytes) -> None:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples**2))) if len(samples) else 0.0
        print(f"received {len(pcm_bytes)} bytes, rms={rms:.4f}")

    adapter = G1MicInputAdapter(on_audio_chunk=_debug_on_chunk)
    print("listening... Ctrl+C to stop")
    with adapter:
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
```

### 6.2 スピーカー出力アダプタ（`g1_speaker_output_adapter.py`）

`unitree_sdk2_python` の公式API `AudioClient.PlayStream()` を使い、GDLのTTS出力を16kHz mono PCMへ変換してG1本体スピーカーへ送る。公式サンプル準拠で約96,000バイト（≒3秒）単位にチャンク分割する。

```python
"""
g1_speaker_output_adapter.py

GDL (Ghost Description Language) のTTS出力を、Unitree G1本体スピーカーへ
送るアダプタ層。

【出典・確認済み事実】
G1の音声出力は unitree_sdk2_python が公式に提供する
`unitree_sdk2py.g1.audio.g1_audio_client.AudioClient` クラス経由で行う。

    AudioClient.PlayStream(app_name: str, stream_id: str, pcm_data: bytes) -> code
    AudioClient.PlayStop(app_name: str) -> code

このAPIは **16kHz mono 16bit PCM** を要求する（Unitree公式サンプル
`example/g1/audio/g1_audio_client_play_wav.py` で "must be 16kHz mono" と明記）。
1回のPlayStream呼び出しで送れるデータ量には実務上の上限があるため、公式サンプル
(`example/g1/audio/wav.py` の `play_pcm_stream`)ではチャンクサイズ 96000 バイト
（16kHz mono 16bitで約3秒分）に分割し、chunk間に約1秒のsleepを挟んで送信している。
本アダプタもこのチャンク送信パターンを踏襲する。

なお `AudioClient.TtsMaker()` というG1内蔵TTSを呼び出すAPIも存在するが、これは
「G1本体の内蔵音声合成エンジン」を使うものであり、GDLのTTSとは別物なので今回は使わない
（GDLが生成した音声波形をそのまま再生したいので `PlayStream` を使う）。

【要確認・要調整の前提】
- GDL側の「音声出力プラグイン」の呼び出しインターフェース(基底クラス/メソッド名/
  引数でのサンプルレート・チャンネル数の渡し方)は未確認。ここでは
  `play(pcm_bytes, sample_rate, channels)` という一般的な形にしてあるので、
  実際のGDLプラグインABCに合わせて調整すること。
- GDLのTTSが16kHz mono以外(例: 22.05kHz, ステレオ)で出力する場合はリサンプル/
  ダウンミックスが必須。本アダプタにはnumpyベースの簡易リサンプラを同梱した。
"""

from __future__ import annotations

import time
from typing import Optional

import numpy as np

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient


class G1SpeakerOutputAdapter:
    """
    GDLのTTS出力(PCM)を受け取り、G1本体スピーカーで再生するアダプタ。

    使い方:
        adapter = G1SpeakerOutputAdapter(network_interface="eth0")
        adapter.init()
        adapter.play(pcm_bytes, sample_rate=22050, channels=1)  # 自動でリサンプルされる
        ...
        adapter.close()
    """

    TARGET_SAMPLE_RATE = 16000
    TARGET_CHANNELS = 1
    CHUNK_BYTES = 96000  # 公式サンプル準拠（16kHz mono 16bitで約3秒分）
    CHUNK_SLEEP_SEC = 1.0
    APP_NAME = "gdl_voice"

    def __init__(self, network_interface: str = "eth0", timeout_sec: float = 10.0):
        self._network_interface = network_interface
        self._timeout_sec = timeout_sec
        self._client: Optional[AudioClient] = None

    def init(self) -> None:
        """DDS通信の初期化とAudioClientのセットアップ。プロセス起動時に一度だけ呼ぶ。"""
        ChannelFactoryInitialize(0, self._network_interface)
        self._client = AudioClient()
        self._client.SetTimeout(self._timeout_sec)
        self._client.Init()

    def close(self) -> None:
        if self._client is not None:
            self._client.PlayStop(self.APP_NAME)

    def set_volume(self, volume: int) -> None:
        assert self._client is not None, "call init() first"
        self._client.SetVolume(volume)

    def play(self, pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> None:
        """
        GDLから受け取ったPCM音声をG1スピーカーで再生する。
        sample_rate/channelsがG1要件(16kHz mono)と異なる場合は自動変換する。
        """
        assert self._client is not None, "call init() first"

        pcm_bytes = self._to_g1_format(pcm_bytes, sample_rate, channels)

        stream_id = str(int(time.time() * 1000))
        offset = 0
        total = len(pcm_bytes)

        while offset < total:
            chunk = pcm_bytes[offset : offset + self.CHUNK_BYTES]
            code = self._client.PlayStream(self.APP_NAME, stream_id, chunk)
            if code != 0:
                raise RuntimeError(f"AudioClient.PlayStream failed: code={code}")
            offset += len(chunk)
            if offset < total:
                time.sleep(self.CHUNK_SLEEP_SEC)

        self._client.PlayStop(self.APP_NAME)

    def _to_g1_format(self, pcm_bytes: bytes, sample_rate: int, channels: int) -> bytes:
        """16bit PCMを前提に、16kHz monoへ変換する。"""
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)

        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)

        if sample_rate != self.TARGET_SAMPLE_RATE:
            samples = self._resample(samples, sample_rate, self.TARGET_SAMPLE_RATE)

        return samples.astype("<i2").tobytes()

    @staticmethod
    def _resample(samples: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        if orig_sr == target_sr or len(samples) == 0:
            return samples
        duration = len(samples) / orig_sr
        target_len = max(1, int(duration * target_sr))
        resampled = np.interp(
            np.linspace(0, len(samples) - 1, target_len),
            np.arange(len(samples)),
            samples.astype(np.float32),
        )
        return resampled.astype(np.int16)


# --- 動作確認用の簡易サンプル ------------------------------------------------
if __name__ == "__main__":
    import sys
    import wave

    if len(sys.argv) < 3:
        print(f"usage: python3 {sys.argv[0]} <network_interface> <wav_path>")
        sys.exit(1)

    net_if, wav_path = sys.argv[1], sys.argv[2]

    with wave.open(wav_path, "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        sr = wf.getframerate()
        ch = wf.getnchannels()

    adapter = G1SpeakerOutputAdapter(network_interface=net_if)
    adapter.init()
    adapter.play(pcm, sample_rate=sr, channels=ch)
    adapter.close()
```

### 6.3 GDLプラグインラッパー（`g1_gdl_audio_plugin.py`）

上記2つのアダプタを束ね、GDLの音声I/Oプラグインとして登録できる形にまとめたラッパー。`GDLAudioIOPluginBase` はGDL側の実際のプラグイン基底クラスが未確認のための仮実装であり、実仕様が判明次第差し替えが必要（本レポート5.2節の残課題）。

```python
"""
g1_gdl_audio_plugin.py

G1MicInputAdapter / G1SpeakerOutputAdapter を束ねて、GDLの
「音声I/Oプラグイン」として登録できる形にまとめたラッパー。

【重要な注意】
GDL本体が実際に要求するプラグイン基底クラス・メソッドシグネチャは未確認のため、
このファイルの `GDLAudioIOPluginBase` は「入出力プラグインは大体こういう形だろう」
という一般的な仮の抽象クラスとして書いてある。実際のGDLのプラグインAPIが判明したら、
このクラスをGDL側の本物の基底クラスに差し替えて、メソッド名・引数を合わせること。
（マイク/スピーカー個別のアダプタ実装=G1固有ロジックの部分はそのまま使い回せるはず。）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from g1_mic_input_adapter import G1MicInputAdapter
from g1_speaker_output_adapter import G1SpeakerOutputAdapter


# ---- ここから下は「GDL側の想定インターフェース」の仮置き -------------------
class GDLAudioIOPluginBase(ABC):
    """仮のGDL音声I/Oプラグイン基底クラス。実物に差し替えること。"""

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def play(self, pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> None:
        """GDLのTTSが生成した音声を鳴らす。"""
        ...

    # マイク入力はGDL側がコールバック登録方式を要求する想定。
    # (ジェネレータ/asyncストリーム方式を要求するGDL実装であれば、
    #  G1MicInputAdapterのon_audio_chunkをasyncio.Queueに積む形に変更すればよい)
    @abstractmethod
    def set_audio_chunk_handler(self, handler: Callable[[bytes], None]) -> None:
        ...
# ---------------------------------------------------------------------------


class G1AudioIOPlugin(GDLAudioIOPluginBase):
    """
    GDLから見た「G1用オーディオI/Oプラグイン」の実装。

    使い方（イメージ）:
        plugin = G1AudioIOPlugin(network_interface="eth0")
        gdl.register_audio_plugin(plugin)     # ← 実際のGDL側APIに合わせて呼び方は変わる
        plugin.start()
        ...
        plugin.stop()
    """

    def __init__(
        self,
        network_interface: str = "eth0",
        mic_multicast_group: str = "239.168.123.161",
        mic_port: int = 5555,
        mic_local_ip: str = "192.168.123.164",
    ):
        self._speaker = G1SpeakerOutputAdapter(network_interface=network_interface)
        self._chunk_handler: Optional[Callable[[bytes], None]] = None
        self._mic: Optional[G1MicInputAdapter] = None

        self._mic_multicast_group = mic_multicast_group
        self._mic_port = mic_port
        self._mic_local_ip = mic_local_ip

    def set_audio_chunk_handler(self, handler: Callable[[bytes], None]) -> None:
        """GDLのSTT入力側がここにコールバックを登録し、マイクの生PCMを受け取る。"""
        self._chunk_handler = handler

    def start(self) -> None:
        if self._chunk_handler is None:
            raise RuntimeError(
                "set_audio_chunk_handler() を先に呼び、GDL側のSTT入力コールバックを"
                "登録してください。"
            )

        self._speaker.init()

        self._mic = G1MicInputAdapter(
            on_audio_chunk=self._chunk_handler,
            multicast_group=self._mic_multicast_group,
            port=self._mic_port,
            local_ip=self._mic_local_ip,
        )
        self._mic.start()

    def stop(self) -> None:
        if self._mic is not None:
            self._mic.stop()
            self._mic = None
        self._speaker.close()

    def play(self, pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> None:
        """GDLのTTS出力をG1スピーカーで再生する。"""
        self._speaker.play(pcm_bytes, sample_rate=sample_rate, channels=channels)

    def set_volume(self, volume: int) -> None:
        self._speaker.set_volume(volume)
```

---

## 7. 参考情報源

- [Unitree G1 公式サイト](https://www.unitree.com/g1/)
- [Unitree G1 Overview - QRE Docs](https://docs.quadruped.de/projects/g1/html/g1_overview.html)
- [Unitree G1 ROS2 Driver - QRE Docs](https://docs.quadruped.de/projects/g1/html/g1_ros2_driver.html)
- [unitree_sdk2 (GitHub)](https://github.com/unitreerobotics/unitree_sdk2)
- [unitree_sdk2_python (GitHub)](https://github.com/unitreerobotics/unitree_sdk2_python)
- [Is there any way to get Microphone's audio input in G1's PC2? (GitHub Issue #143)](https://github.com/unitreerobotics/unitree_sdk2_python/issues/143)
- [SaxionMechatronics/unitree_converse (GitHub)](https://github.com/SaxionMechatronics/unitree_converse)
- [Configuring Unitree Go2 EDU for Real-Time Voice Interaction with OpenAI (HackMD)](https://hackmd.io/@c12hQ00ySVi6JYIERU7bCg/ByAOr12qJg)
- [Unitree G1 Price 2026 (theresarobotforthat.com)](https://theresarobotforthat.com/blog/unitree-g1-price/)
- [Unitree G1 EDU Ultimate Technical Specifications (RoboStore)](https://robostore.com/blogs/news/unitree-g1-edu-ultimate-technical-specifications)

※本レポートは複数のサードパーティ情報源（コミュニティリポジトリ、レビューサイト）を含んでおり、価格・スペックの一部は情報源間で差異があった点に留意すること。特に「マイク生データ取得の制約」はコミュニティ報告ベースであり、Unitree公式のドキュメントでは明示的に確認できていない。
