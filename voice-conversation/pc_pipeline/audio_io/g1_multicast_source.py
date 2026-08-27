"""
pc_pipeline.audio_io.g1_multicast_source

G1のマイク音声をUDPマルチキャストで直接受信するAudioSource。

【重要】ここはUnitree SDKを一切使わない。Python標準のsocketだけで動くので、
Windows/macOS/Linuxどれでも動作する。SDK(DDS)が必要なのはスピーカー出力側だけ。

G1のマイクはPC2(Jetson)ではなくRockChip系のMCUが制御していて、
16bit mono 16kHz PCM を 239.168.123.161:5555 宛にマルチキャスト配信している。

【前提】
- PCがG1と同一のL2セグメントにいること（首のEthernetポートに有線直結）
- PCのIPを 192.168.123.99/24 などG1と同じサブネットに設定すること
- 複数NICがある場合、local_ip の指定を間違えると別NICで待ち受けて無音になる
"""

from __future__ import annotations

import socket
import struct

from ..config import NetworkConfig
from .base import ThreadedQueueSource


class G1MulticastSource(ThreadedQueueSource):
    """G1マイクのマルチキャストストリームを受信する。"""

    def __init__(self, network: NetworkConfig, recv_timeout: float = 0.2):
        super().__init__()
        self._network = network
        self._recv_timeout = recv_timeout
        self._sock: socket.socket | None = None

    def _open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # SO_REUSEPORTはLinux/macOSにはあるがWindowsには無いので、あれば設定する
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass

        sock.bind(("", self._network.mic_port))

        # マルチキャストグループに参加する。
        # 第2引数のローカルIPで「どのNICで受けるか」が決まるので明示が重要。
        mreq = struct.pack(
            "4s4s",
            socket.inet_aton(self._network.mic_multicast_group),
            socket.inet_aton(self._network.local_ip),
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(self._recv_timeout)

        self._sock = sock

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _receive_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return

        while self._running.is_set():
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                # stop()でソケットが閉じられた場合など
                break
            self._push(data)
