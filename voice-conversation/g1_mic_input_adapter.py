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
