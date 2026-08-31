"""
pc_pipeline.audio_io.g1_bridge_sink

G1のPC2上で動くブリッジにTCPでPCMを送り、G1本体のスピーカーで再生させるAudioSink。

【この方式の理由】
G1スピーカーの再生には AudioClient.PlayStream() = DDS通信が必要で、
unitree_sdk2_python + cyclonedds が要る。この依存はLinux前提なので、
Windows PCから直接叩くのはリスクが高い。

そこでSDK依存部分だけをG1側（Jetson/Ubuntu）に置く。さらにG1は共有機材なので、
**G1側に何もインストールさせない**ため、通信は標準ライブラリだけで書ける
素のTCPにしてある（WebSocket版は g1_bridge_ws_sink.py にバックアップとして残してある）。

play()は playback_done を受け取るまでブロックする。これによりエコー対策
（再生中はマイクを捨てる）の区間を正確に取れる。
"""

from __future__ import annotations

import socket

from ..audio_utils import pcm_duration_sec, to_g1_format
from ..config import G1_SAMPLE_RATE, NetworkConfig
from .base import AudioSink
from .bridge_protocol import BridgeConnection, connect

# 1フレームあたりの送信バイト数。16kHz mono 16bitで0.5秒分。
_SEND_CHUNK_BYTES = 16000

# 再生完了待ちの上限。音声長 + この余裕分。
_DONE_TIMEOUT_MARGIN_SEC = 15.0


class G1BridgeSink(AudioSink):
    """TCP経由でG1スピーカーに再生させる。"""

    def __init__(self, network: NetworkConfig, connect_timeout: float = 10.0):
        self._host = network.bridge_host
        self._port = network.bridge_port
        self._conn: BridgeConnection = connect(
            self._host, self._port, timeout=connect_timeout
        )

    def play(self, pcm: bytes, sample_rate: int, channels: int = 1) -> None:
        g1_pcm = to_g1_format(pcm, sample_rate, channels)
        if not g1_pcm:
            return

        duration = pcm_duration_sec(g1_pcm, G1_SAMPLE_RATE)
        self._conn.settimeout(duration + _DONE_TIMEOUT_MARGIN_SEC)

        self._conn.send_json({"type": "play_start", "sample_rate": G1_SAMPLE_RATE})
        for offset in range(0, len(g1_pcm), _SEND_CHUNK_BYTES):
            self._conn.send_pcm(g1_pcm[offset : offset + _SEND_CHUNK_BYTES])
        self._conn.send_json({"type": "play_end"})

        # ブリッジ側が「実際に鳴り終わった」と通知してくるまで待つ
        try:
            event = self._conn.recv_json()
        except socket.timeout as exc:
            raise TimeoutError("再生完了の通知が来ませんでした") from exc

        if event is None:
            raise RuntimeError("ブリッジとの接続が切れました")
        if event.get("type") == "error":
            raise RuntimeError(f"ブリッジ側でエラー: {event.get('message')}")
        if event.get("type") != "playback_done":
            raise RuntimeError(f"予期しない応答: {event}")

    def set_volume(self, volume: int) -> None:
        self._conn.send_json({"type": "set_volume", "volume": int(volume)})

    def stop(self) -> None:
        try:
            self._conn.send_json({"type": "stop"})
        except OSError:
            pass

    def close(self) -> None:
        self._conn.close()
