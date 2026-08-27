"""
pc_pipeline.audio_io.g1_bridge_sink

G1のPC2上で動くブリッジ(g1_bridge/bridge_server.py)にWebSocketでPCMを送り、
G1本体のスピーカーで再生させるAudioSink。

【この構成の理由】
G1スピーカーの再生には AudioClient.PlayStream() = DDS通信が必要で、
これには unitree_sdk2_python + cyclonedds が要る。この依存はLinux前提のため、
Windows PCから直接叩くのは動作実績が確認できずリスクが高い。
そこでSDK依存部分だけをG1側（Jetson/Ubuntu = SDKが確実に動く環境）に置き、
PCとは素のWebSocketで喋る。PC側の依存は websockets だけで済む。

【プロトコル】
  PC → G1 : {"type":"play_start","sample_rate":16000} → PCMバイナリ… → {"type":"play_end"}
  G1 → PC : {"type":"playback_done"} / {"type":"error","message":...}

play()は playback_done を受け取るまでブロックする。これによりエコー対策
（再生中はマイクを捨てる）の区間を正確に取れる。

websockets が必要:  pip install websockets
"""

from __future__ import annotations

import asyncio
import json
import threading

from ..audio_utils import to_g1_format
from ..config import G1_SAMPLE_RATE, NetworkConfig
from .base import AudioSink

# 1フレームあたりの送信バイト数。16kHz mono 16bitで 0.5秒分。
_SEND_CHUNK_BYTES = 16000

# 再生完了待ちの上限。音声長 + この余裕分を超えたら諦める。
_DONE_TIMEOUT_MARGIN_SEC = 15.0


class G1BridgeSink(AudioSink):
    """WebSocket経由でG1スピーカーに再生させる。"""

    def __init__(self, network: NetworkConfig, connect_timeout: float = 10.0):
        self._url = network.bridge_url
        self._connect_timeout = connect_timeout

        # asyncioのイベントループを専用スレッドで回す。
        # パイプライン本体は同期コードなので、ここで非同期を隠蔽する。
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self._ws = None
        self._connect()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, timeout: float):
        """コルーチンをループスレッドで実行し、完了を同期的に待つ。"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # --- 接続管理 ---
    async def _aconnect(self) -> None:
        import websockets

        # 大きめのPCMを送るので上限を上げておく
        self._ws = await websockets.connect(self._url, max_size=32 * 1024 * 1024)

    def _connect(self) -> None:
        try:
            self._submit(self._aconnect(), timeout=self._connect_timeout)
        except Exception as exc:
            raise RuntimeError(
                f"G1ブリッジに接続できません ({self._url}): {exc}\n"
                "G1のPC2上で bridge_server.py が起動しているか、"
                "IPとポートの設定が正しいか確認してください。"
            ) from exc

    # --- 再生 ---
    async def _aplay(self, pcm: bytes, timeout: float) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("ブリッジに接続していません")

        await ws.send(json.dumps({"type": "play_start", "sample_rate": G1_SAMPLE_RATE}))

        for offset in range(0, len(pcm), _SEND_CHUNK_BYTES):
            await ws.send(pcm[offset : offset + _SEND_CHUNK_BYTES])

        await ws.send(json.dumps({"type": "play_end"}))

        # ブリッジ側が再生完了を通知してくるまで待つ
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("再生完了の通知が来ませんでした")

            message = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if isinstance(message, bytes):
                continue  # マイク中継のバイナリが混ざる場合は無視

            event = json.loads(message)
            kind = event.get("type")
            if kind == "playback_done":
                return
            if kind == "error":
                raise RuntimeError(f"ブリッジ側でエラー: {event.get('message')}")

    def play(self, pcm: bytes, sample_rate: int, channels: int = 1) -> None:
        from ..audio_utils import pcm_duration_sec

        g1_pcm = to_g1_format(pcm, sample_rate, channels)
        if not g1_pcm:
            return

        duration = pcm_duration_sec(g1_pcm, G1_SAMPLE_RATE)
        timeout = duration + _DONE_TIMEOUT_MARGIN_SEC
        self._submit(self._aplay(g1_pcm, timeout), timeout=timeout + 5.0)

    async def _astop(self) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps({"type": "stop"}))

    def stop(self) -> None:
        try:
            self._submit(self._astop(), timeout=5.0)
        except Exception:
            pass

    async def _aclose(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    def close(self) -> None:
        try:
            self._submit(self._aclose(), timeout=5.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)
