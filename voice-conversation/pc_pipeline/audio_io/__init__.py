"""
pc_pipeline.audio_io

音声I/Oの抽象と実装。

実装は遅延インポートする（各実装が sounddevice / websockets / unitree_sdk2py
といった別々のオプション依存を持つため、使わない実装の依存で落ちないように）。

ブリッジ経由の実装は2系統ある。

  g1-bridge     … 素のTCP（既定・推奨）
                  G1側は標準ライブラリだけで動くので、共有機材に何も入れずに済む
  g1-bridge-ws  … WebSocket（バックアップ）
                  G1側に websockets が必要。TCP版がうまくいかない場合の代替
"""

from __future__ import annotations

from .base import AudioSink, AudioSource, ThreadedQueueSource

__all__ = [
    "AudioSource",
    "AudioSink",
    "ThreadedQueueSource",
    "create_source",
    "create_sink",
    "SOURCE_CHOICES",
    "SINK_CHOICES",
]

SOURCE_CHOICES = ("g1-multicast", "g1-bridge", "g1-bridge-ws", "local-mic")
SINK_CHOICES = ("g1-bridge", "g1-bridge-ws", "g1-direct", "local-speaker")


def create_source(kind: str, config) -> AudioSource:
    """名前からAudioSourceを組み立てる。"""
    if kind == "g1-multicast":
        from .g1_multicast_source import G1MulticastSource

        return G1MulticastSource(config.network)

    if kind == "g1-bridge":
        from .g1_bridge_source import G1BridgeSource

        return G1BridgeSource(config.network)

    if kind == "g1-bridge-ws":
        from .g1_bridge_ws_source import G1BridgeWsSource

        return G1BridgeWsSource(config.network)

    if kind == "local-mic":
        from .local_mic_source import LocalMicSource

        return LocalMicSource()

    raise ValueError(f"未知のsource: {kind}（選択肢: {', '.join(SOURCE_CHOICES)}）")


def create_sink(kind: str, config) -> AudioSink:
    """名前からAudioSinkを組み立てる。"""
    if kind == "g1-bridge":
        from .g1_bridge_sink import G1BridgeSink

        return G1BridgeSink(config.network)

    if kind == "g1-bridge-ws":
        from .g1_bridge_ws_sink import G1BridgeWsSink

        return G1BridgeWsSink(config.network)

    if kind == "g1-direct":
        from .g1_direct_sink import G1DirectSink

        return G1DirectSink(config.network)

    if kind == "local-speaker":
        from .local_speaker_sink import LocalSpeakerSink

        return LocalSpeakerSink()

    raise ValueError(f"未知のsink: {kind}（選択肢: {', '.join(SINK_CHOICES)}）")
