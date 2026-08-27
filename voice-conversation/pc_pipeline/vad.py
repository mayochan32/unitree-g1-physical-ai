"""
pc_pipeline.vad

RMS閾値ベースの発話区間検出。

ライブラリ依存のないシンプルな実装。まずこれで動かして、G1のサーボ音などで
誤爆するようなら webrtcvad / silero-vad に差し替える想定。
差し替えやすいように、判定は `_is_speech()` の1メソッドに閉じてある。

2つのモードで使える:
  - プッシュトゥトーク: 呼び出し側が録音開始を決め、終端検出だけをVADに任せる
                        （force_started=True でSegmenterを作る）
  - VAD自動           : 発話の開始・終了ともVADが検出する
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum, auto

from .audio_utils import pcm_duration_sec, rms
from .config import VadConfig


class SegmentState(Enum):
    WAITING = auto()   # 発話開始待ち
    SPEAKING = auto()  # 発話中
    DONE = auto()      # 発話完了（utteranceが取り出せる）


@dataclass
class SegmentResult:
    state: SegmentState
    utterance: bytes | None = None  # DONEのときだけ中身が入る
    level: float = 0.0              # 直近チャンクのRMS（デバッグ表示用）


class SpeechSegmenter:
    """
    PCMチャンクを順に食わせると、1発話ぶんのPCMを切り出して返す。

    使い方:
        seg = SpeechSegmenter(config, sample_rate=16000)
        while True:
            chunk = source.read_chunk(timeout=0.5)
            if chunk is None:
                continue
            result = seg.feed(chunk)
            if result.state is SegmentState.DONE:
                utterance = result.utterance
                break
    """

    def __init__(
        self,
        config: VadConfig,
        sample_rate: int,
        force_started: bool = False,
    ):
        """
        Args:
            config: 閾値などのパラメータ
            sample_rate: 入力PCMのサンプルレート
            force_started: Trueなら最初から発話中とみなす（プッシュトゥトーク用）。
                           押した瞬間から録音され、無音検出だけで終端を決める。
        """
        self._config = config
        self._sample_rate = sample_rate
        self._force_started = force_started

        # 発話開始前のチャンクを一時的に溜めておくリングバッファ（語頭の欠け防止）
        self._pre_roll: deque[bytes] = deque()
        self._pre_roll_sec = 0.0

        self._frames: list[bytes] = []
        self._speech_sec = 0.0     # 蓄積した音声の長さ
        self._silence_sec = 0.0    # 発話開始後に連続した無音の長さ
        self._started = force_started

    def reset(self) -> None:
        """次の発話に備えて内部状態を初期化する。"""
        self._pre_roll.clear()
        self._pre_roll_sec = 0.0
        self._frames.clear()
        self._speech_sec = 0.0
        self._silence_sec = 0.0
        self._started = self._force_started

    def _is_speech(self, chunk: bytes) -> tuple[bool, float]:
        """このチャンクが音声かどうか。差し替えるならここだけ変えればよい。"""
        level = rms(chunk)
        return level >= self._config.silence_threshold, level

    def feed(self, chunk: bytes) -> SegmentResult:
        if not chunk:
            return SegmentResult(
                SegmentState.SPEAKING if self._started else SegmentState.WAITING
            )

        is_speech, level = self._is_speech(chunk)
        chunk_sec = pcm_duration_sec(chunk, self._sample_rate)

        # --- まだ発話が始まっていない ---
        if not self._started:
            if is_speech:
                # 発話開始。溜めておいたpre-rollを頭に付けてから本編を積む。
                self._started = True
                self._frames.extend(self._pre_roll)
                self._speech_sec += self._pre_roll_sec
                self._pre_roll.clear()
                self._pre_roll_sec = 0.0

                self._frames.append(chunk)
                self._speech_sec += chunk_sec
                return SegmentResult(SegmentState.SPEAKING, level=level)

            # 無音なのでpre-rollに積み、規定長を超えた分は捨てる
            self._pre_roll.append(chunk)
            self._pre_roll_sec += chunk_sec
            while self._pre_roll and self._pre_roll_sec > self._config.pre_roll_sec:
                dropped = self._pre_roll.popleft()
                self._pre_roll_sec -= pcm_duration_sec(dropped, self._sample_rate)
            return SegmentResult(SegmentState.WAITING, level=level)

        # --- 発話中 ---
        self._frames.append(chunk)
        self._speech_sec += chunk_sec

        if is_speech:
            self._silence_sec = 0.0
        else:
            self._silence_sec += chunk_sec

        # 終端条件1: 無音が規定時間続いた
        if self._silence_sec >= self._config.silence_duration:
            return self._finish(level)

        # 終端条件2: 最大長に達した（暴走防止）
        if self._speech_sec >= self._config.max_utterance_sec:
            return self._finish(level)

        return SegmentResult(SegmentState.SPEAKING, level=level)

    def _finish(self, level: float) -> SegmentResult:
        utterance = b"".join(self._frames)

        # 実質的な発話時間（末尾の無音を除いた長さ）が短すぎるならノイズ扱い
        voiced_sec = self._speech_sec - self._silence_sec
        if voiced_sec < self._config.min_speech_sec:
            self.reset()
            return SegmentResult(SegmentState.WAITING, level=level)

        self.reset()
        return SegmentResult(SegmentState.DONE, utterance=utterance, level=level)
