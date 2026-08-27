"""
pc_pipeline.pipeline

会話の状態機械。

  IDLE
   │ トリガ（Enterキー or VADが発話開始を検出）
   ▼
  LISTENING ──── マイクPCMを蓄積
   │ 無音検出 or 最大長
   ▼
  THINKING ───── STT → LLM → TTS（この間マイクは捨てる）
   │
   ▼
  SPEAKING ───── 再生（この間もマイクは捨てる）
   │ 再生完了 + 追加でcooldown分を捨てる
   ▼
  IDLE

【エコー対策】
G1のスピーカーの音をG1のマイクが拾うので、対策なしだと自分の発話を認識して
無限ループする。THINKING/SPEAKINGの間はマイク入力を破棄し、再生後もcooldown
の間だけ捨て続けてからIDLEに戻る。初版は半二重（barge-in非対応）。
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from enum import Enum, auto

from .audio_io.base import AudioSink, AudioSource
from .config import G1_SAMPLE_RATE, PipelineConfig
from .llm import ConversationLLM
from .stt import SpeechToText
from .tts import TextToSpeech
from .vad import SegmentState, SpeechSegmenter


class State(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()


class TriggerMode(Enum):
    PUSH_TO_TALK = "ptt"  # Enterキーで録音開始（実機の立ち上げ時はこれが確実）
    VAD = "vad"           # 声を検出したら自動で録音開始


class ConversationPipeline:
    def __init__(
        self,
        config: PipelineConfig,
        source: AudioSource,
        sink: AudioSink,
        stt: SpeechToText,
        llm: ConversationLLM,
        tts: TextToSpeech,
        trigger_mode: TriggerMode = TriggerMode.PUSH_TO_TALK,
        on_event: Callable[[str, str], None] | None = None,
    ):
        self._config = config
        self._source = source
        self._sink = sink
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._trigger_mode = trigger_mode
        self._on_event = on_event or _default_reporter
        self._state = State.IDLE

    @property
    def state(self) -> State:
        return self._state

    def _set_state(self, state: State) -> None:
        self._state = state

    def _report(self, kind: str, message: str) -> None:
        self._on_event(kind, message)

    # --- LISTENING ---
    def _listen(self) -> bytes | None:
        """1発話ぶんのPCMを取る。取れなければNone。"""
        force_started = self._trigger_mode is TriggerMode.PUSH_TO_TALK
        segmenter = SpeechSegmenter(
            self._config.vad,
            sample_rate=G1_SAMPLE_RATE,
            force_started=force_started,
        )

        # 直前までのチャンクは古いので捨ててから始める
        self._source.drain()
        self._set_state(State.LISTENING)
        self._report("state", "LISTENING（話してください）")

        # 無音のまま延々待ち続けないための上限
        started = time.monotonic()
        idle_limit = self._config.vad.max_utterance_sec + 30.0

        while True:
            chunk = self._source.read_chunk(timeout=0.5)
            if chunk is None:
                if time.monotonic() - started > idle_limit:
                    self._report("warn", "音声が届きません。接続を確認してください。")
                    return None
                continue

            result = segmenter.feed(chunk)
            if result.state is SegmentState.DONE:
                return result.utterance

    # --- THINKING/SPEAKINGの間、マイクを捨て続ける ---
    def _discard_for(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._source.read_chunk(timeout=0.1)
        self._source.drain()

    def _speak(self, text: str) -> None:
        pcm, sample_rate = self._tts.synthesize(text)
        if not pcm:
            return

        self._set_state(State.SPEAKING)
        # 再生はブロックする契約。この間マイクに溜まったものは後でまとめて捨てる。
        self._sink.play(pcm, sample_rate)

        # 残響とバッファ内の残りを流す
        self._discard_for(self._config.echo_cooldown_sec)

    # --- 1ターン ---
    def run_turn(self) -> bool:
        """
        1往復ぶんの会話を処理する。
        Returns: 継続してよければTrue、終了すべきならFalse
        """
        if self._trigger_mode is TriggerMode.PUSH_TO_TALK:
            try:
                command = input("\n[Enter]で話しかける / 'q'+[Enter]で終了 > ").strip()
            except EOFError:
                return False
            if command.lower() in ("q", "quit", "exit"):
                return False

        utterance = self._listen()
        if not utterance:
            self._set_state(State.IDLE)
            return True

        self._set_state(State.THINKING)
        self._report("state", "THINKING")

        # STT
        stt_started = time.monotonic()
        user_text = self._stt.transcribe(utterance, G1_SAMPLE_RATE)
        stt_elapsed = time.monotonic() - stt_started

        if not user_text:
            self._report("warn", "認識結果が空でした")
            self._set_state(State.IDLE)
            return True

        self._report("user", f"{user_text}  （STT {stt_elapsed:.2f}秒）")

        # LLM → TTS → 再生
        reply_started = time.monotonic()
        if self._config.stream_response:
            first_audio_at: float | None = None
            pieces: list[str] = []
            for sentence in self._llm.stream_sentences(user_text):
                pieces.append(sentence)
                if first_audio_at is None:
                    first_audio_at = time.monotonic()
                    self._report(
                        "timing",
                        f"最初の一文まで {first_audio_at - reply_started:.2f}秒",
                    )
                self._speak(sentence)
            self._report("assistant", "".join(pieces))
        else:
            reply = self._llm.respond(user_text)
            llm_elapsed = time.monotonic() - reply_started
            self._report("assistant", f"{reply}  （LLM {llm_elapsed:.2f}秒）")
            self._speak(reply)

        total = time.monotonic() - stt_started
        self._report("timing", f"発話終了から応答完了まで {total:.2f}秒")

        self._set_state(State.IDLE)
        return True

    def run_forever(self) -> None:
        self._report("info", f"トリガ方式: {self._trigger_mode.value}")
        if self._trigger_mode is TriggerMode.VAD:
            self._report("info", "話しかけると自動で認識します。Ctrl+Cで終了。")

        try:
            while self.run_turn():
                pass
        except KeyboardInterrupt:
            self._report("info", "終了します")


def _default_reporter(kind: str, message: str) -> None:
    labels = {
        "state": "  ...",
        "user": "あなた:",
        "assistant": "G1    :",
        "timing": "  [計測]",
        "warn": "  [警告]",
        "info": "  [情報]",
        "error": "  [エラー]",
    }
    stream = sys.stderr if kind in ("warn", "error") else sys.stdout
    print(f"{labels.get(kind, '')} {message}", file=stream, flush=True)
