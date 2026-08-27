"""
pc_pipeline.audio_io.local_speaker_sink

PC本体のスピーカーで再生するAudioSink。

用途は2つ:
  1. 実機前の開発（G1が無くてもパイプライン全体を通せる）
  2. 当日G1にSSHできずブリッジを配置できなかった場合の縮退運転
     （マイクはG1から取り、音だけPCから出す。パイプラインが動くことは示せる）

sounddevice が必要:  pip install sounddevice
"""

from __future__ import annotations

from ..audio_utils import pcm_to_ndarray
from .base import AudioSink


class LocalSpeakerSink(AudioSink):
    """PCのスピーカーで鳴らす。play()は再生完了までブロックする。"""

    def __init__(self, device: int | None = None):
        self._device = device
        self._sd = None

    def _ensure_sd(self):
        if self._sd is None:
            try:
                import sounddevice as sd
            except ImportError as exc:
                raise RuntimeError(
                    "LocalSpeakerSinkには sounddevice が必要です: pip install sounddevice"
                ) from exc
            self._sd = sd
        return self._sd

    def play(self, pcm: bytes, sample_rate: int, channels: int = 1) -> None:
        if not pcm:
            return
        sd = self._ensure_sd()

        samples = pcm_to_ndarray(pcm)
        if channels > 1:
            usable = (samples.size // channels) * channels
            samples = samples[:usable].reshape(-1, channels)

        # PCのスピーカーはリサンプル不要（そのままのレートで鳴らせる）
        sd.play(samples, samplerate=sample_rate, device=self._device)
        sd.wait()  # 再生完了までブロック（エコー対策の同期に必要）

    def stop(self) -> None:
        if self._sd is not None:
            try:
                self._sd.stop()
            except Exception:
                pass
