"""
pc_pipeline.audio_utils

PCMの変換ユーティリティ。

このパイプラインで扱うPCMはすべて **16bit signed little-endian** で統一する。
サンプルレートとチャンネル数だけが場所によって変わる。

  G1マイク       : 16kHz mono
  OpenAI STT入力 : 16kHz mono（WAVヘッダを付けて送る）
  OpenAI TTS出力 : 24kHz mono（ヘッダなしの生PCM）
  G1スピーカー   : 16kHz mono  ← ここへ入れる前に必ず24k→16k変換が要る
"""

from __future__ import annotations

import io
import wave
from math import gcd

import numpy as np

PCM_DTYPE = "<i2"  # 16bit signed little-endian

# scipy.signal の初回importは実測で約0.8秒かかる。これを resample_pcm() の中で
# 遅延importすると「セッション最初の応答だけ0.8秒遅い」という形で表面化するため、
# モジュール読み込み時（＝プログラム起動時）に解決してしまう。
try:
    from scipy.signal import resample_poly as _resample_poly
except ImportError:  # scipyが無ければ線形補間にフォールバックする
    _resample_poly = None


def pcm_to_ndarray(pcm: bytes) -> np.ndarray:
    """生PCMバイト列をint16のndarrayにする。"""
    return np.frombuffer(pcm, dtype=PCM_DTYPE)


def ndarray_to_pcm(samples: np.ndarray) -> bytes:
    """ndarrayを16bit LEの生PCMバイト列にする。クリッピング処理込み。"""
    clipped = np.clip(np.round(samples), -32768, 32767)
    return clipped.astype(PCM_DTYPE).tobytes()


def rms(pcm: bytes) -> float:
    """
    PCMチャンクのRMS（0.0〜1.0に正規化）を返す。VADの判定に使う。
    空のチャンクは0.0。
    """
    samples = pcm_to_ndarray(pcm)
    if samples.size == 0:
        return 0.0
    normalized = samples.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(normalized**2)))


def downmix_to_mono(pcm: bytes, channels: int) -> bytes:
    """マルチチャンネルPCMをmonoにミックスダウンする。"""
    if channels <= 1:
        return pcm
    samples = pcm_to_ndarray(pcm)
    usable = (samples.size // channels) * channels
    if usable == 0:
        return b""
    reshaped = samples[:usable].reshape(-1, channels)
    return ndarray_to_pcm(reshaped.mean(axis=1))


def resample_pcm(pcm: bytes, orig_sr: int, target_sr: int) -> bytes:
    """
    16bit PCMのサンプルレートを変換する。

    scipyがあれば resample_poly（多相フィルタ）を使う。24kHz→16kHzは
    ちょうど 2/3 なので up=2, down=3 できれいに落ちる。
    scipyが無い環境では線形補間にフォールバックする（品質は落ちるが動く）。
    """
    if orig_sr == target_sr:
        return pcm

    samples = pcm_to_ndarray(pcm)
    if samples.size == 0:
        return b""

    as_float = samples.astype(np.float32)

    if _resample_poly is not None:
        divisor = gcd(orig_sr, target_sr)
        up = target_sr // divisor
        down = orig_sr // divisor
        resampled = _resample_poly(as_float, up, down)
    else:
        target_len = max(1, int(round(samples.size * target_sr / orig_sr)))
        resampled = np.interp(
            np.linspace(0.0, samples.size - 1, target_len),
            np.arange(samples.size),
            as_float,
        )

    return ndarray_to_pcm(resampled)


def warmup() -> None:
    """
    初回呼び出し時のコストを起動時に前払いしておく。

    scipyのFFTプランなど、最初の1回だけ余分に時間がかかる処理があるため、
    ダミーのPCMで一度通しておく。これをやらないと「最初の応答だけ遅い」
    という形で体感レイテンシに乗ってくる。
    """
    from .config import G1_SAMPLE_RATE, OPENAI_TTS_SAMPLE_RATE

    dummy = b"\x00\x00" * OPENAI_TTS_SAMPLE_RATE  # 1秒ぶんの無音
    resample_pcm(dummy, OPENAI_TTS_SAMPLE_RATE, G1_SAMPLE_RATE)


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """
    生PCMにWAVヘッダを付けてバイト列で返す。
    OpenAIの音声認識APIにファイルとして渡すために使う。
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)  # 16bit固定
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def wav_bytes_to_pcm(wav_bytes: bytes) -> tuple[bytes, int, int]:
    """
    WAVバイト列を (生PCM, サンプルレート, チャンネル数) に分解する。
    16bit以外のWAVは扱わない（例外を投げる）。
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"16bitのWAVのみ対応: sampwidth={wav.getsampwidth()}")
        pcm = wav.readframes(wav.getnframes())
        return pcm, wav.getframerate(), wav.getnchannels()


def pcm_duration_sec(pcm: bytes, sample_rate: int, channels: int = 1) -> float:
    """PCMの再生時間（秒）。再生完了待ちの見積もりに使う。"""
    frame_bytes = 2 * channels
    if frame_bytes == 0 or sample_rate == 0:
        return 0.0
    return len(pcm) / frame_bytes / sample_rate


def to_g1_format(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """
    任意のPCMをG1スピーカーが要求する形式（16kHz mono 16bit LE）に揃える。
    ダウンミックス → リサンプルの順で処理する。
    """
    from .config import G1_SAMPLE_RATE

    mono = downmix_to_mono(pcm, channels)
    return resample_pcm(mono, sample_rate, G1_SAMPLE_RATE)
