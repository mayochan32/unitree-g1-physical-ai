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
