"""
pc_pipeline.audio_io.g1_direct_sink

Unitree SDKを直接使ってG1スピーカーを鳴らすAudioSink。

【使えるのはSDKが動く環境のみ】
unitree_sdk2_python + cyclonedds が必要。公式のインストール手順がLinux前提
なので、実質Linux機（またはG1のPC2上）でのみ使う想定。
Windowsから使う場合はブリッジ経由(G1BridgeSink)を推奨。

このクラスはG1のPC2上でパイプラインごと動かす場合にも使える。
"""

from __future__ import annotations

import time

from ..audio_utils import pcm_duration_sec, to_g1_format
from ..config import G1_SAMPLE_RATE, NetworkConfig
from .base import AudioSink

# 公式サンプル(example/g1/audio/wav.py)準拠のチャンクサイズ。
# 96000バイト = 16kHz * 2byte * 3秒
_CHUNK_BYTES = 96000
_CHUNK_SLEEP_SEC = 1.0

_APP_NAME = "pc_pipeline"


class G1DirectSink(AudioSink):
    """AudioClient.PlayStream() で直接再生する。"""

    def __init__(self, network: NetworkConfig, timeout_sec: float = 10.0):
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
        except ImportError as exc:
            raise RuntimeError(
                "G1DirectSinkには unitree_sdk2_python が必要です。\n"
                "WindowsなどSDKが入らない環境では --sink g1-bridge を使ってください。"
            ) from exc

        ChannelFactoryInitialize(0, network.dds_interface)
        self._client = AudioClient()
        self._client.SetTimeout(timeout_sec)
        self._client.Init()

    def set_volume(self, volume: int) -> None:
        self._client.SetVolume(volume)

    def play(self, pcm: bytes, sample_rate: int, channels: int = 1) -> None:
        g1_pcm = to_g1_format(pcm, sample_rate, channels)
        if not g1_pcm:
            return

        duration = pcm_duration_sec(g1_pcm, G1_SAMPLE_RATE)
        stream_id = str(int(time.time() * 1000))

        started = time.monotonic()
        offset = 0
        while offset < len(g1_pcm):
            chunk = g1_pcm[offset : offset + _CHUNK_BYTES]
            code = self._client.PlayStream(_APP_NAME, stream_id, chunk)
            if code != 0:
                raise RuntimeError(f"AudioClient.PlayStream に失敗: code={code}")
            offset += len(chunk)
            if offset < len(g1_pcm):
                time.sleep(_CHUNK_SLEEP_SEC)

        # 送信完了 != 再生完了。実際に鳴り終わるまで待つ。
        # ここを待たないと、まだ鳴っている音をマイクが拾ってエコーになる。
        remaining = duration - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)

        self._client.PlayStop(_APP_NAME)

    def stop(self) -> None:
        try:
            self._client.PlayStop(_APP_NAME)
        except Exception:
            pass
