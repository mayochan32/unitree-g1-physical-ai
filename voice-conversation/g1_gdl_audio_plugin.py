"""
g1_gdl_audio_plugin.py

G1MicInputAdapter / G1SpeakerOutputAdapter を束ねて、GDLの
「音声I/Oプラグイン」として登録できる形にまとめたラッパー。

【重要な注意】
GDL本体が実際に要求するプラグイン基底クラス・メソッドシグネチャは未確認のため、
このファイルの `GDLAudioIOPluginBase` は「入出力プラグインは大体こういう形だろう」
という一般的な仮の抽象クラスとして書いてある。実際のGDLのプラグインAPIが判明したら、
このクラスをGDL側の本物の基底クラスに差し替えて、メソッド名・引数を合わせること。
（マイク/スピーカー個別のアダプタ実装=G1固有ロジックの部分はそのまま使い回せるはず。）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from g1_mic_input_adapter import G1MicInputAdapter
from g1_speaker_output_adapter import G1SpeakerOutputAdapter


# ---- ここから下は「GDL側の想定インターフェース」の仮置き -------------------
class GDLAudioIOPluginBase(ABC):
    """仮のGDL音声I/Oプラグイン基底クラス。実物に差し替えること。"""

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def play(self, pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> None:
        """GDLのTTSが生成した音声を鳴らす。"""
        ...

    # マイク入力はGDL側がコールバック登録方式を要求する想定。
    # (ジェネレータ/asyncストリーム方式を要求するGDL実装であれば、
    #  G1MicInputAdapterのon_audio_chunkをasyncio.Queueに積む形に変更すればよい)
    @abstractmethod
    def set_audio_chunk_handler(self, handler: Callable[[bytes], None]) -> None:
        ...
# ---------------------------------------------------------------------------


class G1AudioIOPlugin(GDLAudioIOPluginBase):
    """
    GDLから見た「G1用オーディオI/Oプラグイン」の実装。

    使い方（イメージ）:
        plugin = G1AudioIOPlugin(network_interface="eth0")
        gdl.register_audio_plugin(plugin)     # ← 実際のGDL側APIに合わせて呼び方は変わる
        plugin.start()
        ...
        plugin.stop()
    """

    def __init__(
        self,
        network_interface: str = "eth0",
        mic_multicast_group: str = "239.168.123.161",
        mic_port: int = 5555,
        mic_local_ip: str = "192.168.123.164",
    ):
        self._speaker = G1SpeakerOutputAdapter(network_interface=network_interface)
        self._chunk_handler: Optional[Callable[[bytes], None]] = None
        self._mic: Optional[G1MicInputAdapter] = None

        self._mic_multicast_group = mic_multicast_group
        self._mic_port = mic_port
        self._mic_local_ip = mic_local_ip

    def set_audio_chunk_handler(self, handler: Callable[[bytes], None]) -> None:
        """GDLのSTT入力側がここにコールバックを登録し、マイクの生PCMを受け取る。"""
        self._chunk_handler = handler

    def start(self) -> None:
        if self._chunk_handler is None:
            raise RuntimeError(
                "set_audio_chunk_handler() を先に呼び、GDL側のSTT入力コールバックを"
                "登録してください。"
            )

        self._speaker.init()

        self._mic = G1MicInputAdapter(
            on_audio_chunk=self._chunk_handler,
            multicast_group=self._mic_multicast_group,
            port=self._mic_port,
            local_ip=self._mic_local_ip,
        )
        self._mic.start()

    def stop(self) -> None:
        if self._mic is not None:
            self._mic.stop()
            self._mic = None
        self._speaker.close()

    def play(self, pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> None:
        """GDLのTTS出力をG1スピーカーで再生する。"""
        self._speaker.play(pcm_bytes, sample_rate=sample_rate, channels=channels)

    def set_volume(self, volume: int) -> None:
        self._speaker.set_volume(volume)
