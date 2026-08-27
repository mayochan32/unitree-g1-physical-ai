"""
pc_pipeline

Unitree G1 の音声会話パイプライン（PC側実行）。

  G1マイク → STT → LLM → TTS → G1スピーカー

音声の入り口(AudioSource)と出口(AudioSink)を抽象化してあるので、
G1が無い環境ではPCのマイク/スピーカーに差し替えて開発できる。
"""

__all__ = ["config", "audio_utils", "vad", "stt", "llm", "tts", "pipeline", "audio_io"]
