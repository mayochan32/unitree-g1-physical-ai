"""
g1_bridge/bridge_server.py

G1のPC2上で動かす中継サーバ（標準ライブラリのみ版）。

【この版の狙い：G1の環境を一切変更しない】
G1は共有機材なので、pip installでシステムのPythonを汚したくない。
そこで通信をWebSocketから素のTCPに変え、**Python標準ライブラリだけで動くように**
してある。追加インストールもsudoも設定変更も不要。

  scp bridge_server.py unitree@192.168.123.164:/tmp/
  ssh unitree@192.168.123.164 "python3 /tmp/bridge_server.py --iface eth0"

これだけで動く。/tmp に置けば再起動で自動的に消えるので痕跡も残らない。
（unitree_sdk2py だけはG1に元から入っているものを使う）

【痕跡を残さないための配慮】
- 起動時に本体の音量を読んでおき、終了時に元に戻す（AudioClient.SetVolumeは
  本体設定を変えるため、次に使う人に影響しないようにする）
- ファイルは書かない。ログは標準出力のみ
- 終了時にPlayStopを呼んで再生状態をリセットする

【プロトコル】
長さ付きの単純なフレーム。

    [1バイト: 種別][4バイト: ペイロード長(ビッグエンディアン)][ペイロード]

    種別 0x01 = JSON制御メッセージ
    種別 0x02 = 生PCM（16kHz mono 16bit LE）

  PC → G1
    JSON {"type":"play_start","sample_rate":16000}
    PCM  ...（複数フレーム）
    JSON {"type":"play_end"}
    JSON {"type":"stop"}                    再生中断
    JSON {"type":"mic_start"} / {"type":"mic_stop"}
    JSON {"type":"ping"}

  G1 → PC
    JSON {"type":"playback_done"}           ← 実際に鳴り終わってから送る
    JSON {"type":"error","message":"..."}
    JSON {"type":"pong"}
    PCM  マイク音声（mic_start中のみ）

【重要】playback_done は「送信完了」ではなく「再生完了」で返す。
PlayStreamは送るだけなので、音声長ぶんの時間が経つまで待ってから通知する。
これをしないとPC側のエコー対策の区間がずれ、G1が自分の発話を拾ってループする。

再生用とマイク用で別々のTCP接続を張る想定。サーバは接続ごとにスレッドを立てるので、
両方が同時に来ても問題ない。
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import threading
import time

# --- G1の固定仕様 ---
G1_SAMPLE_RATE = 16000
G1_CHANNELS = 1
G1_SAMPLE_WIDTH = 2

# PlayStreamのチャンク分割（公式サンプル準拠: 96000バイト = 約3秒分）
CHUNK_BYTES = 96000
CHUNK_SLEEP_SEC = 1.0

APP_NAME = "pc_pipeline"

# マイクのマルチキャスト（RockChip系MCUが配信）
MIC_MULTICAST_GROUP = "239.168.123.161"
MIC_PORT = 5555
MIC_LOCAL_IP = "192.168.123.164"  # G1のPC2自身のIP

# --- フレーム種別 ---
FRAME_JSON = 0x01
FRAME_PCM = 0x02

_HEADER = struct.Struct("!BI")  # 種別1バイト + 長さ4バイト
MAX_FRAME_BYTES = 64 * 1024 * 1024


# =========================================================================
# フレームの送受信（標準ライブラリのみ）
# =========================================================================

def send_frame(sock: socket.socket, frame_type: int, payload: bytes) -> None:
    sock.sendall(_HEADER.pack(frame_type, len(payload)) + payload)


def send_json(sock: socket.socket, obj: dict) -> None:
    send_frame(sock, FRAME_JSON, json.dumps(obj).encode("utf-8"))


def recv_exact(sock: socket.socket, count: int) -> bytes | None:
    """countバイト読み切る。接続が閉じたらNone。"""
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(min(remaining, 1 << 20))
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    """1フレーム読む。接続が閉じたらNone。"""
    header = recv_exact(sock, _HEADER.size)
    if header is None:
        return None
    frame_type, length = _HEADER.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise ValueError(f"フレームが大きすぎます: {length}バイト")
    payload = recv_exact(sock, length) if length else b""
    if payload is None:
        return None
    return frame_type, payload


# =========================================================================
# スピーカー再生
# =========================================================================

class SpeakerPlayer:
    """AudioClientをラップして再生する。音量は起動時の値を覚えて終了時に戻す。"""

    def __init__(self, iface: str, timeout_sec: float = 10.0):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

        ChannelFactoryInitialize(0, iface)
        self._client = AudioClient()
        self._client.SetTimeout(timeout_sec)
        self._client.Init()
        self._lock = threading.Lock()

        # 共有機材への配慮：元の音量を覚えておき、終了時に戻す
        self._original_volume = self._read_volume()
        if self._original_volume is not None:
            print(f"[bridge] 現在の音量を記録: {self._original_volume}（終了時に戻す）", flush=True)
        else:
            print("[bridge] 音量の取得に失敗（終了時の復元は行わない）", flush=True)

        print(f"[bridge] AudioClient初期化完了 (iface={iface})", flush=True)

    def _read_volume(self) -> int | None:
        try:
            code, data = self._client.GetVolume()
            if code == 0 and isinstance(data, dict):
                for key in ("volume", "Volume", "vol"):
                    if key in data:
                        return int(data[key])
        except Exception:
            pass
        return None

    def set_volume(self, volume: int) -> None:
        self._client.SetVolume(volume)

    def play_blocking(self, pcm: bytes) -> None:
        """
        PCMを再生する。実際に鳴り終わるまでブロックする。
        PlayStreamは送信するだけなので、音声長ぶん待つ処理を自前で入れている。
        """
        if not pcm:
            return

        duration = len(pcm) / (G1_SAMPLE_RATE * G1_SAMPLE_WIDTH * G1_CHANNELS)
        stream_id = str(int(time.time() * 1000))

        with self._lock:
            started = time.monotonic()
            offset = 0
            while offset < len(pcm):
                chunk = pcm[offset : offset + CHUNK_BYTES]
                code = self._client.PlayStream(APP_NAME, stream_id, chunk)
                if code != 0:
                    raise RuntimeError(f"PlayStream失敗: code={code}")
                offset += len(chunk)
                if offset < len(pcm):
                    time.sleep(CHUNK_SLEEP_SEC)

            remaining = duration - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)

            self._client.PlayStop(APP_NAME)

    def stop(self) -> None:
        try:
            self._client.PlayStop(APP_NAME)
        except Exception:
            pass

    def restore(self) -> None:
        """終了時に呼ぶ。再生を止め、音量を元に戻す。"""
        self.stop()
        if self._original_volume is not None:
            try:
                self._client.SetVolume(self._original_volume)
                print(f"[bridge] 音量を {self._original_volume} に戻した", flush=True)
            except Exception as exc:
                print(f"[bridge] 音量の復元に失敗: {exc}", flush=True)


# =========================================================================
# マイクのマルチキャスト受信
# =========================================================================

def open_mic_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("", MIC_PORT))
    mreq = struct.pack(
        "4s4s",
        socket.inet_aton(MIC_MULTICAST_GROUP),
        socket.inet_aton(MIC_LOCAL_IP),
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(0.2)
    return sock


# =========================================================================
# 接続ごとの処理
# =========================================================================

class Connection:
    def __init__(self, sock: socket.socket, addr, player: SpeakerPlayer):
        self._sock = sock
        self._addr = addr
        self._player = player
        self._send_lock = threading.Lock()  # マイクスレッドと再生応答が競合するため
        self._buffer = bytearray()
        self._receiving = False
        self._mic_thread: threading.Thread | None = None
        self._mic_running = threading.Event()

    def _send_json(self, obj: dict) -> None:
        with self._send_lock:
            send_json(self._sock, obj)

    def _send_pcm(self, pcm: bytes) -> None:
        with self._send_lock:
            send_frame(self._sock, FRAME_PCM, pcm)

    def serve(self) -> None:
        print(f"[bridge] 接続: {self._addr}", flush=True)
        try:
            while True:
                frame = recv_frame(self._sock)
                if frame is None:
                    break
                frame_type, payload = frame

                if frame_type == FRAME_PCM:
                    if self._receiving:
                        self._buffer.extend(payload)
                    continue

                if frame_type == FRAME_JSON:
                    self._handle_json(json.loads(payload.decode("utf-8")))
                    continue

                self._send_json(
                    {"type": "error", "message": f"未知のフレーム種別: {frame_type}"}
                )
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            print(f"[bridge] 接続エラー: {exc}", flush=True)
        finally:
            self._stop_mic()
            try:
                self._sock.close()
            except OSError:
                pass
            print(f"[bridge] 切断: {self._addr}", flush=True)

    def _handle_json(self, event: dict) -> None:
        kind = event.get("type")

        if kind == "play_start":
            self._buffer = bytearray()
            self._receiving = True

        elif kind == "play_end":
            self._receiving = False
            pcm = bytes(self._buffer)
            self._buffer = bytearray()
            self._play(pcm)

        elif kind == "stop":
            self._player.stop()

        elif kind == "set_volume":
            self._player.set_volume(int(event.get("volume", 50)))

        elif kind == "mic_start":
            self._start_mic()

        elif kind == "mic_stop":
            self._stop_mic()

        elif kind == "ping":
            self._send_json({"type": "pong"})

        else:
            self._send_json({"type": "error", "message": f"未知のtype: {kind}"})

    def _play(self, pcm: bytes) -> None:
        try:
            self._player.play_blocking(pcm)
            self._send_json({"type": "playback_done"})
            print(f"[bridge] 再生完了 ({len(pcm)}バイト)", flush=True)
        except Exception as exc:
            self._send_json({"type": "error", "message": str(exc)})
            print(f"[bridge] 再生エラー: {exc}", flush=True)

    def _start_mic(self) -> None:
        if self._mic_thread is not None:
            return
        self._mic_running.set()
        self._mic_thread = threading.Thread(target=self._mic_loop, daemon=True)
        self._mic_thread.start()
        print("[bridge] マイク中継を開始", flush=True)

    def _stop_mic(self) -> None:
        if self._mic_thread is None:
            return
        self._mic_running.clear()
        self._mic_thread.join(timeout=2.0)
        self._mic_thread = None
        print("[bridge] マイク中継を停止", flush=True)

    def _mic_loop(self) -> None:
        try:
            sock = open_mic_socket()
        except OSError as exc:
            self._send_json({"type": "error", "message": f"マイク受信の開始に失敗: {exc}"})
            return
        try:
            while self._mic_running.is_set():
                try:
                    data, _addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if data:
                    try:
                        self._send_pcm(data)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
        finally:
            sock.close()


# =========================================================================
# サーバ本体
# =========================================================================

def run(iface: str, host: str, port: int) -> None:
    player = SpeakerPlayer(iface)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(8)
    server.settimeout(1.0)

    print(f"[bridge] tcp://{host}:{port} で待機中", flush=True)
    print("[bridge] 停止する場合は Ctrl+C（音量は自動で元に戻ります）", flush=True)

    try:
        while True:
            try:
                client_sock, addr = server.accept()
            except socket.timeout:
                continue
            client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection = Connection(client_sock, addr, player)
            threading.Thread(target=connection.serve, daemon=True).start()
    except KeyboardInterrupt:
        print("\n[bridge] 終了します", flush=True)
    finally:
        server.close()
        player.restore()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="G1音声ブリッジ（PC2上で実行 / 標準ライブラリのみ）"
    )
    parser.add_argument("--iface", default="eth0", help="DDSに使うNIC名（既定: eth0）")
    parser.add_argument("--host", default="0.0.0.0", help="待ち受けアドレス")
    parser.add_argument("--port", type=int, default=8765, help="待ち受けポート")
    args = parser.parse_args()

    try:
        run(args.iface, args.host, args.port)
    except ImportError as exc:
        print(f"unitree_sdk2py が読み込めません: {exc}", file=sys.stderr)
        print("このスクリプトはG1のPC2上で実行してください。", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
