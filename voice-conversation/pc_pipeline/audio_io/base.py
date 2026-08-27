"""
pc_pipeline.audio_io.base

音声の入り口(AudioSource)と出口(AudioSink)の抽象。

この抽象を挟むことで、実行時に「G1のマイク／PCのマイク」「G1のスピーカー／
PCのスピーカー」を差し替えられる。実機に行く前にPCだけでパイプライン全体を
完成させておき、当日は接続確認と設定切り替えだけで済ませるための仕掛け。

AudioSourceが吐くPCMは必ず 16kHz mono 16bit LE に揃える（G1の仕様に合わせる）。
"""

from __future__ import annotations

import queue
import threading
from abc import ABC, abstractmethod


class AudioSource(ABC):
    """
    音声入力。16kHz mono 16bit LE のPCMチャンクを供給する。

    実装は内部で受信スレッドを回し、チャンクをキューに積むこと。
    「読まない」のではなく「読んで捨てる」必要があるため（UDPの受信バッファが
    溢れるのを防ぐ）、受信は常時動かし続け、破棄は drain() で行う設計にしている。
    """

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def read_chunk(self, timeout: float = 0.5) -> bytes | None:
        """PCMチャンクを1つ取り出す。タイムアウトしたらNone。"""
        ...

    @abstractmethod
    def drain(self) -> int:
        """
        溜まっているチャンクを全部捨てる。捨てた個数を返す。
        エコー対策で、再生中・再生直後に呼ぶ。
        """
        ...

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


class AudioSink(ABC):
    """
    音声出力。

    play() は **再生が終わるまでブロックする** 契約にしている。
    こうしないと、再生中にマイクが自分の声を拾ってしまう区間を正確に
    ガードできない（エコー対策の同期のため）。
    """

    @abstractmethod
    def play(self, pcm: bytes, sample_rate: int, channels: int = 1) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        """再生中の音を止める。"""
        ...

    def close(self) -> None:
        """後始末。必要な実装だけオーバーライドする。"""
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class ThreadedQueueSource(AudioSource):
    """
    「内部スレッドで受信し続けてキューに積む」型のAudioSourceの共通実装。

    サブクラスは _receive_loop() だけ実装すればよい。
    キューが溢れたら古いチャンクから捨てる（リアルタイム性を優先）。
    """

    def __init__(self, max_queue_chunks: int = 256):
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=max_queue_chunks)
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    # --- サブクラスが実装する ---
    def _open(self) -> None:
        """リソース確保（ソケットを開くなど）。"""
        return None

    def _close(self) -> None:
        """リソース解放。"""
        return None

    @abstractmethod
    def _receive_loop(self) -> None:
        """
        self._running がセットされている間、受信して self._push() を呼び続ける。
        """
        ...

    # --- 共通処理 ---
    def _push(self, chunk: bytes) -> None:
        if not chunk:
            return
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            # 最も古いものを捨てて、新しいものを入れる
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(chunk)
            except (queue.Empty, queue.Full):
                pass

    def start(self) -> None:
        if self._running.is_set():
            return
        self._open()
        self._running.set()
        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._close()

    def read_chunk(self, timeout: float = 0.5) -> bytes | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> int:
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                dropped += 1
            except queue.Empty:
                return dropped
