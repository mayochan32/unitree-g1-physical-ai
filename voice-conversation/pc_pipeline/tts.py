"""
pc_pipeline.tts

OpenAIのTTS API（/v1/audio/speech）で応答テキストを音声にする。

response_format="pcm" を指定すると、公式ドキュメントによれば
**24kHz・16bit signed・リトルエンディアン・ヘッダなし** の生PCMが返る。
G1スピーカーは16kHz monoを要求するので、この後で必ずリサンプルが要る
（変換はAudioSink側の to_g1_format() が担当する）。

比が 24000:16000 = 3:2 なので、scipyの resample_poly(up=2, down=3) で
きれいに落とせる。
"""

from __future__ import annotations

from .config import OPENAI_TTS_SAMPLE_RATE, OpenAIConfig


class TextToSpeech:
    def __init__(self, config: OpenAIConfig, client=None):
        self._config = config
        self._client = client or _build_client(config)

    @property
    def sample_rate(self) -> int:
        """このTTSが返すPCMのサンプルレート。"""
        return OPENAI_TTS_SAMPLE_RATE

    def synthesize(self, text: str) -> tuple[bytes, int]:
        """
        テキストを音声にする。

        Returns:
            (生PCMバイト列, サンプルレート)
        """
        if not text.strip():
            return b"", OPENAI_TTS_SAMPLE_RATE

        kwargs = {
            "model": self._config.tts_model,
            "voice": self._config.tts_voice,
            "input": text,
            "response_format": "pcm",
        }

        # instructions は gpt-4o-mini-tts 系でのみ有効。
        # tts-1 などに渡すとエラーになるので、弾かれたら外して再試行する。
        if self._config.tts_instructions:
            kwargs["instructions"] = self._config.tts_instructions

        try:
            response = self._client.audio.speech.create(**kwargs)
        except Exception:
            kwargs.pop("instructions", None)
            response = self._client.audio.speech.create(**kwargs)

        return response.content, OPENAI_TTS_SAMPLE_RATE


def _build_client(config: OpenAIConfig):
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openaiパッケージが必要です: pip install openai") from exc

    if not config.api_key:
        raise RuntimeError("環境変数 OPENAI_API_KEY が設定されていません")

    return OpenAI(api_key=config.api_key)
