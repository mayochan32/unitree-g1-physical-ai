"""
pc_pipeline.audio_io.local_mic_source

PC本体のマイクを使うAudioSource。

実機に行く前にPCだけでパイプライン全体（STT→LLM→TTS）を組み上げて
テストするためのもの。G1が無くても開発を進められる。

sounddevice が必要:  pip install sounddevice
"""

from __future__ import annotations

from ..config import G1_SAMPLE_RATE
from .base import ThreadedQueueSource


class LocalMicSource(ThreadedQueueSource):
    """
    PCのマイクから16kHz mono 16bit PCMを取り込む。

    G1のマイクと同じフォーマットで吐くので、パイプラインから見ると
    G1MulticastSourceと完全に等価に扱える。
    """

    def __init__(self, blocksize: int = 1600, device: int | None = None):
        """
        Args:
            blocksize: 1チャンクのサンプル数。1600 = 16kHzで0.1秒分。
            device: 入力デバイス番号。Noneなら既定のデバイス。
        """
        super().__init__()
        self._blocksize = blocksize
        self._device = device
        self._stream = None

    def _open(self) -> None:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError(
                "LocalMicSourceには sounddevice が必要です: pip install sounddevice"
            ) from exc

        def callback(indata, _frames, _time, status):
            if status:
                # オーバーフロー等。落とさずに続行する。
                pass
            self._push(bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=G1_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=self._blocksize,
            device=self._device,
            callback=callback,
        )
        self._stream.start()

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _receive_loop(self) -> None:
        # sounddeviceがコールバックで供給してくれるので、ここは待つだけ。
        # （_runningはセット済みなのでEvent.wait()は即座に返る。time.sleepを使う）
        import time

        while self._running.is_set():
            time.sleep(0.1)
