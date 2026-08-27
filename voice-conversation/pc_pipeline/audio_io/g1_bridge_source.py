"""
pc_pipeline.audio_io.g1_bridge_source

G1のマイク音声を、G1側ブリッジ経由（WebSocket）で受け取るAudioSource。

【いつ使うか】
通常は G1MulticastSource（マルチキャスト直接受信）の方が経路が短くてよい。
ただし次の場合はマルチキャストが届かないので、こちらに切り替える。

  - PCをG1と同一L2セグメントに置けない（Wi-Fi経由、ルータ越しなど）
  - PCの複数NIC構成が複雑で、マルチキャストのjoinがうまくいかない

ブリッジはTCP(WebSocket)のユニキャストなので、疎通さえすれば経路を問わない。
"""

from __future__ import annotations

import asyncio
import json
import threading

from ..config import NetworkConfig
from .base import AudioSource


class G1BridgeSource(AudioSource):
    """ブリッジからマイクPCMを中継してもらう。"""

    def __init__(self, network: NetworkConfig, connect_timeout: float = 10.0):
        self._url = network.bridge_url
        self._connect_timeout = connect_timeout

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self._queue: asyncio.Queue[bytes] | None = None
        self._ws = None
        self._running = threading.Event()
        self._reader_task = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _astart(self) -> None:
        import websockets

        self._queue = asyncio.Queue(maxsize=256)
        self._ws = await websockets.connect(self._url, max_size=32 * 1024 * 1024)
        await self._ws.send(json.dumps({"type": "mic_start"}))
        self._reader_task = asyncio.create_task(self._areader())

    async def _areader(self) -> None:
        ws = self._ws
        queue = self._queue
        if ws is None or queue is None:
            return
        try:
            async for message in ws:
                if not isinstance(message, bytes):
                    continue  # 制御メッセージは無視
                if queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                await queue.put(message)
        except Exception:
            return

    def start(self) -> None:
        if self._running.is_set():
            return
        self._submit(self._astart(), timeout=self._connect_timeout)
        self._running.set()

    async def _aread(self, timeout: float) -> bytes | None:
        if self._queue is None:
            return None
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def read_chunk(self, timeout: float = 0.5) -> bytes | None:
        try:
            return self._submit(self._aread(timeout), timeout=timeout + 2.0)
        except Exception:
            return None

    async def _adrain(self) -> int:
        if self._queue is None:
            return 0
        dropped = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        return dropped

    def drain(self) -> int:
        try:
            return self._submit(self._adrain(), timeout=2.0)
        except Exception:
            return 0

    async def _astop(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "mic_stop"}))
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    def stop(self) -> None:
        self._running.clear()
        try:
            self._submit(self._astop(), timeout=5.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)
