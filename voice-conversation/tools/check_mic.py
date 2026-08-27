"""
tools/check_mic.py

【当日いちばん最初に走らせるスクリプト】

G1のマイクのマルチキャストがPCに届いているかだけを確認する。
Unitree SDKを一切使わないので、Windows/macOS/Linuxどこでも動く。

  python tools/check_mic.py
  python tools/check_mic.py --local-ip 192.168.123.99 --seconds 20

【見方】
  - パケットが届いていれば受信バイト数が増えていく
  - 静かにしているとRMSは小さく、声を出すと跳ね上がる
  - 「受信0バイト」なら → ネットワーク設定かマルチキャストのjoin先NICを疑う
  - 「受信はあるがRMSが常に0.0000」なら → G1内蔵の音声アシスタントが
     マイクを掴んでいる可能性がある（調査レポート3章の既知課題）

--save を付けると受信音声をWAVに保存するので、後で本当に声が入っているか
耳で確認できる。
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time
import wave

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2


def compute_rms(pcm: bytes) -> float:
    """0.0〜1.0に正規化したRMS。numpy無しで動かすため手計算する。"""
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    total = 0.0
    for value in struct.unpack(f"<{count}h", pcm[: count * 2]):
        normalized = value / 32768.0
        total += normalized * normalized
    return math.sqrt(total / count)


def main() -> int:
    parser = argparse.ArgumentParser(description="G1マイクのマルチキャスト受信確認")
    parser.add_argument("--group", default="239.168.123.161", help="マルチキャストアドレス")
    parser.add_argument("--port", type=int, default=5555, help="ポート")
    parser.add_argument(
        "--local-ip",
        default="192.168.123.99",
        help="このPCのIP（どのNICで受けるかを決める。複数NIC環境では必須）",
    )
    parser.add_argument("--seconds", type=float, default=15.0, help="計測時間")
    parser.add_argument("--save", default="", help="受信音声のWAV保存先（省略時は保存しない）")
    args = parser.parse_args()

    print(f"マルチキャスト {args.group}:{args.port} を {args.local_ip} で受信します")
    print("（声を出すとRMSが跳ね上がれば成功）\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

    try:
        sock.bind(("", args.port))
        mreq = struct.pack(
            "4s4s", socket.inet_aton(args.group), socket.inet_aton(args.local_ip)
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as exc:
        print(f"ソケットの準備に失敗しました: {exc}", file=sys.stderr)
        print("  --local-ip がこのPCの実際のIPと一致しているか確認してください。", file=sys.stderr)
        return 1

    sock.settimeout(0.5)

    frames: list[bytes] = []
    total_bytes = 0
    packets = 0
    peak_rms = 0.0
    deadline = time.monotonic() + args.seconds
    last_print = 0.0

    try:
        while time.monotonic() < deadline:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue

            packets += 1
            total_bytes += len(data)
            frames.append(data)

            level = compute_rms(data)
            peak_rms = max(peak_rms, level)

            now = time.monotonic()
            if now - last_print >= 0.2:
                last_print = now
                bar = "#" * min(40, int(level * 400))
                print(f"\r  RMS {level:.4f} |{bar:<40}| {total_bytes:>9,}バイト", end="")
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()

    seconds_of_audio = total_bytes / (SAMPLE_RATE * SAMPLE_WIDTH)
    print("\n")
    print("--- 結果 ---")
    print(f"  パケット数    : {packets:,}")
    print(f"  受信バイト数  : {total_bytes:,}")
    print(f"  音声長換算    : {seconds_of_audio:.2f}秒（16kHz mono 16bit想定）")
    print(f"  RMSの最大値   : {peak_rms:.4f}")

    if packets == 0:
        print("\n[NG] パケットが1つも届いていません。")
        print("  - PCとG1が同じL2セグメントにいるか（首のEthernetに有線直結）")
        print("  - PCのIPがG1と同じサブネットか（192.168.123.99 など）")
        print("  - --local-ip が正しいNICのIPを指しているか")
        return 1

    if peak_rms < 0.001:
        print("\n[要注意] パケットは届いていますが、ほぼ無音です。")
        print("  G1内蔵の音声アシスタントがマイクを掴んでいる可能性があります。")
        print("  （調査レポート3章の既知課題。内蔵アシスタントのモードを確認してください）")
    else:
        print("\n[OK] マイク音声が届いています。")

    if args.save and frames:
        with wave.open(args.save, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(b"".join(frames))
        print(f"\n  WAVに保存しました: {args.save}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
