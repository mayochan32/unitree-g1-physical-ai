"""
g1_bridge/bridge_server.py

G1のPC2（Jetson / Ubuntu）上で動かす薄い中継サーバ。

【役割】
Unitree SDK（DDS）依存の部分だけをここに閉じ込める。PC側はWebSocketで
喋るだけになるので、WindowsでもmacOSでもSDKを入れずに済む。

  PC ──WebSocket──> このサーバ ──AudioClient.PlayStream()──> G1スピーカー
  PC <─WebSocket─── このサーバ <──UDPマルチキャスト──── G1マイク（オプション）

【プロトコル】
  PC → G1
    {"type":"play_start","sample_rate":16000}
    <バイナリ: 16kHz mono 16bit LE PCM>  ...繰り返し
    {"type":"play_end"}
    {"type":"stop"}                      再生中断
    {"type":"mic_start"} / {"type":"mic_stop"}   マイク中継の制御

  G1 → PC
    {"type":"playback_done"}             ← 実際に鳴り終わってから送る
    {"type":"error","message":"..."}
    <バイナリ: マイクPCM>                 mic_start中のみ

【重要】playback_done は「送信完了」ではなく「再生完了」で返す。
PlayStreamは送るだけなので、音声長ぶんの時間が経つまで待ってから通知する。
これをしないとPC側のエコー対策の区間がずれて、自分の声を拾ってしまう。

起動:
    python3 bridge_server.py --iface eth0 --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import struct
import time

# --- G1の固定仕様 ---
G1_SAMPLE_RATE = 16000
G1_CHANNELS = 1
G1_SAMPLE_WIDTH = 2

# PlayStreamのチャンク分割（公式サンプル準拠: 96000バイト = 約3秒分）
CHUNK_BYTES = 96000
CHUNK_SLEEP_SEC = 1.0

APP_NAME = "pc_pipeline"

# マイクのマルチキャスト
MIC_MULTICAST_GROUP = "239.168.123.161"
MIC_PORT = 5555
MIC_LOCAL_IP = "192.168.123.164"  # G1のPC2自身のIP


class SpeakerPlayer:
    """AudioClientをラップして再生する。"""

    def __init__(self, iface: str, timeout_sec: float = 10.0):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

        ChannelFactoryInitialize(0, iface)
        self._client = AudioClient()
        self._client.SetTimeout(timeout_sec)
        self._client.Init()
        print(f"[bridge] AudioClient初期化完了 (iface={iface})", flush=True)

    def play_blocking(self, pcm: bytes) -> None:
        """
        PCMを再生する。実際に鳴り終わるまでブロックする。
        （PlayStreamは送信するだけなので、音声長ぶん待つ処理を自前で入れている）
        """
        if not pcm:
            return

        duration = len(pcm) / (G1_SAMPLE_RATE * G1_SAMPLE_WIDTH * G1_CHANNELS)
        stream_id = str(int(time.time() * 1000))
        started = time.monotonic()

        offset = 0
        while offset < len(pcm):
            chunk = pcm[offset : offset + CHUNK_BYTES]
            code = self._client.PlayStream(APP_NAME, stream_id, chunk)
            if code != 0:
                raise RuntimeError(f"PlayStream失敗: code={code}")
            offset += len(chunk)
            if offset < len(pcm):
                time.sleep(CHUNK_SLEEP_SEC)

        remaining = duration - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)

        self._client.PlayStop(APP_NAME)

    def stop(self) -> None:
        try:
            self._client.PlayStop(APP_NAME)
        except Exception:
            pass

    def set_volume(self, volume: int) -> None:
        self._client.SetVolume(volume)


def open_mic_socket() -> socket.socket:
    """マイクのマルチキャストを受けるソケットを開く。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("", MIC_PORT))
    mreq = struct.pack(
        "4s4s",
        socket.inet_aton(MIC_MULTICAST_GROUP),
        socket.inet_aton(MIC_LOCAL_IP),
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(0.2)
    return sock


class Session:
    """1つのWebSocket接続に対応する状態。"""

    def __init__(self, websocket, player: SpeakerPlayer):
        self._ws = websocket
        self._player = player
        self._buffer = bytearray()
        self._receiving = False
        self._mic_task: asyncio.Task | None = None

    async def handle_message(self, message) -> None:
        # バイナリ = 再生するPCM
        if isinstance(message, (bytes, bytearray)):
            if self._receiving:
                self._buffer.extend(message)
            return

        event = json.loads(message)
        kind = event.get("type")

        if kind == "play_start":
            self._buffer = bytearray()
            self._receiving = True

        elif kind == "play_end":
            self._receiving = False
            pcm = bytes(self._buffer)
            self._buffer = bytearray()
            await self._play(pcm)

        elif kind == "stop":
            await asyncio.to_thread(self._player.stop)

        elif kind == "set_volume":
            await asyncio.to_thread(self._player.set_volume, int(event.get("volume", 50)))

        elif kind == "mic_start":
            self._start_mic()

        elif kind == "mic_stop":
            self._stop_mic()

        else:
            await self._ws.send(
                json.dumps({"type": "error", "message": f"未知のtype: {kind}"})
            )

    async def _play(self, pcm: bytes) -> None:
        try:
            # PlayStreamは同期・ブロッキングなので別スレッドで回す
            await asyncio.to_thread(self._player.play_blocking, pcm)
            await self._ws.send(json.dumps({"type": "playback_done"}))
            print(f"[bridge] 再生完了 ({len(pcm)}バイト)", flush=True)
        except Exception as exc:
            await self._ws.send(json.dumps({"type": "error", "message": str(exc)}))
            print(f"[bridge] 再生エラー: {exc}", flush=True)

    def _start_mic(self) -> None:
        if self._mic_task is not None:
            return
        self._mic_task = asyncio.create_task(self._mic_loop())
        print("[bridge] マイク中継を開始", flush=True)

    def _stop_mic(self) -> None:
        if self._mic_task is not None:
            self._mic_task.cancel()
            self._mic_task = None
            print("[bridge] マイク中継を停止", flush=True)

    async def _mic_loop(self) -> None:
        sock = await asyncio.to_thread(open_mic_socket)
        try:
            while True:
                try:
                    data = await asyncio.to_thread(_recv, sock)
                except Exception:
                    await asyncio.sleep(0.05)
                    continue
                if data:
                    await self._ws.send(data)
        except asyncio.CancelledError:
            raise
        finally:
            sock.close()

    def cleanup(self) -> None:
        self._stop_mic()


def _recv(sock: socket.socket) -> bytes:
    try:
        data, _addr = sock.recvfrom(65535)
        return data
    except socket.timeout:
        return b""


def make_handler(player: SpeakerPlayer):
    async def handler(websocket, *_args):
        peer = getattr(websocket, "remote_address", "unknown")
        print(f"[bridge] 接続: {peer}", flush=True)
        session = Session(websocket, player)
        try:
            async for message in websocket:
                await session.handle_message(message)
        except Exception as exc:
            print(f"[bridge] 接続エラー: {exc}", flush=True)
        finally:
            session.cleanup()
            print(f"[bridge] 切断: {peer}", flush=True)

    return handler


async def run(iface: str, host: str, port: int) -> None:
    import websockets

    player = SpeakerPlayer(iface)
    handler = make_handler(player)

    # websockets 12〜14で serve の置き場所が変わっているので両対応にする
    try:
        from websockets.asyncio.server import serve  # websockets >= 13
    except ImportError:
        from websockets import serve  # type: ignore  # websockets < 13

    async with serve(handler, host, port, max_size=32 * 1024 * 1024):
        print(f"[bridge] ws://{host}:{port} で待機中", flush=True)
        await asyncio.Future()  # 永久に待つ


def main() -> None:
    parser = argparse.ArgumentParser(description="G1音声ブリッジ（PC2上で実行）")
    parser.add_argument("--iface", default="eth0", help="DDSに使うNIC名（既定: eth0）")
    parser.add_argument("--host", default="0.0.0.0", help="待ち受けアドレス")
    parser.add_argument("--port", type=int, default=8765, help="待ち受けポート")
    args = parser.parse_args()

    try:
        asyncio.run(run(args.iface, args.host, args.port))
    except KeyboardInterrupt:
        print("\n[bridge] 終了します", flush=True)


if __name__ == "__main__":
    main()
