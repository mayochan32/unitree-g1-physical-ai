"""
pc_pipeline.main

CLIエントリポイント。

音声の入り口と出口をコマンドラインで差し替えられるようにしてある。
実機に行く前はPCだけで開発し、当日は設定を切り替えるだけで済ませるための仕掛け。

使い方:

  # 事前開発（G1不要。PCのマイクとスピーカーだけで一通り動かす）
  python -m pc_pipeline.main --source local-mic --sink local-speaker

  # 本命（G1のマイクを直接受信 + ブリッジ経由でG1スピーカー）
  python -m pc_pipeline.main --source g1-multicast --sink g1-bridge

  # 縮退運転（G1にSSHできずブリッジを置けなかった場合）
  python -m pc_pipeline.main --source g1-multicast --sink local-speaker

  # 全部G1経由（マルチキャストが届かない場合）
  python -m pc_pipeline.main --source g1-bridge --sink g1-bridge

  # VAD自動（プッシュトゥトークをやめる）
  python -m pc_pipeline.main --source g1-multicast --sink g1-bridge --trigger vad
"""

from __future__ import annotations

import argparse
import sys

from .audio_io import SINK_CHOICES, SOURCE_CHOICES, create_sink, create_source
from .config import PipelineConfig
from .llm import ConversationLLM
from .pipeline import ConversationPipeline, TriggerMode
from .stt import SpeechToText
from .tts import TextToSpeech


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc_pipeline",
        description="Unitree G1 音声会話パイプライン（PC側で STT/LLM/TTS を実行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        choices=SOURCE_CHOICES,
        default="g1-multicast",
        help="音声入力元（既定: g1-multicast）",
    )
    parser.add_argument(
        "--sink",
        choices=SINK_CHOICES,
        default="g1-bridge",
        help="音声出力先（既定: g1-bridge）",
    )
    parser.add_argument(
        "--trigger",
        choices=[mode.value for mode in TriggerMode],
        default=TriggerMode.PUSH_TO_TALK.value,
        help="発話開始のトリガ（既定: ptt = Enterキー）",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="LLM応答を文単位でTTSに流す（体感レイテンシが下がる）",
    )
    parser.add_argument(
        "--gdl-profile",
        default=None,
        help="GDL人格プロファイル(JSON)のパス。システムプロンプトに注入される。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config = PipelineConfig()
    if args.stream:
        config.stream_response = True
    if args.gdl_profile is not None:
        config.gdl_profile_path = args.gdl_profile

    if not config.openai.api_key:
        print(
            "環境変数 OPENAI_API_KEY が設定されていません。\n"
            "  Windows(PowerShell): $env:OPENAI_API_KEY=\"sk-...\"\n"
            "  Linux/macOS        : export OPENAI_API_KEY=sk-...",
            file=sys.stderr,
        )
        return 1

    print("=" * 60)
    print(f"  入力: {args.source}")
    print(f"  出力: {args.sink}")
    print(f"  STT : {config.openai.stt_model}")
    print(f"  LLM : {config.openai.llm_model}")
    if config.openai.tts_provider == "elevenlabs":
        print(
            f"  TTS : elevenlabs / model={config.openai.elevenlabs_model} "
            f"/ voice_id={config.openai.elevenlabs_voice_id}"
        )
    else:
        print(f"  TTS : {config.openai.tts_model} / voice={config.openai.tts_voice}")
    if config.gdl_profile_path:
        print(f"  GDL : {config.gdl_profile_path}")
    print("=" * 60)

    # リサンプルの初回コスト（scipyのimport等で実測0.8秒程度）を
    # ここで前払いしておく。最初の応答だけ遅くなるのを防ぐ。
    from .audio_utils import warmup

    warmup()

    try:
        source = create_source(args.source, config)
        sink = create_sink(args.sink, config)
    except Exception as exc:
        print(f"音声I/Oの初期化に失敗しました: {exc}", file=sys.stderr)
        return 1

    try:
        stt = SpeechToText(config.openai)
        llm = ConversationLLM(
            config.openai,
            system_prompt=config.system_prompt,
            gdl_profile_path=config.gdl_profile_path,
        )
        tts = TextToSpeech(config.openai)
    except Exception as exc:
        print(f"OpenAIクライアントの初期化に失敗しました: {exc}", file=sys.stderr)
        return 1

    pipeline = ConversationPipeline(
        config=config,
        source=source,
        sink=sink,
        stt=stt,
        llm=llm,
        tts=tts,
        trigger_mode=TriggerMode(args.trigger),
    )

    try:
        source.start()
        pipeline.run_forever()
    finally:
        source.stop()
        sink.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
