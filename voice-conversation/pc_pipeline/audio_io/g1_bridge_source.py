"""
pc_pipeline.audio_io.g1_bridge_source

G1のマイク音声を、ブリッジ経由（TCP）で受け取るAudioSource。

【いつ使うか】
通常は G1MulticastSource（マルチキャスト直接受信）の方が経路が短くてよい。
ただし次の場合はマルチキャストが届かないので、こちらに切り替える。

  - PCをG1と同一L2セグメントに置けない（ルータ越しなど）
  - PCの複数NIC構成が複雑で、マルチキャストのjoinがうまくいかない
  - Windowsのファイアウォールで受信UDPがどうしても通らない

ブリッジとはTCPのユニキャストで喋るので、疎通さえすれば経路を問わない。
再生用のsinkとは別の接続を張る（多重化の複雑さを避けるため）。
"""

from __future__ import annotations

import socket

from ..config import NetworkConfig
from .base import ThreadedQueueSource
from .bridge_protocol import FRAME_PCM, BridgeConnection, connect


class G1BridgeSource(ThreadedQueueSource):
    """ブリッジからマイクPCMを中継してもらう。"""

    def __init__(self, network: NetworkConfig, connect_timeout: float = 10.0):
        super().__init__()
        self._host = network.bridge_host
        self._port = network.bridge_port
        self._connect_timeout = connect_timeout
        self._conn: BridgeConnection | None = None

    def _open(self) -> None:
        self._conn = connect(self._host, self._port, timeout=self._connect_timeout)
        self._conn.send_json({"type": "mic_start"})
        # 受信ループがstop()に反応できるよう、短めのタイムアウトで回す
        self._conn.settimeout(0.5)

    def _close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.settimeout(2.0)
            self._conn.send_json({"type": "mic_stop"})
        except OSError:
            pass
        self._conn.close()
        self._conn = None

    def _receiving_loop_guard(self) -> bool:
        return self._running.is_set() and self._conn is not None

    def _receive_loop(self) -> None:
        while self._receiving_loop_guard():
            conn = self._conn
            if conn is None:
                break
            try:
                frame = conn.recv_frame()
            except socket.timeout:
                continue
            except OSError:
                break

            if frame is None:
                break

            frame_type, payload = frame
            if frame_type == FRAME_PCM and payload:
                self._push(payload)
            # JSONフレーム（errorなど）はここでは無視する
