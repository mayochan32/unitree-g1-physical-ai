"""
pc_pipeline.stt

OpenAIの音声認識API（/v1/audio/transcriptions）を叩く。

G1のマイクから来る生PCMにWAVヘッダを付けて送る。
日本語なら language="ja" を明示した方が精度・速度ともに安定する。
"""

from __future__ import annotations

from .audio_utils import pcm_to_wav_bytes
from .config import G1_SAMPLE_RATE, OpenAIConfig


class SpeechToText:
    def __init__(self, config: OpenAIConfig, client=None):
        self._config = config
        self._client = client or _build_client(config)

    def transcribe(self, pcm: bytes, sample_rate: int = G1_SAMPLE_RATE) -> str:
        """PCMを文字起こしする。認識結果が空なら空文字を返す。"""
        if not pcm:
            return ""

        wav_bytes = pcm_to_wav_bytes(pcm, sample_rate)

        # ファイル名の拡張子でフォーマットを判別されるので .wav を付ける
        response = self._client.audio.transcriptions.create(
            model=self._config.stt_model,
            file=("utterance.wav", wav_bytes, "audio/wav"),
            language=self._config.language or None,
        )
        return (response.text or "").strip()


def _build_client(config: OpenAIConfig):
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openaiパッケージが必要です: pip install openai") from exc

    if not config.api_key:
        raise RuntimeError("環境変数 OPENAI_API_KEY が設定されていません")

    return OpenAI(api_key=config.api_key)
