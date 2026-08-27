"""
tests/test_pipeline_logic.py

外部依存（G1・OpenAI）なしで検証できるロジックのテスト。

  python3 -m pytest tests/ -v
  python3 tests/test_pipeline_logic.py     ← pytestが無くても走る

実機やAPIキーが無くても、リサンプル比・WAVヘッダ・VADの区間検出・
状態機械のエコー対策までを確認できる。
"""

from __future__ import annotations

import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pc_pipeline.audio_utils import (  # noqa: E402
    downmix_to_mono,
    pcm_duration_sec,
    pcm_to_wav_bytes,
    resample_pcm,
    rms,
    wav_bytes_to_pcm,
)
from pc_pipeline.config import VadConfig  # noqa: E402
from pc_pipeline.vad import SegmentState, SpeechSegmenter  # noqa: E402

SR = 16000


def _tone(seconds: float, freq: float = 440.0, amp: int = 12000, sr: int = SR) -> bytes:
    count = int(sr * seconds)
    return struct.pack(
        f"<{count}h",
        *[int(amp * math.sin(2 * math.pi * freq * i / sr)) for i in range(count)],
    )


def _silence(seconds: float, sr: int = SR) -> bytes:
    return b"\x00\x00" * int(sr * seconds)


# --- audio_utils ---------------------------------------------------------

def test_resample_24k_to_16k_length():
    """24kHz→16kHzで長さがちょうど2/3になること（TTS→G1の要）。"""
    pcm = _tone(1.0, sr=24000)
    out = resample_pcm(pcm, 24000, 16000)
    in_samples = len(pcm) // 2
    out_samples = len(out) // 2
    ratio = out_samples / in_samples
    assert abs(ratio - 2 / 3) < 0.01, f"比が2/3でない: {ratio}"


def test_resample_preserves_duration():
    """リサンプル後も再生時間が変わらないこと（速度がおかしくならない）。"""
    pcm = _tone(1.5, sr=24000)
    before = pcm_duration_sec(pcm, 24000)
    out = resample_pcm(pcm, 24000, 16000)
    after = pcm_duration_sec(out, 16000)
    assert abs(before - after) < 0.01, f"再生時間が変化: {before} -> {after}"


def test_resample_noop_same_rate():
    pcm = _tone(0.1)
    assert resample_pcm(pcm, SR, SR) == pcm


def test_resample_empty():
    assert resample_pcm(b"", 24000, 16000) == b""


def test_wav_roundtrip():
    """WAV化して読み戻すと元のPCMに一致すること（STTへの受け渡し）。"""
    pcm = _tone(0.2)
    wav = pcm_to_wav_bytes(pcm, SR)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    restored, sr, ch = wav_bytes_to_pcm(wav)
    assert restored == pcm
    assert sr == SR and ch == 1


def test_rms_levels():
    """無音のRMSはほぼ0、音があれば閾値を明確に超えること。"""
    assert rms(_silence(0.1)) == 0.0
    assert rms(_tone(0.1)) > 0.1
    assert rms(b"") == 0.0


def test_downmix_stereo_to_mono():
    stereo = struct.pack("<8h", 100, 200, 300, 400, 500, 600, 700, 800)
    mono = downmix_to_mono(stereo, 2)
    assert struct.unpack("<4h", mono) == (150, 350, 550, 750)


def test_pcm_duration():
    assert abs(pcm_duration_sec(_tone(2.0), SR) - 2.0) < 1e-6


# --- VAD -----------------------------------------------------------------

def _feed_all(seg: SpeechSegmenter, pcm: bytes, chunk_sec: float = 0.1):
    """PCMを細かいチャンクに割って食わせ、完了したらutteranceを返す。"""
    step = int(SR * chunk_sec) * 2
    for offset in range(0, len(pcm), step):
        result = seg.feed(pcm[offset : offset + step])
        if result.state is SegmentState.DONE:
            return result.utterance
    return None


def test_vad_detects_end_of_speech():
    """発話 → 無音1秒で発話終了を検出すること。"""
    config = VadConfig()
    config.silence_duration = 1.0
    config.min_speech_sec = 0.3
    seg = SpeechSegmenter(config, SR, force_started=True)

    utterance = _feed_all(seg, _tone(1.0) + _silence(1.5))
    assert utterance is not None, "発話終了を検出できなかった"

    duration = pcm_duration_sec(utterance, SR)
    # 発話1.0秒 + 無音1.0秒ぶんで切れるはず（多少の粒度誤差は許容）
    assert 1.8 < duration < 2.3, f"切り出し長が想定外: {duration}"


