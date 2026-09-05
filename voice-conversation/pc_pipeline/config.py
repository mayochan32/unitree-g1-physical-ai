"""
pc_pipeline.config

パイプライン全体の設定。環境変数で上書きできる。

G1本体のオーディオ仕様（16kHz mono 16bit PCM）は固定なので定数として持つ。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# --- G1本体の固定仕様（変更不可） ---------------------------------------
G1_SAMPLE_RATE = 16000       # マイク・スピーカーともに16kHz
G1_CHANNELS = 1              # mono
G1_SAMPLE_WIDTH = 2          # 16bit = 2バイト

# OpenAI TTSの pcm 出力仕様（公式ドキュメント: 24kHz 16bit signed LE ヘッダなし）
OPENAI_TTS_SAMPLE_RATE = 24000


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class NetworkConfig:
    """G1との接続設定。"""

    # G1マイクのマルチキャスト（RockChip系MCUが配信）
    mic_multicast_group: str = field(
        default_factory=lambda: _env("G1_MIC_MCAST_GROUP", "239.168.123.161")
    )
    mic_port: int = field(default_factory=lambda: _env_int("G1_MIC_PORT", 5555))

    # マルチキャストグループに参加する側（このPC）のIP。
    # Unitree公式ドキュメントはユーザPCに 192.168.123.99 を推奨。
    # 複数NICがある環境では必ず明示すること（別NICで待ち受けて無音になるため）。
    local_ip: str = field(default_factory=lambda: _env("G1_LOCAL_IP", "192.168.123.99"))

    # G1側ブリッジ（PC2上で動かす bridge_server.py）
    bridge_host: str = field(default_factory=lambda: _env("G1_BRIDGE_HOST", "192.168.123.164"))
    bridge_port: int = field(default_factory=lambda: _env_int("G1_BRIDGE_PORT", 8765))

    # SDK直接利用時のネットワークインターフェース名（PCがLinuxの場合のみ）
    dds_interface: str = field(default_factory=lambda: _env("G1_DDS_IFACE", "eth0"))

    @property
    def bridge_url(self) -> str:
        return f"ws://{self.bridge_host}:{self.bridge_port}"


@dataclass
class VadConfig:
    """発話区間検出のパラメータ。実機のノイズ環境に合わせて要調整。"""

    # 音声とみなすRMSの閾値（0.0〜1.0に正規化した振幅のRMS）
    # G1向けの既存実装(unitree_converse)では 0.008 が使われていた。
    silence_threshold: float = field(
        default_factory=lambda: _env_float("VAD_SILENCE_THRESHOLD", 0.008)
    )
    # 発話開始後、この秒数だけ無音が続いたら発話終了とみなす
    silence_duration: float = field(
        default_factory=lambda: _env_float("VAD_SILENCE_DURATION", 1.0)
    )
    # 1発話の最大長（暴走防止）
    max_utterance_sec: float = field(
        default_factory=lambda: _env_float("VAD_MAX_UTTERANCE_SEC", 15.0)
    )
    # これより短い発話はノイズとして捨てる
    min_speech_sec: float = field(
        default_factory=lambda: _env_float("VAD_MIN_SPEECH_SEC", 0.3)
    )
    # 発話開始検出の前に遡って残しておく長さ（語頭の欠けを防ぐ）
    pre_roll_sec: float = field(default_factory=lambda: _env_float("VAD_PRE_ROLL_SEC", 0.3))


@dataclass
class OpenAIConfig:
    api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))

    stt_model: str = field(default_factory=lambda: _env("STT_MODEL", "gpt-4o-transcribe"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "gpt-4o-mini"))
    tts_provider: str = field(default_factory=lambda: _env("TTS_PROVIDER", "openai").lower())
    tts_model: str = field(default_factory=lambda: _env("TTS_MODEL", "gpt-4o-mini-tts"))
    tts_voice: str = field(default_factory=lambda: _env("TTS_VOICE", "alloy"))
    elevenlabs_api_key: str = field(
        default_factory=lambda: os.environ.get("ELEVENLABS_API_KEY", "")
    )
    elevenlabs_voice_id: str = field(
        default_factory=lambda: _env("ELEVENLABS_VOICE_ID", "")
    )
    elevenlabs_model: str = field(
        default_factory=lambda: _env("ELEVENLABS_MODEL", "eleven_v3")
    )

    # 音声の言語。日本語なら "ja" を明示した方が精度・速度ともに安定する。
    language: str = field(default_factory=lambda: _env("SPEECH_LANGUAGE", "ja"))

    # TTSの話し方の指示（gpt-4o-mini-tts の instructions パラメータ）
    tts_instructions: str = field(
        default_factory=lambda: _env(
            "TTS_INSTRUCTIONS",
            "自然な日本語で、落ち着いた親しみやすいトーンで話してください。",
        )
    )

    # 会話履歴として保持する往復数（多すぎるとレイテンシとコストが増える）
    max_history_turns: int = field(default_factory=lambda: _env_int("MAX_HISTORY_TURNS", 10))


@dataclass
class PipelineConfig:
    network: NetworkConfig = field(default_factory=NetworkConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)

    # 再生完了後、追加でマイク入力を捨てる時間。
    # G1のスピーカーの残響とバッファ内の残りを流すためのもの。エコー対策上とても重要。
    echo_cooldown_sec: float = field(
        default_factory=lambda: _env_float("ECHO_COOLDOWN_SEC", 0.4)
    )

    # LLMの応答を文単位でストリーミングしてTTSに流すか。
    # True にすると最初の一文から喋り始めるので体感レイテンシが下がる。
    # 初回の立ち上げ時は False（逐次処理）の方がデバッグしやすい。
    stream_response: bool = field(
        default_factory=lambda: _env("STREAM_RESPONSE", "false").lower() == "true"
    )

    system_prompt: str = field(
        default_factory=lambda: _env(
            "SYSTEM_PROMPT",
            "あなたはUnitree G1というヒューマノイドロボットに搭載された対話AIです。"
            "音声で会話しているので、簡潔で自然な話し言葉で答えてください。"
            "箇条書きや記号は使わず、2〜3文程度でまとめてください。",
        )
    )

    # GDLの人格記述ファイル（JSON）へのパス。指定するとsystem_promptに追記される。
    gdl_profile_path: str = field(default_factory=lambda: _env("GDL_PROFILE_PATH", ""))
