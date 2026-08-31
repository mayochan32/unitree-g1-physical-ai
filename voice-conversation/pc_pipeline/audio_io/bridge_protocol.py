"""
pc_pipeline.audio_io.bridge_protocol

G1ブリッジとの通信プロトコル（PC側）。標準ライブラリのみで実装してある。

フレーム形式は g1_bridge/bridge_server.py と対になっている。

    [1バイト: 種別][4バイト: ペイロード長(ビッグエンディアン)][ペイロード]

    種別 0x01 = JSON制御メッセージ
    種別 0x02 = 生PCM（16kHz mono 16bit LE）

WebSocketをやめて素のTCPにしたのは、**G1側に何もインストールさせないため**。
G1は共有機材なので、システムのPythonにパッケージを入れたくない。
"""

from __future__ import annotations

import json
import socket
import struct

FRAME_JSON = 0x01
FRAME_PCM = 0x02

_HEADER = struct.Struct("!BI")
MAX_FRAME_BYTES = 64 * 1024 * 1024


class BridgeConnection:
    """
    ブリッジへのTCP接続。1接続を1用途（再生 or マイク）に使う想定。

    再生用とマイク用で別々の接続を張ることで、多重化の複雑さを避けている。
    サーバ側は接続ごとにスレッドを立てるので同時接続で問題ない。
    """

    def __init__(self, host: str, port: int, timeout: float = 10.0):
        self._host = host
        self._port = port
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    @property
    def peer(self) -> str:
        return f"{self._host}:{self._port}"

    def settimeout(self, timeout: float | None) -> None:
        self._sock.settimeout(timeout)

    # --- 送信 ---
    def send_frame(self, frame_type: int, payload: bytes) -> None:
        self._sock.sendall(_HEADER.pack(frame_type, len(payload)) + payload)

    def send_json(self, obj: dict) -> None:
        self.send_frame(FRAME_JSON, json.dumps(obj).encode("utf-8"))

    def send_pcm(self, pcm: bytes) -> None:
        self.send_frame(FRAME_PCM, pcm)

    # --- 受信 ---
    def _recv_exact(self, count: int) -> bytes | None:
        chunks = []
        remaining = count
        while remaining > 0:
            chunk = self._sock.recv(min(remaining, 1 << 20))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def recv_frame(self) -> tuple[int, bytes] | None:
        """1フレーム受信する。接続が閉じたらNone。タイムアウトはsocket.timeoutを送出。"""
        header = self._recv_exact(_HEADER.size)
        if header is None:
            return None
        frame_type, length = _HEADER.unpack(header)
        if length > MAX_FRAME_BYTES:
            raise ValueError(f"フレームが大きすぎます: {length}バイト")
        payload = self._recv_exact(length) if length else b""
        if payload is None:
            return None
        return frame_type, payload

    def recv_json(self) -> dict | None:
        """JSONフレームが来るまで読む。PCMフレームは読み飛ばす。"""
        while True:
            frame = self.recv_frame()
            if frame is None:
                return None
            frame_type, payload = frame
            if frame_type == FRAME_JSON:
                return json.loads(payload.decode("utf-8"))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "BridgeConnection":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


def connect(host: str, port: int, timeout: float = 10.0) -> BridgeConnection:
    """
    ブリッジに接続する。失敗したら原因の当たりを付けやすいメッセージにして投げ直す。
    """
    try:
        return BridgeConnection(host, port, timeout=timeout)
    except OSError as exc:
        raise RuntimeError(
            f"G1ブリッジに接続できません (tcp://{host}:{port}): {exc}\n"
            "  - G1のPC2上で bridge_server.py が起動しているか\n"
            "  - IPとポートの設定が正しいか\n"
            "  - PCのファイアウォールが送信TCPを塞いでいないか"
        ) from exc
