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

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import OPENAI_TTS_SAMPLE_RATE, OpenAIConfig


class TextToSpeech:
    def __init__(self, config: OpenAIConfig, client=None):
        self._config = config
        self._client = client
        if config.tts_provider == "openai":
            self._client = client or _build_openai_client(config)
        elif config.tts_provider != "elevenlabs":
            raise RuntimeError(
                f"未対応のTTS_PROVIDERです: {config.tts_provider} "
                "(openai または elevenlabs を指定してください)"
            )

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

        if self._config.tts_provider == "elevenlabs":
            return self._synthesize_elevenlabs(text), OPENAI_TTS_SAMPLE_RATE

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

    def _synthesize_elevenlabs(self, text: str) -> bytes:
        if not self._config.elevenlabs_api_key:
            raise RuntimeError("環境変数 ELEVENLABS_API_KEY が設定されていません")
        if not self._config.elevenlabs_voice_id:
            raise RuntimeError("環境変数 ELEVENLABS_VOICE_ID が設定されていません")

        url = (
            "https://api.elevenlabs.io/v1/text-to-speech/"
            f"{self._config.elevenlabs_voice_id}?output_format=pcm_24000"
        )
        body = json.dumps(
            {
                "text": text,
                "model_id": self._config.elevenlabs_model,
            }
        ).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={
                "Accept": "audio/pcm",
                "Content-Type": "application/json",
                "xi-api-key": self._config.elevenlabs_api_key,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ElevenLabs TTS API エラー ({exc.code}): {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"ElevenLabs TTS API に接続できません: {exc.reason}") from exc


def _build_openai_client(config: OpenAIConfig):
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openaiパッケージが必要です: pip install openai") from exc

    if not config.api_key:
        raise RuntimeError("環境変数 OPENAI_API_KEY が設定されていません")

    return OpenAI(api_key=config.api_key)