def test_vad_rejects_too_short():
    """min_speech_secより短い音はノイズとして捨てること。"""
    config = VadConfig()
    config.silence_duration = 0.5
    config.min_speech_sec = 1.0
    seg = SpeechSegmenter(config, SR, force_started=True)

    assert _feed_all(seg, _tone(0.2) + _silence(1.0)) is None


def test_vad_auto_start_skips_leading_silence():
    """VADモードで、先頭の長い無音が発話に含まれないこと。"""
    config = VadConfig()
    config.silence_duration = 0.8
    config.min_speech_sec = 0.3
    config.pre_roll_sec = 0.3
    seg = SpeechSegmenter(config, SR, force_started=False)

    utterance = _feed_all(seg, _silence(3.0) + _tone(1.0) + _silence(1.2))
    assert utterance is not None

    duration = pcm_duration_sec(utterance, SR)
    # pre-roll 0.3 + 発話 1.0 + 無音 0.8 ≒ 2.1秒。3秒の無音は入らない。
    assert duration < 2.6, f"先頭の無音が混入している: {duration}"


def test_vad_max_utterance_cap():
    """最大長で強制的に打ち切られること（暴走防止）。"""
    config = VadConfig()
    config.max_utterance_sec = 2.0
    config.silence_duration = 10.0
    config.min_speech_sec = 0.3
    seg = SpeechSegmenter(config, SR, force_started=True)

    utterance = _feed_all(seg, _tone(5.0))
    assert utterance is not None
    assert pcm_duration_sec(utterance, SR) <= 2.2


# --- パイプライン状態機械（フェイクI/Oで通す） ---------------------------

def test_pipeline_turn_with_fakes():
    """
    STT/LLM/TTSとI/Oを全部フェイクにして1ターン通し、
    エコー対策（再生中にマイクを捨てる）が効いているかを確認する。
    """
    from pc_pipeline.audio_io.base import AudioSink, AudioSource
    from pc_pipeline.config import PipelineConfig
    from pc_pipeline.pipeline import ConversationPipeline, State, TriggerMode

    class FakeSource(AudioSource):
        def __init__(self):
            # 発話1秒 + 無音1.5秒を0.1秒刻みで供給する
            pcm = _tone(1.0) + _silence(1.5)
            step = int(SR * 0.1) * 2
            self.chunks = [pcm[i : i + step] for i in range(0, len(pcm), step)]
            self.index = 0
            self.drain_calls = 0
            self.reads_after_play = 0
            self.playing = False

        def start(self): ...
        def stop(self): ...

        def read_chunk(self, timeout=0.5):
            if self.playing:
                self.reads_after_play += 1
                return _silence(0.05)
            if self.index >= len(self.chunks):
                return None
            chunk = self.chunks[self.index]
            self.index += 1
            return chunk

        def drain(self):
            self.drain_calls += 1
            return 0

    class FakeSink(AudioSink):
        def __init__(self, source):
            self.played = []
            self._source = source

        def play(self, pcm, sample_rate, channels=1):
            self._source.playing = True
            self.played.append((len(pcm), sample_rate))

        def stop(self): ...

    class FakeSTT:
        def transcribe(self, pcm, sample_rate=SR):
            return "こんにちは"

    class FakeLLM:
        def respond(self, text):
            return f"「{text}」と聞こえました。"

        def stream_sentences(self, text):
            yield f"「{text}」と聞こえました。"

    class FakeTTS:
        def synthesize(self, text):
            return _tone(0.5, sr=24000), 24000

    source = FakeSource()
    sink = FakeSink(source)
    config = PipelineConfig()
    config.echo_cooldown_sec = 0.1

    events = []
    pipeline = ConversationPipeline(
        config=config,
        source=source,
        sink=sink,
        stt=FakeSTT(),
        llm=FakeLLM(),
        tts=FakeTTS(),
        trigger_mode=TriggerMode.VAD,  # ptt=input()待ちになるのでVADで回す
        on_event=lambda kind, msg: events.append((kind, msg)),
    )

    assert pipeline.run_turn() is True
    assert pipeline.state is State.IDLE

    # 再生された
    assert len(sink.played) == 1, "再生が呼ばれていない"

    # 再生中/直後にマイクを読み捨てている（エコー対策が動いている）
    assert source.reads_after_play > 0, "再生中にマイクを捨てていない"
    assert source.drain_calls >= 2, "drainが呼ばれていない"

    kinds = [kind for kind, _ in events]
    assert "user" in kinds and "assistant" in kinds


def _run_all():
    """pytestが無くても走らせられる簡易ランナー。"""
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, func in tests:
        try:
            func()
            print(f"  PASS  {name}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} 件成功")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
