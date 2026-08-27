"""
tools/check_speaker.py

G1のスピーカーから音が出るかだけを確認する。

【実行場所に注意】
このスクリプトはUnitree SDK（DDS）を使うので、**G1のPC2上か、SDKが入った
Linux機**で動かす。Windows PCからは動かない可能性が高い。
Windowsから確認したい場合は check_bridge.py（ブリッジ経由）を使うこと。

  # G1のPC2上で
  python3 tools/check_speaker.py --iface eth0
  python3 tools/check_speaker.py --iface eth0 --wav some_audio.wav

WAVを指定しない場合は440Hzのテストトーンを1秒鳴らす。
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time
import wave

SAMPLE_RATE = 16000
CHUNK_BYTES = 96000
CHUNK_SLEEP_SEC = 1.0
APP_NAME = "check_speaker"


def make_test_tone(seconds: float = 1.0, freq: float = 440.0) -> bytes:
    """16kHz mono 16bitのサイン波を生成する。"""
    count = int(SAMPLE_RATE * seconds)
    samples = []
    for index in range(count):
        # 端でプチッと鳴らないようにフェードをかける
        fade = min(1.0, index / 800.0, (count - index) / 800.0)
        value = math.sin(2.0 * math.pi * freq * index / SAMPLE_RATE)
        samples.append(int(value * fade * 12000))
    return struct.pack(f"<{count}h", *samples)


def load_wav(path: str) -> bytes:
    with wave.open(path, "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("16bitのWAVのみ対応しています")
        if wav.getframerate() != SAMPLE_RATE or wav.getnchannels() != 1:
            raise ValueError(
                f"16kHz monoのWAVが必要です "
                f"(このファイル: {wav.getframerate()}Hz {wav.getnchannels()}ch)"
            )
        return wav.readframes(wav.getnframes())


def main() -> int:
    parser = argparse.ArgumentParser(description="G1スピーカーの再生確認")
    parser.add_argument("--iface", default="eth0", help="DDSに使うNIC名")
    parser.add_argument("--wav", default="", help="鳴らすWAV（16kHz mono 16bit）")
    parser.add_argument("--volume", type=int, default=-1, help="音量(0-100)。-1なら変更しない")
    args = parser.parse_args()

    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
    except ImportError:
        print("unitree_sdk2_python が見つかりません。", file=sys.stderr)
        print("このスクリプトはG1のPC2上（またはSDK入りのLinux機）で実行してください。", file=sys.stderr)
        return 1

    pcm = load_wav(args.wav) if args.wav else make_test_tone()
    duration = len(pcm) / (SAMPLE_RATE * 2)
    print(f"再生時間: {duration:.2f}秒 ({len(pcm):,}バイト)")

    ChannelFactoryInitialize(0, args.iface)
    client = AudioClient()
    client.SetTimeout(10.0)
    client.Init()

    if args.volume >= 0:
        client.SetVolume(args.volume)
        print(f"音量を {args.volume} に設定しました")

    stream_id = str(int(time.time() * 1000))
    started = time.monotonic()

    offset = 0
    while offset < len(pcm):
        chunk = pcm[offset : offset + CHUNK_BYTES]
        code = client.PlayStream(APP_NAME, stream_id, chunk)
        if code != 0:
            print(f"PlayStreamが失敗しました: code={code}", file=sys.stderr)
            return 1
        offset += len(chunk)
        print(f"  送信 {offset:,}/{len(pcm):,} バイト")
        if offset < len(pcm):
            time.sleep(CHUNK_SLEEP_SEC)

    # 送信完了 != 再生完了。鳴り終わるまで待つ。
    remaining = duration - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)

    client.PlayStop(APP_NAME)
    print("\n[OK] 送信完了。G1から音が聞こえましたか？")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
