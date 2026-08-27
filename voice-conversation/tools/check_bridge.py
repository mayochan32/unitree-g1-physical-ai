"""
tools/check_bridge.py

PC（Windows可）からブリッジ経由でG1スピーカーを鳴らせるか確認する。

G1のPC2上で bridge_server.py を起動した後、PC側からこれを実行する。
SDKは要らず、websocketsだけで動く。

  python tools/check_bridge.py --host 192.168.123.164
  python tools/check_bridge.py --host 192.168.123.164 --wav some_audio.wav

playback_done が返ってくれば、PC→ブリッジ→スピーカーの経路が通っている。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import struct
import sys
import time
import wave

SAMPLE_RATE = 16000
SEND_CHUNK_BYTES = 16000  # 0.5秒分ずつ送る


def make_test_tone(seconds: float = 1.0, freq: float = 440.0) -> bytes:
    count = int(SAMPLE_RATE * seconds)
    samples = []
    for index in range(count):
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


async def run(url: str, pcm: bytes) -> int:
    try:
        import websockets
    except ImportError:
        print("websocketsが必要です: pip install websockets", file=sys.stderr)
        return 1

    duration = len(pcm) / (SAMPLE_RATE * 2)
    print(f"接続先: {url}")
    print(f"再生時間: {duration:.2f}秒 ({len(pcm):,}バイト)")

    try:
        async with websockets.connect(url, max_size=32 * 1024 * 1024) as ws:
            print("接続しました。PCMを送ります…")
            started = time.monotonic()

            await ws.send(json.dumps({"type": "play_start", "sample_rate": SAMPLE_RATE}))
            for offset in range(0, len(pcm), SEND_CHUNK_BYTES):
                await ws.send(pcm[offset : offset + SEND_CHUNK_BYTES])
            await ws.send(json.dumps({"type": "play_end"}))
            print("送信完了。playback_done を待ちます…")

            while True:
                message = await asyncio.wait_for(ws.recv(), timeout=duration + 20.0)
                if isinstance(message, bytes):
                    continue
                event = json.loads(message)
                if event.get("type") == "playback_done":
                    elapsed = time.monotonic() - started
                    print(f"\n[OK] playback_done を受信（往復 {elapsed:.2f}秒）")
                    print("G1から音が聞こえましたか？")
                    return 0
                if event.get("type") == "error":
                    print(f"\n[NG] ブリッジ側エラー: {event.get('message')}", file=sys.stderr)
                    return 1
    except asyncio.TimeoutError:
        print("\n[NG] playback_done が返ってきませんでした", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\n[NG] 接続に失敗しました: {exc}", file=sys.stderr)
        print("  - G1のPC2上で bridge_server.py が起動しているか", file=sys.stderr)
        print("  - --host のIPとポートが正しいか", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="ブリッジ経由のG1スピーカー確認")
    parser.add_argument("--host", default="192.168.123.164", help="ブリッジのホスト")
    parser.add_argument("--port", type=int, default=8765, help="ブリッジのポート")
    parser.add_argument("--wav", default="", help="鳴らすWAV（16kHz mono 16bit）")
    args = parser.parse_args()

    pcm = load_wav(args.wav) if args.wav else make_test_tone()
    return asyncio.run(run(f"ws://{args.host}:{args.port}", pcm))


if __name__ == "__main__":
    raise SystemExit(main())
