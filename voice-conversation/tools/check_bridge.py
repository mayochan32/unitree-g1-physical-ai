"""
tools/check_bridge.py

PC（Windows可）からブリッジ経由でG1スピーカーを鳴らせるか確認する。

G1のPC2上で bridge_server.py を起動した後、PC側からこれを実行する。
**標準ライブラリだけで動く**のでSDKもwebsocketsも要らない。

  python tools\\check_bridge.py --host 192.168.123.164
  python tools\\check_bridge.py --host 192.168.123.164 --wav some_audio.wav
  python tools\\check_bridge.py --host 192.168.123.164 --mic 5

playback_done が返ってくれば、PC→ブリッジ→スピーカーの経路が通っている。
--mic を付けると、ブリッジ経由のマイク中継も確認できる。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import struct
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pc_pipeline.audio_io.bridge_protocol import (  # noqa: E402
    FRAME_PCM,
    BridgeConnection,
    connect,
)

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


def check_playback(conn: BridgeConnection, pcm: bytes) -> int:
    duration = len(pcm) / (SAMPLE_RATE * 2)
    print(f"再生時間: {duration:.2f}秒 ({len(pcm):,}バイト)")
    print("PCMを送ります…")

    conn.settimeout(duration + 20.0)
    started = time.monotonic()

    conn.send_json({"type": "play_start", "sample_rate": SAMPLE_RATE})
    for offset in range(0, len(pcm), SEND_CHUNK_BYTES):
        conn.send_pcm(pcm[offset : offset + SEND_CHUNK_BYTES])
    conn.send_json({"type": "play_end"})
    print("送信完了。playback_done を待ちます…")

    try:
        event = conn.recv_json()
    except socket.timeout:
        print("\n[NG] playback_done が返ってきませんでした", file=sys.stderr)
        return 1

    if event is None:
        print("\n[NG] 接続が切れました", file=sys.stderr)
        return 1
    if event.get("type") == "error":
        print(f"\n[NG] ブリッジ側エラー: {event.get('message')}", file=sys.stderr)
        return 1
    if event.get("type") != "playback_done":
        print(f"\n[NG] 予期しない応答: {event}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    print(f"\n[OK] playback_done を受信（往復 {elapsed:.2f}秒）")
    if elapsed < duration * 0.8:
        print("  [注意] 往復が音声長より明らかに短い。")
        print("         「送信完了」で返っている可能性があり、エコー対策の同期がずれる。")
    print("G1から音が聞こえましたか？")
    return 0


def check_mic(conn: BridgeConnection, seconds: float) -> int:
    print(f"\nブリッジ経由のマイク中継を {seconds:.0f}秒 確認します。話しかけてください。")
    conn.send_json({"type": "mic_start"})
    conn.settimeout(1.0)

    total = 0
    peak = 0.0
    deadline = time.monotonic() + seconds
    last_print = 0.0

    while time.monotonic() < deadline:
        try:
            frame = conn.recv_frame()
        except socket.timeout:
            continue
        if frame is None:
            break
        frame_type, payload = frame

        # ブリッジ側のエラー（マルチキャストのjoin失敗など）は理由を出す
        if frame_type != FRAME_PCM:
            try:
                event = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if event.get("type") == "error":
                print(f"\n[NG] ブリッジ側エラー: {event.get('message')}", file=sys.stderr)
                return 1
            continue

        if not payload:
            continue

        total += len(payload)
        count = len(payload) // 2
        if count:
            acc = 0.0
            for value in struct.unpack(f"<{count}h", payload[: count * 2]):
                norm = value / 32768.0
                acc += norm * norm
            level = math.sqrt(acc / count)
            peak = max(peak, level)

            now = time.monotonic()
            if now - last_print >= 0.2:
                last_print = now
                bar = "#" * min(40, int(level * 400))
                print(f"\r  RMS {level:.4f} |{bar:<40}| {total:>9,}バイト", end="")

    try:
        conn.send_json({"type": "mic_stop"})
    except OSError:
        pass

    print("\n")
    print(f"  受信バイト数: {total:,}")
    print(f"  RMSの最大値  : {peak:.4f}")
    if total == 0:
        print("\n[NG] マイクデータが届きませんでした。")
        print("  G1側でマルチキャストが受信できているか、ブリッジのログを確認してください。")
        return 1
    if peak < 0.001:
        print("\n[要注意] データは届いていますが、ほぼ無音です。")
        print("  G1内蔵の音声アシスタントがマイクを掴んでいる可能性があります。")
        return 0
    print("\n[OK] ブリッジ経由でマイク音声が届いています。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ブリッジ経由のG1スピーカー/マイク確認")
    parser.add_argument("--host", default="192.168.123.164", help="ブリッジのホスト")
    parser.add_argument("--port", type=int, default=8765, help="ブリッジのポート")
    parser.add_argument("--wav", default="", help="鳴らすWAV（16kHz mono 16bit）")
    parser.add_argument(
        "--mic",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="再生確認の後、マイク中継も指定秒数だけ確認する",
    )
    args = parser.parse_args()

    pcm = load_wav(args.wav) if args.wav else make_test_tone()

    print(f"接続先: tcp://{args.host}:{args.port}")
    try:
        conn = connect(args.host, args.port)
    except RuntimeError as exc:
        print(f"\n[NG] {exc}", file=sys.stderr)
        return 1

    print("接続しました。")
    try:
        status = check_playback(conn, pcm)
        if status == 0 and args.mic > 0:
            status = check_mic(conn, args.mic)
        return status
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
